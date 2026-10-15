"""Local file arguments: the local policy, preparation, the upload handshake, refs.

The backend decides which arguments are local paths, which MIME types it accepts and how media is
prepared; this module decides which files may leave the machine. The backend can narrow that
policy, never widen it. An artifact goes up in one PUT, or in parts whose URLs the backend's
`upload.parts` tool signs a batch at a time; each part is retried on its own.
"""

from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
import mcp_types as types
from mcp.shared.exceptions import MCPError

from oh_my_cassette.bridge.errors import (
    BridgeError,
    backoff_seconds,
    rate_limited,
    retry_after_seconds,
    unauthorized,
    unreachable,
)
from oh_my_cassette.bridge.jobs import DONE, TRANSCODING, UPLOADING, FileProgress, LocalJob
from oh_my_cassette.bridge.prepare import Preparer
from oh_my_cassette.bridge.settings import BridgeSettings
from oh_my_cassette.bridge.upstream import BackendUnavailable, RateLimited, Unauthorized, Upstream
from oh_my_cassette.contract import (
    ARTIFACT_ORIGINAL,
    DEFAULT_LOCAL_ACCEPT,
    ROLE_UPLOAD_BEGIN,
    ROLE_UPLOAD_COMPLETE,
    ROLE_UPLOAD_PARTS,
    ContractError,
    Convert,
    LocalFilesSpec,
    local_files_spec,
    mime_accepted,
    pointer_get,
    pointer_set,
    preparation_for,
)

CHUNK = 1024 * 1024
PART_ATTEMPTS = 4  # sends of one part before a transient failure is reported
PART_URL_MAX_AGE_SEC = 600.0  # a part URL signed longer ago than this is signed again before use

# Media types the platform tables often miss.
for _ext, _mime in {
    ".m4a": "audio/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".mxf": "video/mxf",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".jfif": "image/jpeg",
    ".apng": "image/apng",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
}.items():
    mimetypes.add_type(_mime, _ext)


def guess_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def same_origin(url: str, other: str) -> bool:
    a, b = urlsplit(url), urlsplit(other)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


async def sha256_file(path: Path) -> str:
    return await anyio.to_thread.run_sync(_sha256, path)


def _text(result: types.CallToolResult) -> str:
    return (
        " ".join(block.text for block in result.content if isinstance(block, types.TextContent))
        or "no details"
    )


@dataclass(frozen=True)
class LocalSource:
    """A local file a call names, after the local policy: its real path and identity."""

    path: Path
    relative: str
    size: int
    mtime_ns: int
    mime: str


@dataclass(frozen=True)
class LocalCall:
    """One host call's local files. `sources` are distinct; the n-th one is clientRef `f<n>`."""

    tool: str
    spec: LocalFilesSpec
    arguments: dict[str, Any]
    slots: tuple[tuple[str, int | None, Path], ...]
    sources: tuple[LocalSource, ...]

    def client_ref(self, path: Path) -> str:
        return f"f{next(i for i, source in enumerate(self.sources) if source.path == path)}"

    def _substitute(self, value: Callable[[Path], str]) -> dict[str, Any]:
        args = copy.deepcopy(self.arguments)
        for pointer, index, path in self.slots:
            if index is None:
                pointer_set(args, pointer, value(path))
            else:
                pointer_get(args, pointer)[1][index] = value(path)
        return args

    def client_arguments(self) -> dict[str, Any]:
        """The call's arguments with each local path replaced by its clientRef (sent to upload.begin)."""
        return self._substitute(self.client_ref)

    def with_refs(self, refs: Mapping[Path, str]) -> dict[str, Any]:
        """The call's arguments with each local path replaced by the backend's ref."""
        return self._substitute(lambda path: refs[path])

    @property
    def key(self) -> Hashable:
        """What makes two calls the same local work: tool, preparation, arguments and file identities."""
        prepare = self.spec.prepare.digest if self.spec.prepare else None
        arguments = json.dumps(self.client_arguments(), sort_keys=True, default=str)
        return (self.tool, prepare, arguments, tuple((s.path, s.mtime_ns, s.size) for s in self.sources))

    def progress_files(self) -> list[FileProgress]:
        return [
            FileProgress(
                source.relative,
                (TRANSCODING, UPLOADING)
                if preparation_for(self.spec.prepare, source.mime, source.path.name)
                else (UPLOADING,),
            )
            for source in self.sources
        ]


