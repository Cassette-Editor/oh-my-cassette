"""Thin async HTTP layer over the Cassette backend: bearer auth, typed errors, bounded retries, SSE."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from oh_my_cassette import __version__
from oh_my_cassette.cassette.errors import HttpError, TransportError

log = logging.getLogger("oh_my_cassette.http")

TokenProvider = Callable[[], Awaitable[str | None]]

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


@dataclass
class SseFrame:
    event: str | None
    data: str
    id: str | None

    def json(self) -> Any:
        return json.loads(self.data)


def error_from_response(response: httpx.Response) -> HttpError:
    body: Any = None
    try:
        body = response.json()
    except ValueError:
        body = None
    code = f"http_{response.status_code}"
    message = response.reason_phrase or code
    retryable = response.status_code in RETRYABLE_STATUS
    details: Any = None
    if isinstance(body, dict):
        raw_code = body.get("code") or body.get("errorCode")
        raw_error = body.get("error")
        raw_message = body.get("message")
        if isinstance(raw_code, str) and raw_code:
            code = raw_code
        elif (
            isinstance(raw_error, str) and raw_error and " " not in raw_error.strip() and len(raw_error) < 64
        ):
            code = raw_error
        if isinstance(raw_message, str) and raw_message:
            message = raw_message
        elif isinstance(raw_error, str) and raw_error:
            message = raw_error
        if isinstance(body.get("retryable"), bool):
            retryable = body["retryable"]
        details = {
            k: v for k, v in body.items() if k not in {"code", "error", "message", "statusCode"}
        } or None
    elif body is None and response.content:
        message = response.text[:300]
    return HttpError(response.status_code, code, message, retryable=retryable, details=details)


class CassetteHttp:
    def __init__(
        self,
        base_url: str,
        *,
        token_provider: TokenProvider | None = None,
        timeout_sec: float = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._timeout = timeout_sec
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_sec, connect=15),
            headers={"user-agent": f"oh-my-cassette/{__version__}"},
            transport=transport,
            follow_redirects=False,
        )
        # Presigned PUT/GET targets live on other origins and must not see the bearer token.
        self._external = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30), transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._external.aclose()

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._token_provider is not None:
            token = await self._token_provider()
            if token:
                headers["authorization"] = f"Bearer {token}"
        if extra:
            headers.update(extra)
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retries: int = 2,
        ok: tuple[int, ...] = (200, 201, 202),
    ) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                    files=files,
                    data=data,
                    headers=await self._headers(headers),
                    timeout=timeout if timeout is not None else self._timeout,
                )
            except httpx.HTTPError as exc:
                if attempt <= retries and method.upper() == "GET":
                    await asyncio.sleep(min(8, 0.5 * 2**attempt) + random.random() * 0.2)
                    continue
                raise TransportError(
                    f"{method} {self.base_url}{path}: {exc.__class__.__name__}: {exc}"
                ) from exc
            if response.status_code in ok:
                return response
            if response.status_code in RETRYABLE_STATUS and attempt <= retries:
                retry_after = response.headers.get("retry-after")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit() else min(8, 0.5 * 2**attempt)
                )
                log.debug("retrying %s %s after %s (%.1fs)", method, path, response.status_code, delay)
                await asyncio.sleep(delay + random.random() * 0.2)
                continue
            raise error_from_response(response)

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        return (await self.request("GET", path, params=params, **kw)).json()

    async def post_json(self, path: str, body: Any = None, **kw: Any) -> Any:
        response = await self.request("POST", path, json_body=body, **kw)
        if not response.content:
            return None
        return response.json()

    # ── Server-sent events ──

    async def sse(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        last_event_id: str | None = None,
        read_timeout: float = 90,
    ) -> AsyncIterator[SseFrame]:
        """Yield SSE frames. Comment lines (heartbeats) are skipped; the stream ends when the
        server closes it or the caller stops iterating."""
        headers = await self._headers({"accept": "text/event-stream", "cache-control": "no-cache"})
        if last_event_id is not None:
            headers["last-event-id"] = str(last_event_id)
        timeout = httpx.Timeout(read_timeout, connect=15, read=read_timeout, write=15, pool=15)
        try:
            async with self._client.stream(
                "GET", path, params=params, headers=headers, timeout=timeout
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise error_from_response(response)
                event: str | None = None
                data_lines: list[str] = []
                frame_id: str | None = None
                async for raw in response.aiter_lines():
                    line = raw.rstrip("\r")
                    if line == "":
                        if data_lines:
                            yield SseFrame(event=event, data="\n".join(data_lines), id=frame_id)
                        event, data_lines, frame_id = None, [], None
                        continue
                    if line.startswith(":"):
                        continue
                    field, _, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if field == "event":
                        event = value
                    elif field == "data":
                        data_lines.append(value)
                    elif field == "id":
                        frame_id = value
                if data_lines:
                    yield SseFrame(event=event, data="\n".join(data_lines), id=frame_id)
        except httpx.HTTPError as exc:
            raise TransportError(f"SSE {path}: {exc.__class__.__name__}: {exc}") from exc

    # ── Bytes in and out of object storage ──

    async def put_file(self, url: str, path: Path, content_type: str) -> None:
        size = path.stat().st_size

        async def body() -> AsyncIterator[bytes]:
            with path.open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    yield chunk

        try:
            response = await self._external.put(
                url, content=body(), headers={"content-type": content_type, "content-length": str(size)}
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"upload PUT failed: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code >= 300:
            raise HttpError(
                response.status_code, "upload_put_failed", response.text[:300] or "presigned PUT rejected"
            )

    async def download(self, path_or_url: str, dest: Path) -> Path:
        """GET a backend path (bearer) that either streams bytes or 302s to object storage."""
        headers = await self._headers()
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._client.stream(
                "GET", path_or_url, headers=headers, timeout=httpx.Timeout(600)
            ) as first:
                if first.status_code in (301, 302, 303, 307, 308):
                    location = first.headers.get("location")
                    if not location:
                        raise HttpError(
                            first.status_code, "download_redirect_missing", "redirect without location"
                        )
                    await first.aclose()
                    await self._download_external(location, dest)
                    return dest
                if first.status_code != 200:
                    await first.aread()
                    raise error_from_response(first)
                with dest.open("wb") as handle:
                    async for chunk in first.aiter_bytes():
                        handle.write(chunk)
                return dest
        except httpx.HTTPError as exc:
            raise TransportError(f"download failed: {exc.__class__.__name__}: {exc}") from exc

    async def _download_external(self, url: str, dest: Path) -> None:
        async with self._external.stream("GET", url, follow_redirects=True) as response:
            if response.status_code != 200:
                await response.aread()
                raise HttpError(
                    response.status_code, "download_failed", response.text[:300] or "object fetch failed"
                )
            with dest.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    handle.write(chunk)
