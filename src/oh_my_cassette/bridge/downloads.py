"""Result downloads (contract §5): fetch declared artifacts into the workspace."""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path

import anyio
import httpx
import mcp_types as types

from oh_my_cassette.bridge.errors import BridgeError
from oh_my_cassette.bridge.files import CHUNK, same_origin
from oh_my_cassette.bridge.settings import BridgeSettings
from oh_my_cassette.contract import ContractError, DownloadSpec, download_specs, pointer_set


def safe_file_name(name: str) -> str:
    base = re.split(r"[\\/]", name.strip())[-1]
    cleaned = re.sub(r"[^\w.\- ()]+", "_", base).strip(" .")
    return cleaned[:200] or "download"


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def _note(text: str) -> types.TextContent:
    return types.TextContent(type="text", text=text)


class Downloads:
    def __init__(self, settings: BridgeSettings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

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
        for spec in specs:
            try:
                path = await self.fetch(spec)
            except BridgeError as exc:
                content.append(
                    _note(f"Download of {spec.file_name} failed ({exc.message}); it is still at {spec.url}")
                )
                continue
            content.append(_note(f"Downloaded {spec.file_name} to {path}"))
            if spec.pointer and isinstance(structured, dict):
                try:
                    pointer_set(structured, spec.pointer, str(path))
                except ContractError:
                    pass
        return result.model_copy(update={"content": content, "structured_content": structured})

    async def fetch(self, spec: DownloadSpec) -> Path:
        directory = self._settings.downloads
        directory.mkdir(parents=True, exist_ok=True)
        name = safe_file_name(spec.file_name)
        part = directory / f".{name}.{id(spec):x}.part"
        headers: dict[str, str] = {}
        if self._settings.auth_token and same_origin(spec.url, self._settings.mcp_url):
            headers["Authorization"] = f"Bearer {self._settings.auth_token}"
        digest = hashlib.sha256()
        total = 0
        try:
            async with self._http.stream("GET", spec.url, headers=headers, follow_redirects=True) as response:
                if response.status_code != 200:
                    raise BridgeError(
                        "bridge.download_failed",
                        f"HTTP {response.status_code}",
                        retryable=response.status_code >= 500,
                    )
                async with await anyio.open_file(part, "wb") as handle:
                    async for chunk in response.aiter_bytes(CHUNK):
                        total += len(chunk)
                        if total > self._settings.max_download_bytes:
                            raise BridgeError(
                                "bridge.download_failed", "larger than CASSETTE_MAX_DOWNLOAD_MB"
                            )
                        digest.update(chunk)
                        await handle.write(chunk)
            if spec.size is not None and total != spec.size:
                raise BridgeError(
                    "bridge.download_failed", f"got {total} bytes, expected {spec.size}", retryable=True
                )
            if spec.sha256 is not None and digest.hexdigest() != spec.sha256:
                raise BridgeError("bridge.download_failed", "sha256 mismatch", retryable=True)
            final = unique_path(directory, name)
            part.rename(final)
            return final
        except httpx.HTTPError as exc:
            raise BridgeError(
                "bridge.download_failed", str(exc) or type(exc).__name__, retryable=True
            ) from exc
        finally:
            part.unlink(missing_ok=True)
