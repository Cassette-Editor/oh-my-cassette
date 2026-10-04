"""Local file arguments (contract §3): the local policy, preparation, the upload handshake, refs.

The backend decides which arguments are local paths, which MIME types it accepts and how media is
prepared; this module decides which files may leave the machine. The backend can narrow that
policy, never widen it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import shutil
import tempfile
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
import mcp_types as types
from mcp.shared.exceptions import MCPError

from oh_my_cassette.bridge.errors import BridgeError, unauthorized, unreachable
from oh_my_cassette.bridge.jobs import DONE, TRANSCODING, UPLOADING, FileProgress, LocalJob
from oh_my_cassette.bridge.prepare import Preparer
from oh_my_cassette.bridge.settings import BridgeSettings
from oh_my_cassette.bridge.upstream import BackendUnavailable, Unauthorized, Upstream
from oh_my_cassette.contract import (
    ARTIFACT_ORIGINAL,
    DEFAULT_LOCAL_ACCEPT,
    ROLE_UPLOAD_BEGIN,
    ROLE_UPLOAD_COMPLETE,
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
            raise unreachable(self._settings.mcp_url, str(exc), during_call=exc.during_call) from exc
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
        artifacts, report = await self._preparer.transcode(
            source.path, preparation.renditions, workdir, transcoding
        )
        renditions = tuple(
            [
                UploadArtifact(a.role, a.path, a.path.stat().st_size, await sha256_file(a.path), a.mime_type)
                for a in artifacts
            ]
        )
        name = source.path.name
        return UploadFile(index, name, source.relative, source.size, digest, source.mime, renditions, report)

    # ── handshake (contract §3.1) ──

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
        pending: dict[str, tuple[UploadFile, dict[str, tuple[str, dict[str, str]]]]] = {}
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
                pending[entry["uploadId"]] = (file, self._targets(begin.name, file, entry.get("puts")))
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

        for file, targets in pending.values():
            await self._send(file, targets, job.files[file.index])
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
    def _targets(tool_name: str, file: UploadFile, puts: Any) -> dict[str, tuple[str, dict[str, str]]]:
        """Each artifact role's PUT target, checked: exactly one per artifact, http(s) only."""
        targets: dict[str, tuple[str, dict[str, str]]] = {}
        for put in puts if isinstance(puts, list) else [None]:
            if not isinstance(put, Mapping) or not isinstance(put.get("role"), str):
                raise BridgeError("bridge.contract_violation", f"{tool_name} returned a malformed put target")
            url, headers = put.get("url"), put.get("headers") or {}
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise BridgeError("bridge.contract_violation", f"{tool_name}: upload URLs must be http(s)")
            if not isinstance(headers, Mapping):
                raise BridgeError("bridge.contract_violation", f"{tool_name}: put headers must be an object")
            targets[put["role"]] = (url, {str(k): str(v) for k, v in headers.items()})
        roles = [artifact.role for artifact in file.artifacts]
        if sorted(targets) != sorted(roles) or len(targets) != len(puts):
            raise BridgeError(
                "bridge.contract_violation",
                f"{tool_name} must return one put per artifact of {file.name} ({', '.join(roles)})",
            )
        return targets

    async def _send(
        self, file: UploadFile, targets: dict[str, tuple[str, dict[str, str]]], progress: FileProgress
    ) -> None:
        total, sent = max(1, sum(artifact.size for artifact in file.artifacts)), 0

        def count(size: int) -> None:
            nonlocal sent
            sent += size
            progress.update(UPLOADING, 100 * sent / total)

        for artifact in file.artifacts:
            await self._put(file, artifact, targets[artifact.role], count)

    async def _put(
        self,
        file: UploadFile,
        artifact: UploadArtifact,
        target: tuple[str, dict[str, str]],
        sent: Callable[[int], None],
    ) -> None:
        url, given = target
        # The presigned URL is bound to the headers it was signed with, Content-Type included.
        headers = {**given, "Content-Length": str(artifact.size)}
        if same_origin(url, self._settings.mcp_url):
            headers.update(await self._upstream.credentials.headers(url))

        async def body():
            async with await anyio.open_file(artifact.path, "rb") as handle:
                while chunk := await handle.read(CHUNK):
                    sent(len(chunk))
                    yield chunk

        what = f"uploading {file.relative} ({artifact.role})"
        try:
            response = await self._http.put(url, content=body(), headers=headers)
        except httpx.HTTPError as exc:
            raise BridgeError("bridge.upload_failed", f"{what}: {exc}", retryable=True) from exc
        if response.status_code >= 300:
            raise BridgeError(
                "bridge.upload_failed",
                f"{what}: storage answered HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )
