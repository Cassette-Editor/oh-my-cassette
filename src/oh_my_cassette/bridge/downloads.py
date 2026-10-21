"""Result downloads: fetch declared artifacts into the workspace.

A download writes to a `.part` file named after its URL, so one cut short resumes where it stopped,
in this call or a later one: a ranged request appends to it when the server answers 206 and starts
over when it answers 200. Transient failures (network, 5xx, 429) are retried a few times and leave
the part in place; anything else removes it.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
import mcp_types as types

from oh_my_cassette.auth import Credentials
from oh_my_cassette.bridge.errors import BridgeError, backoff_seconds, retry_after_seconds
from oh_my_cassette.bridge.files import CHUNK, same_origin
from oh_my_cassette.bridge.settings import BridgeSettings
from oh_my_cassette.contract import ContractError, DownloadSpec, download_specs, pointer_set


def safe_file_name(name: str) -> str:
    base = re.split(r"[\\/]", name.strip())[-1]
    cleaned = re.sub(r"[^\w.\- ()]+", "_", base).strip(" .")
    return cleaned[:200] or "download"


def reserve_path(directory: Path, name: str) -> Path:
    """A new empty file in `directory` named `name`, or `name (n)` when taken. Created exclusively, so a file
    that appears meanwhile, or a symlink planted under the name, is never written through or replaced."""
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 0
    while True:
        candidate = directory / (name if counter == 0 else f"{stem} ({counter}){suffix}")
        try:
            os.close(os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
            return candidate
        except FileExistsError:
            counter += 1


def shown(url: str) -> str:
    """A URL without its query, which may carry a signature."""
    parts = urlsplit(url)
    return parts._replace(query="", fragment="").geturl()


def _open_part(path: Path, append: bool):
    """The part file for writing, refusing a symlink in its place."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC) | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o644), "ab" if append else "wb")


DOWNLOAD_ATTEMPTS = 4
MAX_DOWNLOADS = 16  # files one result may declare; any further ones are left at their URLs
CONTENT_RANGE = re.compile(r"bytes (\d+)-\d+/(?:\d+|\*)")


def _note(text: str) -> types.TextContent:
    return types.TextContent(type="text", text=text)


def _hash_into(digest: Any, path: Path) -> None:
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)


class _Interrupted(Exception):
    """A transient failure: the part keeps what arrived and the download may be tried again."""

    def __init__(self, detail: str, retry_after: float | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retry_after = retry_after


class Downloads:
    def __init__(
        self, settings: BridgeSettings, http: httpx.AsyncClient, *, credentials: Credentials | None = None
    ) -> None:
        self._settings = settings
        self._credentials = credentials or Credentials(settings.mcp_url, explicit_token=settings.auth_token)
        self._http = http
        self._etags: dict[str, str] = {}  # a URL's strong ETag, as this process last saw it
        self._parts: dict[Path, anyio.Lock] = {}  # one download at a time per part file

    async def apply(self, result: types.CallToolResult) -> types.CallToolResult:
        try:
            specs = download_specs(result.meta)
        except ContractError as exc:
            notes = [_note(f"bridge: ignored a malformed downloads declaration ({exc})")]
            return result.model_copy(update={"content": [*result.content, *notes]})
        if not specs:
            return result
        content = list(result.content)
        structured = copy.deepcopy(result.structured_content)
        for spec in specs[MAX_DOWNLOADS:]:
            content.append(_note(f"bridge: did not download {spec.file_name}; it is at {shown(spec.url)}"))
        for spec in specs[:MAX_DOWNLOADS]:
            try:
                path = await self.fetch(spec)
            except BridgeError as exc:
                content.append(
                    _note(
                        f"Download of {spec.file_name} failed ({exc.message}); it is still at {shown(spec.url)}"
                    )
                )
                continue
            content.append(_note(f"Downloaded {spec.file_name} to {path}"))
            if spec.pointer and isinstance(structured, dict):
                try:
                    pointer_set(structured, spec.pointer, str(path))
                except ContractError:
                    pass
        return result.model_copy(update={"content": content, "structured_content": structured})

    def _directory(self) -> Path:
        """The downloads directory, which must stay inside the workspace unless CASSETTE_DOWNLOAD_DIR names it:
        a `cassette-exports` symlink that leads elsewhere is refused."""
        directory = self._settings.downloads
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BridgeError("bridge.download_failed", f"cannot create {directory}: {exc.strerror}") from exc
        real = directory.resolve()
        if self._settings.download_dir is None and not real.is_relative_to(
            self._settings.workspace.resolve()
        ):
            raise BridgeError(
                "bridge.download_failed",
                f"{directory} leads outside the workspace (set CASSETTE_DOWNLOAD_DIR to download there)",
            )
        return real

    async def fetch(self, spec: DownloadSpec) -> Path:
        directory = self._directory()
        name = safe_file_name(spec.file_name)
        part = directory / f".{name}.{hashlib.sha256(spec.url.encode()).hexdigest()[:12]}.part"
        async with self._parts.setdefault(part, anyio.Lock()):
            return await self._fetch(spec, directory, name, part)

    async def _fetch(self, spec: DownloadSpec, directory: Path, name: str, part: Path) -> Path:
        headers: dict[str, str] = {}
        if same_origin(spec.url, self._settings.mcp_url):
            headers.update(await self._credentials.headers(spec.url))
        attempt = 0
        try:
            while True:
                try:
                    total, sha256 = await self._receive(spec, part, headers)
                    break
                except _Interrupted as exc:
                    if attempt + 1 >= DOWNLOAD_ATTEMPTS:
                        raise BridgeError("bridge.download_failed", exc.detail, retryable=True) from exc
                    await anyio.sleep(backoff_seconds(attempt, exc.retry_after))
                    attempt += 1
            if spec.size is not None and total != spec.size:
                raise BridgeError(
                    "bridge.download_failed", f"got {total} bytes, expected {spec.size}", retryable=True
                )
            if spec.sha256 is not None and sha256 != spec.sha256:
                raise BridgeError("bridge.download_failed", "sha256 mismatch", retryable=True)
            final = reserve_path(directory, name)
            os.replace(part, final)
            return final
        except BridgeError as exc:
            if not isinstance(exc.__cause__, _Interrupted):  # only a transient failure keeps the part
                part.unlink(missing_ok=True)
            raise

    async def _receive(self, spec: DownloadSpec, part: Path, headers: dict[str, str]) -> tuple[int, str]:
        """Bring `part` up to the whole file; its size and sha256. Raises `_Interrupted` when a transient
        failure stopped it."""
        if part.is_symlink():  # never resumed into, nor written through
            part.unlink()
        have = part.stat().st_size if part.exists() else 0
        if have > self._settings.max_download_bytes or (spec.size is not None and have > spec.size):
            part.unlink()
            have = 0
        digest = hashlib.sha256()
        if have:
            await anyio.to_thread.run_sync(_hash_into, digest, part)
        # Byte offsets are only meaningful in the file's own bytes, so it is asked for unencoded.
        request = {**headers, "Accept-Encoding": "identity"}
        if have:
            request["Range"] = f"bytes={have}-"
            if (etag := self._etags.get(spec.url)) is not None:
                request["If-Range"] = etag
        try:
            async with self._http.stream("GET", spec.url, headers=request, follow_redirects=True) as response:
                status = response.status_code
                if status == 416 and have:
                    if spec.size is None or have == spec.size:
                        return have, digest.hexdigest()  # the part already holds every byte
                    part.unlink()
                    raise _Interrupted("the partial download no longer matches the file", 0.0)
                if status == 429 or status >= 500:
                    raise _Interrupted(
                        f"HTTP {status}", retry_after_seconds(response.headers.get("retry-after"))
                    )
                if status not in (200, 206) or (status == 206 and not have):
                    raise BridgeError("bridge.download_failed", f"HTTP {status}")
                if status == 206:
                    match = CONTENT_RANGE.fullmatch(response.headers.get("content-range", "").strip())
                    if match is None or int(match.group(1)) != have:
                        part.unlink()
                        raise _Interrupted("the server resumed at another offset", 0.0)
                else:
                    have, digest = 0, hashlib.sha256()  # the whole file again
                etag = response.headers.get("etag")
                if etag and not etag.startswith("W/"):
                    self._etags[spec.url] = etag
                total = have
                async with anyio.wrap_file(_open_part(part, append=bool(have))) as handle:
                    async for chunk in response.aiter_raw():  # as it arrives: a cut keeps what came
                        total += len(chunk)
                        if total > self._settings.max_download_bytes:
                            raise BridgeError(
                                "bridge.download_failed", "larger than CASSETTE_MAX_DOWNLOAD_MB"
                            )
                        digest.update(chunk)
                        await handle.write(chunk)
                return total, digest.hexdigest()
        except httpx.HTTPError as exc:
            raise _Interrupted(str(exc) or type(exc).__name__) from exc