@dataclass(frozen=True)
class PutTarget:
    """Where one artifact goes: one PUT to `url` with `headers`, or `parts` of (part size, part count)."""

    url: str
    headers: dict[str, str]
    parts: tuple[int, int] | None


@dataclass(frozen=True)
class SignedParts:
    completed: frozenset[int]
    urls: dict[int, str]


@dataclass(frozen=True)
class UploadArtifact:
    role: str
    path: Path
    size: int
    sha256: str
    mime: str


@dataclass(frozen=True)
class UploadFile:
    """A file as upload.begin describes it. For a video, name/size/sha256/mime are the source's."""

    index: int
    name: str
    relative: str
    size: int
    sha256: str
    mime: str
    artifacts: tuple[UploadArtifact, ...]
    preparation: dict[str, Any] | None

    @property
    def client_ref(self) -> str:
        return f"f{self.index}"

    def wire(self) -> dict[str, Any]:
        return {
            "clientRef": self.client_ref,
            "name": self.name,
            "relativePath": self.relative,
            "size": self.size,
            "sha256": self.sha256,
            "mimeType": self.mime,
            "artifacts": [
                {"role": a.role, "size": a.size, "sha256": a.sha256, "mimeType": a.mime}
                for a in self.artifacts
            ],
            "preparation": self.preparation,
        }


