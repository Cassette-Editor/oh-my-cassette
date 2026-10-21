"""Host-facing MCP server: mirrors the backend's tools over stdio, adding only the contract's local duties.

The bridge never names a backend tool. It lists what the backend lists (minus the bridge-only upload
tools), forwards calls and progress, prepares and uploads declared local files first, downloads
declared artifacts after, and reports its own failures with `bridge.*` codes. A tool that names its
request in an argument is sent again by the bridge itself when the connection breaks or the service
refuses it, since the backend answers a repeated request id as the first call.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from functools import partial
from typing import Any

import anyio
import httpx
import mcp_types as types
from anyio.abc import TaskStatus
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
from mcp.shared.dispatcher import ProgressFnT
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from oh_my_cassette import __version__
from oh_my_cassette.auth import AuthenticationError, Credentials
from oh_my_cassette.bridge.downloads import Downloads
from oh_my_cassette.bridge.errors import (
    BridgeError,
    backoff_seconds,
    protocol_unsupported,
    rate_limited,
    unauthorized,
    unreachable,
)
from oh_my_cassette.bridge.files import LocalFiles
from oh_my_cassette.bridge.jobs import LocalJobs, preparing_result
from oh_my_cassette.bridge.prepare import FFmpegPreparer, Preparer
from oh_my_cassette.bridge.settings import BridgeSettings
from oh_my_cassette.bridge.upstream import (
    BackendUnavailable,
    ProtocolUnsupported,
    RateLimited,
    Unauthorized,
    Upstream,
)
from oh_my_cassette.contract import (
    BRIDGE_ROLES,
    META_ELAPSED,
    META_HOST,
    META_LOCAL_FILES,
    META_WORKSPACE,
    ContractError,
    bridge_role,
    pointer_get,
    pointer_set,
    request_id_pointer,
)

log = logging.getLogger(__name__)

STATUS_TOOL = "cassette_bridge_status"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
# Request ids the bridge chose for calls whose outcome it could not learn, kept so the host calling again
# with the same arguments picks up that request instead of starting another.
UNSETTLED_REQUESTS = 64

CONNECTED_FALLBACK_INSTRUCTIONS = (
    "Oh My Cassette connects this host to the Cassette video editor. The tools below come from the "
    "Cassette backend; follow their descriptions."
)
OFFLINE_INSTRUCTIONS = """Oh My Cassette connects this host to the Cassette video editor through its MCP endpoint
({url}). The editing tools come from the Cassette backend, which is not available right now: {reason}.
Call {status} to check the connection. The editing tools appear once the backend is reachable; if your
host does not refresh tool lists, restart this MCP server after that."""

STATUS_TOOL_DEF = types.Tool(
    name=STATUS_TOOL,
    title="Cassette connection status",
    description=(
        "Check whether the Cassette backend is reachable and compatible. Waits briefly for a reconnect. "
        "The editing tools are only listed while the backend is connected."
    ),
    input_schema={"type": "object", "properties": {}},
    annotations=types.ToolAnnotations(title="Connection status", read_only_hint=True, open_world_hint=True),
)


class _BridgeServer(Server[Any]):
    """Advertises tool-list changes on every run path (stdio, in-process, tests)."""

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        extensions: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        return super().create_initialization_options(
            notification_options or NotificationOptions(tools_changed=True),
            experimental_capabilities,
            extensions,
        )


class Bridge:
    def __init__(
        self, settings: BridgeSettings, http: httpx.AsyncClient, *, preparer: Preparer | None = None
    ) -> None:
        self.settings = settings
        self.credentials = Credentials(settings.mcp_url, explicit_token=settings.auth_token)
        self.upstream = Upstream(
            settings, credentials=self.credentials, on_tools_changed=self.notify_tools_changed
        )
        self.files = LocalFiles(settings, self.upstream, http, preparer or FFmpegPreparer(settings))
        self.jobs = LocalJobs()
        self.downloads = Downloads(settings, http, credentials=self.credentials)
        self._bus = InMemorySubscriptionBus()
        self._legacy_host_session: Any = None
        self._host_meta: dict[str, Any] | None = {"name": settings.host_name} if settings.host_name else None
        self._unsettled: dict[tuple[str, str], str] = {}

    async def run(self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        """Own the backend connection and the background local work until cancelled.

        Started once the first connection attempt has settled, so the instructions are decided.
        """
        async with anyio.create_task_group() as tg:
            await tg.start(self.jobs.run)
            await tg.start(self.upstream.run)
            task_status.started()

    # ── state ──

    def incompatibility(self) -> str | None:
        return self.upstream.contract.incompatibility(__version__) if self.upstream.connected else None

    @property
    def serving(self) -> bool:
        """True when the backend's tools are being mirrored (connected and compatible)."""
        return self.upstream.connected and self.incompatibility() is None

    def _unauthorized(self) -> BridgeError:
        return (
            unauthorized(self.settings.mcp_url, has_token=bool(self.settings.auth_token))
            if self.settings.auth_token
            else BridgeError(
                "bridge.unauthorized",
                "Sign in with oh-my-cassette login, using the Cassette account of your projects. Tools refresh after authorization; restart the MCP host if it cannot refresh its tool list.",
            )
        )

    def offline_reason(self) -> tuple[str, str]:
        """(bridge error code, reason) while the backend's tools are not being mirrored."""
        if (reason := self.incompatibility()) is not None:
            return "bridge.upgrade_required", reason
        if self.upstream.unauthorized:
            return "bridge.unauthorized", self._unauthorized().message
        if self.upstream.forbidden:
            return "bridge.forbidden", "This account or grant cannot access Cassette (HTTP 403)."
        if self.upstream.protocol_unsupported:
            return "bridge.protocol_unsupported", protocol_unsupported(self.upstream.last_error or "").message
        return "bridge.backend_unreachable", self.upstream.last_error or "not connected yet"

    def instructions(self) -> str:
        if self.serving:
            return self.upstream.instructions or CONNECTED_FALLBACK_INSTRUCTIONS
        code, reason = self.offline_reason()
        return OFFLINE_INSTRUCTIONS.format(
            url=self.settings.mcp_url, reason=f"{code}: {reason}", status=STATUS_TOOL
        )

    def visible_tools(self) -> list[types.Tool]:
        if not self.serving:
            return [STATUS_TOOL_DEF]
        return [
            _host_view(tool) for tool in self.upstream.tools() if bridge_role(tool.meta) not in BRIDGE_ROLES
        ]

    # ── server ──

    def build_server(self) -> Server[Any]:
        return _BridgeServer(
            "oh-my-cassette",
            version=__version__,
            title="Oh My Cassette",
            instructions=self.instructions(),
            website_url="https://github.com/Cassette-Editor/oh-my-cassette",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_subscriptions_listen=ListenHandler(self._bus),
        )

    async def notify_tools_changed(self) -> None:
        if self._legacy_host_session is not None:
            try:
                await self._legacy_host_session.send_tool_list_changed()
            except Exception:
                log.debug("could not notify the host of a tool list change", exc_info=True)
        await self._bus.publish(ToolsListChanged())

    def _remember_host(self, ctx: ServerRequestContext[Any, Any]) -> None:
        if ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            self._legacy_host_session = ctx.session
        info = getattr(ctx.session.client_params, "client_info", None)
        if info is not None:
            self._host_meta = {"name": info.name, "version": info.version}
        elif isinstance((ctx.meta or {}).get(CLIENT_INFO_META_KEY), dict):
            raw = ctx.meta[CLIENT_INFO_META_KEY]  # type: ignore[index]
            self._host_meta = {"name": raw.get("name"), "version": raw.get("version")}

    async def _list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        self._remember_host(ctx)
        return types.ListToolsResult(tools=self.visible_tools())

    async def _call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        started = anyio.current_time()
        self._remember_host(ctx)
        name = params.name
        if name == STATUS_TOOL and not (self.serving and self.upstream.tool(STATUS_TOOL)):
            return await self._status()

        async def progress(value: float, total: float | None, message: str | None) -> None:
            await ctx.session.report_progress(value, total, message)

        try:
            await self.upstream.client()
            if (reason := self.incompatibility()) is not None:
                raise BridgeError("bridge.upgrade_required", f"{reason}; run `uvx oh-my-cassette@latest`")
            tool = self.upstream.tool(name)
            if tool is not None and bridge_role(tool.meta) in BRIDGE_ROLES:
                raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {name}")
            arguments = params.arguments
            call = self.files.plan(tool, arguments) if tool else None
            if call is not None:
                job = self.jobs.attach(call.key, call.progress_files, partial(self.files.run, call))
                window = self.settings.local_wait_sec - (anyio.current_time() - started)
                if not await self.jobs.wait(job, window, progress):
                    return preparing_result(name, job)
                arguments = call.with_refs(self.jobs.settle(call.key, job))
            pointer = _request_id_pointer(tool) if tool else None
            if pointer is None:
                result = await self.upstream.call_tool(
                    name, arguments, meta=self._meta(started), progress=progress
                )
            else:
                result = await self._call_named_request(name, pointer, arguments or {}, started, progress)
            return await self.downloads.apply(result)
        except BridgeError as exc:
            return exc.result()
        except AuthenticationError as exc:
            if exc.status == 401:
                return self._unauthorized().result()
            return BridgeError(
                "bridge.forbidden" if exc.status == 403 else "bridge.auth_unavailable",
                str(exc),
                retryable=exc.status == 503,
            ).result()
        except Unauthorized:
            return self._unauthorized().result()
        except ProtocolUnsupported as exc:
            return protocol_unsupported(str(exc)).result()
        except RateLimited as exc:
            return rate_limited(self.settings.mcp_url, exc.retry_after).result()
        except BackendUnavailable as exc:
            return unreachable(
                self.settings.mcp_url, str(exc), during_call=exc.during_call, retry_after=exc.retry_after
            ).result()

    def _meta(self, started: float) -> dict[str, Any]:
        meta: dict[str, Any] = {
            META_WORKSPACE: {
                "id": self.credentials.workspace_id(self.settings.workspace),
                "name": self.settings.workspace.name,
            },
            META_ELAPSED: round(anyio.current_time() - started, 3),
        }
        if self._host_meta:
            meta[META_HOST] = self._host_meta
        return meta

    async def _call_named_request(
        self, name: str, pointer: str, arguments: dict[str, Any], started: float, progress: ProgressFnT
    ) -> types.CallToolResult:
        """Send a call that names its request until it is answered or the host call's wait runs out.

        The request id is the host's, or one the bridge chooses. A connection that breaks during the call or
        a refusal (HTTP 429/503) is followed by a wait (the service's `Retry-After`, else 1, 2, 4… s), a
        reconnect and the same call with the same id; the backend answers it as the first one.
        """
        found, value = pointer_get(arguments, pointer)
        key: tuple[str, str] | None = None
        if not found or value is None or value == "":
            key = (name, json.dumps(arguments, sort_keys=True, default=str))
            arguments = copy.deepcopy(arguments)
            try:
                pointer_set(arguments, pointer, self._unsettled.pop(key, None) or str(uuid.uuid4()))
            except ContractError as exc:
                raise BridgeError("bridge.contract_violation", f"tool {name}: {exc}") from exc
        attempt, during_call = 0, False
        while True:
            try:
                result = await self.upstream.call_tool(
                    name, arguments, meta=self._meta(started), progress=progress
                )
            except (BackendUnavailable, RateLimited) as exc:
                if isinstance(exc, BackendUnavailable):
                    during_call = during_call or exc.during_call
                    if attempt == 0 and not (exc.during_call or exc.refused):
                        raise  # never connected: nothing was sent
                wait = backoff_seconds(attempt, exc.retry_after)
                if anyio.current_time() - started + wait >= self.settings.local_wait_sec:
                    if key is not None:
                        self._unsettled[key] = pointer_get(arguments, pointer)[1]
                        while len(self._unsettled) > UNSETTLED_REQUESTS:
                            del self._unsettled[next(iter(self._unsettled))]
                    if isinstance(exc, BackendUnavailable) and during_call and not exc.during_call:
                        raise BackendUnavailable(
                            str(exc), during_call=True, retry_after=exc.retry_after
                        ) from exc
                    raise
                log.info("sending %s again in %.0f s: %s", name, wait, exc)
                await anyio.sleep(wait)
                attempt += 1
            else:
                return result

    async def _status(self) -> types.CallToolResult:
        connected = await self.upstream.wait_connected(self.settings.connect_timeout_sec)
        reason = self.incompatibility()
        code, why = self.offline_reason() if not (connected and reason is None) else (None, None)
        contract = self.upstream.contract
        data: dict[str, Any] = {
            "connected": connected,
            "compatible": connected and reason is None,
            "mcp_url": self.settings.mcp_url,
            "bridge_version": __version__,
            "workspace": str(self.settings.workspace),
            "server": self.upstream.server_info if connected else None,
            "protocol_version": self.upstream.protocol_version if connected else None,
            "contract": {
                "declared": contract.declared,
                "version": contract.version,
                "min_bridge_version": contract.min_bridge_version,
            }
            if connected
            else None,
            "tools": len(self.visible_tools()) if connected and reason is None else 0,
            "error": why,
            "error_code": code,
        }
        if code in ("bridge.unauthorized", "bridge.protocol_unsupported"):
            text = f"{code}: {why}."
        elif not connected:
            text = f"Cassette backend at {self.settings.mcp_url} is not reachable: {why}."
        elif reason:
            text = f"Connected to {self.settings.mcp_url}, but {reason}. Upgrade with `uvx oh-my-cassette@latest`."
        else:
            text = (
                f"Connected to {self.settings.mcp_url} ({data['tools']} tools). If they are not listed yet, "
                "restart this MCP server; this host may not refresh tool lists."
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], structured_content=data
        )


def _request_id_pointer(tool: types.Tool) -> str | None:
    try:
        return request_id_pointer(tool.meta)
    except ContractError as exc:
        raise BridgeError("bridge.contract_violation", f"tool {tool.name}: {exc}") from exc


def _host_view(tool: types.Tool) -> types.Tool:
    """A tool that takes local files may answer with the bridge's own `preparing` result, which its
    output schema does not describe, so hosts get it without one."""
    if tool.output_schema is not None and META_LOCAL_FILES in (tool.meta or {}):
        return tool.model_copy(update={"output_schema": None})
    return tool
