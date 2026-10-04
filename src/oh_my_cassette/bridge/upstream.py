"""The connection to the backend's MCP endpoint: discovery, tool cache, reconnect and list changes.

One long-lived task (`run`) owns the connection so its task groups are entered and exited by the
same task; request handlers only borrow the connected client. When a call finds the connection
broken, the owner tears it down and reconnects with backoff (backend deploys restart the server).
A host request that finds no connection cuts the backoff short, so a backend that came back is
picked up on the next call rather than after the longest backoff.

Calls have no deadline of their own: the backend bounds its waits and keeps the response stream
alive with SSE comments, so the HTTP client's idle read timeout is what detects a dead connection.

The bridge speaks only the 2026-07-28 protocol towards the backend (contract §1): leaving a call
closes its response stream, which is how the backend learns to stop waiting for it. An endpoint that
answers only the handshake-era protocol is refused.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

import anyio
import httpx2
import mcp_types as types
from anyio.abc import TaskStatus
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.dispatcher import ProgressFnT
from mcp.shared.exceptions import MCPError
from mcp_types.version import LATEST_MODERN_VERSION, MODERN_PROTOCOL_VERSIONS

from oh_my_cassette import __version__
from oh_my_cassette.auth import AuthenticationError, Credentials
from oh_my_cassette.bridge.settings import BridgeSettings
from oh_my_cassette.contract import ServerContract, bridge_role, server_contract

log = logging.getLogger(__name__)

MAX_SSE_EVENT_SIZE = 32 * 1024 * 1024
GATEWAY_STATUSES = frozenset({502, 503, 504})

TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx2.HTTPError,
    OSError,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
)


class BackendUnavailable(Exception):
    """The backend's MCP endpoint cannot be reached, or the connection broke while a call was out."""

    def __init__(self, message: str, *, during_call: bool = False) -> None:
        super().__init__(message)
        self.during_call = during_call


class Unauthorized(Exception):
    """The backend's MCP endpoint answered HTTP 401: the token is missing or not accepted."""


class ProtocolUnsupported(Exception):
    """The backend's MCP endpoint answers only a handshake-era protocol version."""


def protocol_refusal(exc: BaseException) -> ProtocolUnsupported | None:
    """The refusal `require_modern` raised, also when the client's task groups wrapped it."""
    if isinstance(exc, ProtocolUnsupported):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        return next(filter(None, map(protocol_refusal, exc.exceptions)), None)
    return None


def require_modern(client: Client, url: str) -> None:
    """Refuse a connection that negotiated a handshake-era version (the SDK falls back to one)."""
    if client.protocol_version not in MODERN_PROTOCOL_VERSIONS:
        raise ProtocolUnsupported(
            f"{url} speaks only MCP {client.protocol_version}; "
            f"oh-my-cassette needs MCP {LATEST_MODERN_VERSION} (Streamable HTTP)"
        )


def describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(describe(inner) for inner in exc.exceptions)
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def authentication_failure(error: BaseException) -> AuthenticationError | None:
    if isinstance(error, AuthenticationError):
        return error
    if isinstance(error, BaseExceptionGroup):
        return next(filter(None, (authentication_failure(item) for item in error.exceptions)), None)
    return None


class Upstream:
    def __init__(
        self,
        settings: BridgeSettings,
        *,
        credentials: Credentials | None = None,
        on_tools_changed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.credentials = credentials or Credentials(settings.mcp_url, explicit_token=settings.auth_token)
        self._on_tools_changed = on_tools_changed
        self._client: Client | None = None
        self._broken = anyio.Event()
        self._wake = anyio.Event()
        self._attempting = False
        self._attempts = 0
        self._attempt_done = anyio.Event()
        self._tools_dirty = anyio.Event()
        self._tools: list[types.Tool] = []
        self.contract = ServerContract(declared=False)
        self.instructions: str | None = None
        self.server_info: dict[str, Any] | None = None
        self.protocol_version: str | None = None
        self.last_error: str | None = None
        self.unauthorized = False
        self.forbidden = False
        self.protocol_unsupported = False
        # Counts of HTTP answers the MCP transport turns into generic errors; compared around a call.
        self._rejections = 0
        self._gateway_errors = 0

    # ── connection owner ──

    def _http_client(self) -> httpx2.AsyncClient:
        headers = {"User-Agent": f"oh-my-cassette/{__version__}"}
        timeout = httpx2.Timeout(self.settings.read_timeout_sec, connect=self.settings.connect_timeout_sec)
        http = create_mcp_http_client(headers=headers, timeout=timeout)
        http.event_hooks = {"request": [self._authorize_request], "response": [self._on_response]}
        return http

    async def _authorize_request(self, request: httpx2.Request) -> None:
        request.headers.update(await self.credentials.headers(str(request.url)))

    async def _on_response(self, response: httpx2.Response) -> None:
        if response.status_code == 401:
            self._rejections += 1
        elif response.status_code == 403:
            raise AuthenticationError("This account or grant cannot access the selected target", 403)
        elif response.status_code in GATEWAY_STATUSES:
            self._gateway_errors += 1

    async def run(self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        """Connect, serve until the connection breaks, reconnect with backoff; forever.

        `task_status.started()` fires after the first attempt either way, so the bridge can decide
        its instructions before it starts serving the host.
        """
        started = not self.credentials.configured
        if started:
            self.unauthorized = True
            self.last_error = "Not signed in"
            task_status.started()
        delay = 1.0
        while True:
            self._wake = anyio.Event()
            self._attempting = True
            rejections = self._rejections
            try:
                async with AsyncExitStack() as stack:
                    http = await stack.enter_async_context(self._http_client())
                    client = await stack.enter_async_context(
                        Client(
                            streamable_http_client(
                                self.settings.mcp_url, http_client=http, max_sse_event_size=MAX_SSE_EVENT_SIZE
                            ),
                            mode="auto",
                            message_handler=self._on_message,
                            client_info=types.Implementation(name="oh-my-cassette", version=__version__),
                            cache=None,
                        )
                    )
                    require_modern(client, self.settings.mcp_url)
                    await self._adopt(client)
                    delay = 1.0
                    if not started:
                        started = True
                        task_status.started()
                    else:
                        await self._notify_tools_changed()
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(self._refresh_loop, client)
                        tools = client.server_capabilities.tools
                        if tools and tools.list_changed:
                            tg.start_soon(self._listen, client)
                        await self._broken.wait()
                        tg.cancel_scope.cancel()
            except Exception as exc:
                auth_error = authentication_failure(exc)
                self.unauthorized = self._rejections > rejections or (
                    auth_error is not None and auth_error.status == 401
                )
                self.forbidden = auth_error is not None and auth_error.status == 403
                refusal = protocol_refusal(exc)
                self.protocol_unsupported = refusal is not None
                if self.unauthorized:
                    self.last_error = str(auth_error) if auth_error else "HTTP 401 Unauthorized"
                else:
                    self.last_error = str(refusal) if refusal else describe(exc)
                log.warning(
                    "Cassette MCP endpoint %s unavailable: %s", self.settings.mcp_url, self.last_error
                )
            finally:
                self._client = None
                self._broken = anyio.Event()
                self._end_attempt()
            if not started:
                started = True
                task_status.started()
            with anyio.move_on_after(delay):
                await self._wake.wait()
            delay = min(delay * 2, 30.0)

    def _end_attempt(self) -> None:
        if self._attempting:
            self._attempting = False
            self._attempts += 1
            self._attempt_done.set()
            self._attempt_done = anyio.Event()

    async def _adopt(self, client: Client) -> None:
        caps = client.server_capabilities
        self.contract = server_contract(caps.extensions)
        self.instructions = client.instructions
        info = client.server_info
        self.server_info = {"name": info.name, "version": info.version} if info else None
        self.protocol_version = client.protocol_version
        self._tools = await self._fetch_tools(client)
        self.last_error = None
        self.unauthorized = False
        self.forbidden = False
        self.protocol_unsupported = False
        self._client = client
        self._end_attempt()
        log.info("connected to %s (%s)", self.settings.mcp_url, self.protocol_version)

    @staticmethod
    async def _fetch_tools(client: Client) -> list[types.Tool]:
        tools: list[types.Tool] = []
        cursor: str | None = None
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools

    async def _on_message(self, message: Any) -> None:
        # Runs inside the client's receive loop. Tool-list changes arrive on the listen stream.
        if isinstance(message, Exception):
            log.debug("upstream transport event: %s", describe(message))

    async def _listen(self, client: Client) -> None:
        try:
            async with client.listen(tools_list_changed=True) as subscription:
                async for _ in subscription:
                    self._tools_dirty.set()
        except Exception as exc:  # a dropped listen stream means the connection is suspect
            log.info("upstream listen stream ended: %s", describe(exc))
            self._broken.set()

    async def _refresh_loop(self, client: Client) -> None:
        while True:
            await self._tools_dirty.wait()
            self._tools_dirty = anyio.Event()
            try:
                self._tools = await self._fetch_tools(client)
            except Exception as exc:
                self.last_error = describe(exc)
                self._broken.set()
                return
            await self._notify_tools_changed()

    async def _notify_tools_changed(self) -> None:
        if self._on_tools_changed is None:
            return
        try:
            await self._on_tools_changed()
        except Exception:  # notifying the host must never take the connection down
            log.debug("tools-changed notification failed", exc_info=True)

    # ── used by request handlers ──

    @property
    def connected(self) -> bool:
        return self._client is not None

    def mark_broken(self) -> None:
        self._broken.set()

    async def wait_connected(self, timeout: float) -> bool:
        """Connected now, or after one fresh connection attempt (started at once), within `timeout`."""
        if self._client is not None:
            return True
        # An attempt already under way may have started before the backend came back: wait for the next.
        target = self._attempts + (2 if self._attempting else 1)
        self._wake.set()
        with anyio.move_on_after(timeout):
            while self._client is None and self._attempts < target:
                await self._attempt_done.wait()
        return self._client is not None

    async def client(self) -> Client:
        if not await self.wait_connected(self.settings.connect_timeout_sec):
            if self.forbidden:
                raise AuthenticationError("This account or grant cannot access the selected target", 403)
            if self.unauthorized:
                raise Unauthorized(self.last_error or "HTTP 401 Unauthorized")
            if self.protocol_unsupported and self.last_error:
                raise ProtocolUnsupported(self.last_error)
            raise BackendUnavailable(self.last_error or f"cannot reach {self.settings.mcp_url}")
        assert self._client is not None
        return self._client

    def tools(self) -> list[types.Tool]:
        return list(self._tools)

    def tool(self, name: str) -> types.Tool | None:
        return next((tool for tool in self._tools if tool.name == name), None)

    def tool_for_role(self, role: str) -> types.Tool | None:
        return next((tool for tool in self._tools if bridge_role(tool.meta) == role), None)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        meta: dict[str, Any] | None = None,
        progress: ProgressFnT | None = None,
    ) -> types.CallToolResult:
        client = await self.client()
        rejections, gateway_errors = self._rejections, self._gateway_errors
        try:
            return await client.call_tool(
                name,
                arguments,
                progress_callback=progress,
                meta=meta,  # type: ignore[arg-type]
            )
        except MCPError as exc:
            if self._rejections > rejections:
                self.mark_broken()
                raise Unauthorized("HTTP 401 Unauthorized") from exc
            if exc.code in (types.CONNECTION_CLOSED, types.REQUEST_TIMEOUT) or (
                self._gateway_errors > gateway_errors
            ):
                self.mark_broken()
                raise BackendUnavailable(exc.message, during_call=True) from exc
            raise
        except TRANSPORT_ERRORS as exc:
            self.mark_broken()
            raise BackendUnavailable(describe(exc), during_call=True) from exc
        except AuthenticationError:
            self.mark_broken()
            raise
