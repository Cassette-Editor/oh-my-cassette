"""Errors the bridge itself reports to the host, and the HTTP waits that come with them."""

from __future__ import annotations

import math
import socket
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
import httpx2
import mcp_types as types

# The bridge's own HTTP client (uploads, downloads, sign-in) and the MCP SDK's each raise their own classes.
_TRANSPORT_ERRORS = (httpx.TransportError, httpx2.TransportError)
_CONNECTS = (httpx.ConnectError, httpx2.ConnectError)
_CONNECT_TIMEOUTS = (httpx.ConnectTimeout, httpx2.ConnectTimeout)
_TIMEOUTS = (httpx.TimeoutException, httpx2.TimeoutException)


def retry_after_seconds(value: str | None) -> float | None:
    """An HTTP `Retry-After` value (seconds or an HTTP date) as seconds from now; None when absent or unreadable."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def connection_failure(exc: BaseException) -> str | None:
    """Why an HTTP request could not reach its host, naming the host, e.g. `cannot resolve the host name
    mcp.example.com`; None when `exc` is not a transport failure of a request."""
    if not isinstance(exc, _TRANSPORT_ERRORS):
        return None
    try:
        url = exc.request.url
    except RuntimeError:  # raised without the request it belongs to
        return None
    host = url.host if url.port is None else f"{url.host}:{url.port}"
    causes: list[BaseException] = []
    cause: BaseException | None = exc
    while cause is not None and len(causes) < 16:
        causes.append(cause)
        cause = cause.__cause__ or cause.__context__
    if any(isinstance(item, socket.gaierror) for item in causes):
        return (
            f"cannot resolve the host name {url.host} (DNS lookup failed); check the address and the network"
        )
    if any(isinstance(item, ConnectionRefusedError) for item in causes):
        return f"{host} refused the connection; nothing is listening there"
    if isinstance(exc, _CONNECT_TIMEOUTS):
        return f"{host} did not accept a connection in time (connect timeout)"
    if isinstance(exc, _TIMEOUTS):
        return f"{host} stopped answering ({type(exc).__name__})"
    text = str(exc).strip()
    detail = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    if isinstance(exc, _CONNECTS):
        return f"cannot connect to {host} ({detail})"
    return f"the connection to {host} failed ({detail})"


def backoff_seconds(attempt: int, retry_after: float | None) -> float:
    """The wait before retry number `attempt` (from 0): what the service asked for, else 1, 2, 4… seconds."""
    return retry_after if retry_after is not None else float(2**attempt)


class BridgeError(Exception):
    def __init__(
        self, code: str, message: str, *, retryable: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after

    def result(self) -> types.CallToolResult:
        error: dict[str, object] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.retry_after is not None:
            error["retry_after_seconds"] = math.ceil(self.retry_after)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{self.code}: {self.message}")],
            structured_content={"error": error},
            is_error=True,
        )


def unauthorized(url: str, *, has_token: bool) -> BridgeError:
    if has_token:
        message = (
            f"{url} rejected the credential (HTTP 401); remove any stale CASSETTE_AUTH_TOKEN override, "
            "run oh-my-cassette login, and restart the MCP host if its tool list does not update"
        )
    else:
        message = f"{url} requires sign-in (HTTP 401); run oh-my-cassette login and restart the MCP host if its tool list does not update"
    return BridgeError("bridge.unauthorized", message)


def protocol_unsupported(message: str) -> BridgeError:
    return BridgeError("bridge.protocol_unsupported", f"{message}; the backend must be upgraded")


def unreachable(url: str, detail: str, *, during_call: bool, retry_after: float | None = None) -> BridgeError:
    if during_call:
        message = (
            f"the connection to {url} broke during the call ({detail}). The backend's work may still be "
            "running; call the tool again with the same arguments to pick it up."
        )
    else:
        message = f"{url}: {detail}"
    return BridgeError("bridge.backend_unreachable", message, retryable=True, retry_after=retry_after)


def rate_limited(url: str, retry_after: float | None) -> BridgeError:
    wait = f"; try again in {math.ceil(retry_after)} s" if retry_after is not None else "; try again shortly"
    return BridgeError(
        "bridge.rate_limited",
        f"{url} is taking too many requests and did not take the call (HTTP 429){wait}",
        retryable=True,
        retry_after=retry_after,
    )