class LocalFiles:
    def __init__(
        self, settings: BridgeSettings, upstream: Upstream, http: httpx.AsyncClient, preparer: Preparer
    ) -> None:
        self._settings = settings
        self._upstream = upstream
        self._http = http
        self._preparer = preparer

    # ── policy ──

    def check(self, raw: str, spec: LocalFilesSpec) -> LocalSource:
        """Resolve one argument under the local policy."""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self._settings.workspace / path
        try:
            real = path.resolve(strict=True)
        except OSError:
            raise BridgeError("bridge.file_not_found", f"{raw}: no such file") from None
        if not real.is_file():
            raise BridgeError("bridge.file_not_found", f"{raw}: not a regular file")
        root = next((root for root in self._settings.roots if real.is_relative_to(root)), None)
        if root is None:
            raise BridgeError(
                "bridge.file_outside_workspace",
                f"{raw} is outside the workspace {self._settings.workspace} "
                "(add its directory to CASSETTE_ALLOWED_ROOTS to allow it)",
            )
        stat = real.stat()
        if stat.st_size > self._settings.max_upload_bytes:
            limit = self._settings.max_upload_bytes // (1024 * 1024)
            raise BridgeError("bridge.file_too_large", f"{raw} is larger than CASSETTE_MAX_UPLOAD_MB={limit}")
        mime = guess_mime(real)
        local_ok = self._settings.upload_any_type or mime_accepted(mime, DEFAULT_LOCAL_ACCEPT)
        if not (local_ok and mime_accepted(mime, spec.accept)):
            raise BridgeError(
                "bridge.file_type_rejected",
                f"{raw} ({mime}) is not an accepted type ({', '.join(spec.accept)})",
            )
        return LocalSource(real, real.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns, mime)

    def plan(self, tool: types.Tool, arguments: dict[str, Any] | None) -> LocalCall | None:
        """The local files a call references, checked; None when it references none."""
        try:
            spec = local_files_spec(tool.meta)
        except ContractError as exc:
            raise BridgeError("bridge.contract_violation", f"tool {tool.name}: {exc}") from exc
        if spec is None or not arguments:
            return None
        args = copy.deepcopy(arguments)
        named: list[tuple[str, int | None, str]] = []
        for pointer in spec.pointers:
            found, value = pointer_get(args, pointer)
            if not found or value is None:
                continue
            if isinstance(value, str):
                named.append((pointer, None, value))
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                named.extend((pointer, index, item) for index, item in enumerate(value))
            else:
                raise BridgeError("bridge.invalid_argument", f"{pointer} must be a path or a list of paths")
        if not named:
            return None
        sources: dict[Path, LocalSource] = {}
        slots: list[tuple[str, int | None, Path]] = []
        for pointer, index, raw in named:
            source = self.check(raw, spec)
            sources.setdefault(source.path, source)
            slots.append((pointer, index, source.path))
        if self._upstream.tool_for_role(ROLE_UPLOAD_BEGIN) is None or (
            self._upstream.tool_for_role(ROLE_UPLOAD_COMPLETE) is None
        ):
            raise BridgeError(
                "bridge.contract_violation",
                f"tool {tool.name} takes local files but the backend offers no upload tools",
            )
        return LocalCall(tool.name, spec, args, tuple(slots), tuple(sources.values()))

    # ── the local phase ──

    async def run(self, call: LocalCall, job: LocalJob) -> dict[Path, str]:
        """Prepare and upload a call's files; the backend's ref for each real path."""
        root = self._settings.temp_root
        root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="job-", dir=root))
        try:
            files = [
                await self._describe(index, source, call, workdir / f"f{index}", job.files[index])
                for index, source in enumerate(call.sources)
            ]
            return await self._upload(call, files, job)
        except Unauthorized as exc:
            raise unauthorized(self._settings.mcp_url, has_token=bool(self._settings.auth_token)) from exc
        except BackendUnavailable as exc:
            raise unreachable(
                self._settings.mcp_url, str(exc), during_call=exc.during_call, retry_after=exc.retry_after
            ) from exc
        except RateLimited as exc:
            raise rate_limited(self._settings.mcp_url, exc.retry_after) from exc
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(shutil.rmtree, workdir, True)

    async def _describe(
        self, index: int, source: LocalSource, call: LocalCall, workdir: Path, progress: FileProgress
    ) -> UploadFile:
        preparation = preparation_for(call.spec.prepare, source.mime, source.path.name)

        def transcoding(percent: float) -> None:
            progress.update(TRANSCODING, percent)

        if preparation is None:
            digest = await sha256_file(source.path)
            original = UploadArtifact(ARTIFACT_ORIGINAL, source.path, source.size, digest, source.mime)
            name = source.path.name
            return UploadFile(
                index, name, source.relative, source.size, digest, source.mime, (original,), None
            )
        if isinstance(preparation, Convert):
            conversion = preparation.conversion
            output = await self._preparer.convert(
                source.path, preparation.kind, conversion, workdir, transcoding
            )
            size, digest = output.stat().st_size, await sha256_file(output)
            converted = UploadArtifact(ARTIFACT_ORIGINAL, output, size, digest, conversion.mime_type)
            name = f"{source.path.stem}.{conversion.extension}"
            return UploadFile(
                index, name, source.relative, size, digest, conversion.mime_type, (converted,), None
            )
        digest = await sha256_file(source.path)
        artifacts, report = await self._preparer.transcode(source.path, preparation, workdir, transcoding)
        renditions = tuple(
            [
                UploadArtifact(a.role, a.path, a.path.stat().st_size, await sha256_file(a.path), a.mime_type)
                for a in artifacts
            ]
        )
        name = source.path.name
        return UploadFile(index, name, source.relative, source.size, digest, source.mime, renditions, report)

    # ── handshake ──

    async def _call(self, tool: types.Tool, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            result = await self._upstream.call_tool(tool.name, arguments)
        except MCPError as exc:
            raise BridgeError("bridge.upload_failed", f"{tool.name}: {exc.message}", retryable=True) from exc
        if result.is_error:
            raise BridgeError("bridge.upload_failed", f"{tool.name}: {_text(result)}", retryable=True)
        return result

    async def _upload(self, call: LocalCall, files: list[UploadFile], job: LocalJob) -> dict[Path, str]:
        begin = self._upstream.tool_for_role(ROLE_UPLOAD_BEGIN)
        complete = self._upstream.tool_for_role(ROLE_UPLOAD_COMPLETE)
        parts = self._upstream.tool_for_role(ROLE_UPLOAD_PARTS)
        if begin is None or complete is None:
            raise BridgeError("bridge.contract_violation", "the backend no longer offers its upload tools")
        for file in files:
            job.files[file.index].update(UPLOADING)
        started = await self._call(
            begin,
            {
                "tool": call.tool,
                "arguments": call.client_arguments(),
                "files": [file.wire() for file in files],
            },
        )
        uploads = (started.structured_content or {}).get("uploads")
        if not isinstance(uploads, list):
            raise BridgeError("bridge.contract_violation", f"{begin.name} returned no uploads list")

        by_ref = {file.client_ref: file for file in files}
        refs: dict[Path, str] = {}
        pending: dict[str, tuple[UploadFile, dict[str, PutTarget]]] = {}
        seen: set[str] = set()
        for entry in uploads:
            client_ref = entry.get("clientRef") if isinstance(entry, Mapping) else None
            if client_ref not in by_ref or client_ref in seen:
                raise BridgeError(
                    "bridge.contract_violation", f"{begin.name} returned an unknown or repeated upload entry"
                )
            seen.add(client_ref)
            file = by_ref[client_ref]
            if isinstance(entry.get("uploadId"), str):
                targets = self._targets(begin.name, file, entry.get("puts"))
                if parts is None and any(target.parts for target in targets.values()):
                    raise BridgeError(
                        "bridge.contract_violation",
                        f"{begin.name} asked for {file.name} in parts but the backend offers no tool to sign them",
                    )
                pending[entry["uploadId"]] = (file, targets)
            elif isinstance(entry.get("ref"), str):  # content the backend already has: nothing to send
                refs[call.sources[file.index].path] = entry["ref"]
                job.files[file.index].update(DONE)
            else:
                raise BridgeError(
                    "bridge.contract_violation",
                    f"{begin.name} returned neither uploadId nor ref for {file.name}",
                )
        if seen != set(by_ref):
            raise BridgeError("bridge.contract_violation", f"{begin.name} did not return one entry per file")

        for upload_id, (file, targets) in pending.items():
            await self._send(parts, upload_id, file, targets, job.files[file.index])
        if pending:
            finished = await self._call(complete, {"uploadIds": list(pending)})
            for entry in (finished.structured_content or {}).get("files") or []:
                if not isinstance(entry, Mapping) or entry.get("uploadId") not in pending:
                    continue
                file, _ = pending[entry["uploadId"]]
                if isinstance(entry.get("error"), Mapping):
                    message = entry["error"].get("message") or entry["error"].get("code") or "failed"
                    raise BridgeError("bridge.upload_failed", f"{file.relative}: {message}", retryable=True)
                if isinstance(entry.get("ref"), str):
                    refs[call.sources[file.index].path] = entry["ref"]
                    job.files[file.index].update(DONE)
        missing = [source.relative for source in call.sources if source.path not in refs]
        if missing:
            raise BridgeError(
                "bridge.contract_violation", f"{complete.name} returned no ref for {', '.join(missing)}"
            )
        return refs

    @staticmethod
    def _targets(tool_name: str, file: UploadFile, puts: Any) -> dict[str, PutTarget]:
        """Each artifact role's PUT target, checked: exactly one per artifact, http(s) only, parts that
        cover the artifact exactly."""
        sizes = {artifact.role: artifact.size for artifact in file.artifacts}
        targets: dict[str, PutTarget] = {}
        for put in puts if isinstance(puts, list) else [None]:
            if not isinstance(put, Mapping) or not isinstance(put.get("role"), str):
                raise BridgeError("bridge.contract_violation", f"{tool_name} returned a malformed put target")
            url, headers, multipart = put.get("url"), put.get("headers") or {}, put.get("multipart")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise BridgeError("bridge.contract_violation", f"{tool_name}: upload URLs must be http(s)")
            if not isinstance(headers, Mapping):
                raise BridgeError("bridge.contract_violation", f"{tool_name}: put headers must be an object")
            parts: tuple[int, int] | None = None
            if multipart is not None:
                plan = multipart if isinstance(multipart, Mapping) else {}
                part_size, part_count = plan.get("partSize"), plan.get("partCount")
                size = max(1, sizes.get(put["role"], 0))
                if not (
                    _whole(part_size)
                    and _whole(part_count)
                    and part_size * (part_count - 1) < size <= part_size * part_count
                ):
                    raise BridgeError(
                        "bridge.contract_violation",
                        f"{tool_name}: the parts of {file.name} ({put['role']}) must cover it exactly",
                    )
                parts = (part_size, part_count)
            targets[put["role"]] = PutTarget(url, {str(k): str(v) for k, v in headers.items()}, parts)
        roles = [artifact.role for artifact in file.artifacts]
        if sorted(targets) != sorted(roles) or len(targets) != len(puts):
            raise BridgeError(
                "bridge.contract_violation",
                f"{tool_name} must return one put per artifact of {file.name} ({', '.join(roles)})",
            )
        return targets

    async def _send(
        self,
        parts: types.Tool | None,
        upload_id: str,
        file: UploadFile,
        targets: dict[str, PutTarget],
        progress: FileProgress,
    ) -> None:
        total, sent = max(1, sum(artifact.size for artifact in file.artifacts)), 0

        def count(size: int) -> None:
            nonlocal sent
            sent += size
            progress.update(UPLOADING, 100 * sent / total)

        for artifact in file.artifacts:
            target = targets[artifact.role]
            if target.parts is None:
                await self._put(file, artifact, target, count)
            else:
                assert parts is not None  # `_upload` refuses parts without the tool that signs them
                await self._put_parts(parts, upload_id, file, artifact, target.parts, count)

    async def _put(
        self, file: UploadFile, artifact: UploadArtifact, target: PutTarget, sent: Callable[[int], None]
    ) -> None:
        # The presigned URL is bound to the headers it was signed with, Content-Type included.
        headers = {**target.headers, "Content-Length": str(artifact.size)}
        if same_origin(target.url, self._settings.mcp_url):
            headers.update(await self._upstream.credentials.headers(target.url))
        what = f"uploading {file.relative} ({artifact.role})"
        try:
            response = await self._http.put(
                target.url, content=_read(artifact.path, 0, artifact.size, sent), headers=headers
            )
        except httpx.HTTPError as exc:
            raise BridgeError("bridge.upload_failed", f"{what}: {exc}", retryable=True) from exc
        if response.status_code >= 300:
            raise BridgeError(
                "bridge.upload_failed",
                f"{what}: storage answered HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )

    async def _sign_parts(
        self,
        tool: types.Tool,
        upload_id: str,
        artifact: UploadArtifact,
        plan: tuple[int, int],
        numbers: list[int] | None = None,
    ) -> SignedParts:
        arguments: dict[str, Any] = {"uploadId": upload_id, "role": artifact.role}
        if numbers:
            arguments["partNumbers"] = numbers
        data = (await self._call(tool, arguments)).structured_content or {}
        completed, signed = data.get("completed"), data.get("parts")
        if (data.get("partSize"), data.get("partCount")) != plan:
            raise BridgeError(
                "bridge.contract_violation",
                f"{tool.name} changed the parts of {artifact.role} since upload.begin",
            )
        urls: dict[int, str] = {}
        for part in signed if isinstance(signed, list) else [None]:
            number = part.get("partNumber") if isinstance(part, Mapping) else None
            url = part.get("url") if isinstance(part, Mapping) else None
            if not (_whole(number) and number <= plan[1] and isinstance(url, str)) or not url.startswith(
                ("http://", "https://")
            ):
                raise BridgeError("bridge.contract_violation", f"{tool.name} returned a malformed part")
            urls[number] = url
        if not isinstance(completed, list) or not all(_whole(n) and n <= plan[1] for n in completed):
            raise BridgeError("bridge.contract_violation", f"{tool.name} returned a malformed completed list")
        return SignedParts(frozenset(completed), urls)

    async def _put_parts(
        self,
        tool: types.Tool,
        upload_id: str,
        file: UploadFile,
        artifact: UploadArtifact,
        plan: tuple[int, int],
        sent: Callable[[int], None],
    ) -> None:
        """Send every part the store does not hold yet, a signed batch at a time."""
        part_size, part_count = plan
        what = f"uploading {file.relative} ({artifact.role})"
        counted: set[int] = set()  # parts whose bytes are in the progress already
        put: set[int] = set()  # parts sent in this run

        def span(number: int) -> tuple[int, int]:
            start = (number - 1) * part_size
            return start, min(artifact.size, start + part_size) - start

        while True:
            batch = await self._sign_parts(tool, upload_id, artifact, plan)
            signed_at = anyio.current_time()
            for number in batch.completed - counted:  # held from an earlier attempt
                counted.add(number)
                sent(span(number)[1])
            if not batch.urls:
                if batch.completed >= set(range(1, part_count + 1)):
                    return
                raise BridgeError(
                    "bridge.contract_violation",
                    f"{tool.name} signed no part of {file.name} while parts are missing",
                )
            urls = dict(batch.urls)
            for number in sorted(urls):
                if number in put:
                    raise BridgeError(
                        "bridge.upload_failed",
                        f"{what}: the store did not keep part {number}",
                        retryable=True,
                    )
                if anyio.current_time() - signed_at > PART_URL_MAX_AGE_SEC:
                    left = [n for n in sorted(urls) if n >= number]
                    urls |= (await self._sign_parts(tool, upload_id, artifact, plan, left)).urls
                    signed_at = anyio.current_time()

                async def sign_again(number: int = number) -> str:
                    again = (await self._sign_parts(tool, upload_id, artifact, plan, [number])).urls
                    if number not in again:
                        raise BridgeError(
                            "bridge.contract_violation", f"{tool.name} did not sign part {number} again"
                        )
                    return again[number]

                await self._put_part(what, artifact, number, urls[number], span(number), sign_again, sent)
                put.add(number)
                counted.add(number)

    async def _put_part(
        self,
        what: str,
        artifact: UploadArtifact,
        number: int,
        url: str,
        span: tuple[int, int],
        sign_again: Callable[[], Awaitable[str]],
        sent: Callable[[int], None],
    ) -> None:
        """One part, retried: a refused URL (403/404) is signed again once, a transient failure (network,
        5xx, 429) is sent again after the store's `Retry-After` or 1, 2, 4… seconds."""
        start, length = span
        resigned, attempt = False, 0
        while True:
            moved = 0

            def count(size: int) -> None:
                nonlocal moved
                moved += size
                sent(size)

            # A part URL is signed for its bytes only: no content type, and no credential elsewhere.
            headers = {"Content-Length": str(length)}
            if same_origin(url, self._settings.mcp_url):
                headers.update(await self._upstream.credentials.headers(url))
            status, retry_after, detail = None, None, ""
            try:
                response = await self._http.put(
                    url, content=_read(artifact.path, start, length, count), headers=headers
                )
                status, retry_after = (
                    response.status_code,
                    retry_after_seconds(response.headers.get("retry-after")),
                )
                detail = f"storage answered HTTP {status}"
            except httpx.HTTPError as exc:
                detail = str(exc) or type(exc).__name__
            if status is not None and status < 300:
                return
            sent(-moved)
            if status in (403, 404) and not resigned:
                url, resigned = await sign_again(), True
                continue
            transient = status is None or status >= 500 or status == 429
            if not transient or attempt + 1 >= PART_ATTEMPTS:
                raise BridgeError(
                    "bridge.upload_failed", f"{what}: part {number}: {detail}", retryable=transient
                )
            await anyio.sleep(backoff_seconds(attempt, retry_after))
            attempt += 1


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


async def _read(path: Path, start: int, length: int, sent: Callable[[int], None]) -> AsyncIterator[bytes]:
    """`length` bytes of `path` from `start`, counted as they go."""
    async with await anyio.open_file(path, "rb") as handle:
        await handle.seek(start)
        left = length
        while left > 0 and (chunk := await handle.read(min(CHUNK, left))):
            left -= len(chunk)
            sent(len(chunk))
            yield chunk
