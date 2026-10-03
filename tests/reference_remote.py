"""A reference Cassette backend that implements the bridge contract (docs/v3/contract.md).

It is the executable form of the contract: the bridge tests run against it over real HTTP, and
`oh-my-cassette check` must pass on it. `RemoteOptions` switches single contract features off so the
tests can prove the bridge and the checker notice.

Like the Cassette MCP service it serves only the 2026-07-28 protocol and answers a handshake-era
`initialize` with -32022; `handshake_only` turns it into a backend that predates that protocol.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import socket
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import mcp_types as types
import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel
from sse_starlette.sse import AppStatus
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from oh_my_cassette.contract import (
    EXTENSION_ID,
    META_BRIDGE_ROLE,
    META_DOWNLOADS,
    META_LOCAL_FILES,
    ROLE_UPLOAD_BEGIN,
    ROLE_UPLOAD_COMPLETE,
)

MEDIA = ["video/*", "audio/*", "image/*"]
REFERENCE_TOKEN = "reference-token"


def _rendition(
    role: str, long_edge: int, short_edge: int, crf: int, preset: str, bitrate: int
) -> dict[str, Any]:
    return {
        "role": role,
        "container": "mp4",
        "mimeType": "video/mp4",
        "video": {
            "codec": "h264",
            "profile": "high",
            "pixelFormat": "yuv420p",
            "bitDepth": 8,
            "frameRate": 30,
            "maxLongEdge": long_edge,
            "maxShortEdge": short_edge,
            "keyframeIntervalSeconds": 2,
            "crf": crf,
            "preset": preset,
        },
        "audio": {"codec": "aac", "bitrate": bitrate, "sampleRate": 48000, "channels": 2},
    }


# The declaration the Cassette backend generates from its media preparation profile.
PREPARE: dict[str, Any] = {
    "video": {
        "renditions": [
            _rendition("canonical", 1920, 1080, 20, "medium", 192000),
            _rendition("preview", 1280, 720, 23, "fast", 128000),
        ]
    },
    "audio": {
        "accept": ["mp3", "wav", "m4a", "aac", "ogg", "oga", "opus", "flac"],
        "convert": {"container": "flac", "mimeType": "audio/flac", "extension": "flac"},
    },
    "image": {
        "accept": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "avif"],
        "convert": {"container": "jpeg", "mimeType": "image/jpeg", "extension": "jpg", "quality": 92},
    },
}


class ImportResult(BaseModel):
    status: str
    refs: list[str]
    project_id: str = ""


@dataclass
class RemoteOptions:
    declare_contract: bool = True
    contract_version: int = 1
    min_bridge_version: str | None = None
    upload_tools: bool = True
    cross_origin_uploads: bool = False
    bad_pointer: bool = False
    bad_prepare: bool = False
    begin_without_puts: bool = False
    token: str | None = REFERENCE_TOKEN
    handshake_only: bool = False
    instructions: str | None = "Reference Cassette backend: import local media with import_media."


@dataclass
class ReferenceRemote:
    options: RemoteOptions = field(default_factory=RemoteOptions)
    base_url: str = ""
    url: str = ""
    begun: list[dict[str, Any]] = field(default_factory=list)
    uploads: dict[str, dict[str, Any]] = field(default_factory=dict)
    refs_by_sha: dict[str, str] = field(default_factory=dict)
    blobs: dict[str, dict[str, bytes]] = field(default_factory=dict)
    preparations: dict[str, Any] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    put_headers: list[dict[str, str]] = field(default_factory=list)
    request_headers: list[dict[str, str]] = field(default_factory=list)
    _server: uvicorn.Server | None = field(default=None, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.mcp = self._build()

    async def crash(self) -> None:
        """Die like a killed process: drop every open connection and stop listening, no graceful goodbye."""
        assert self._server is not None and self._task is not None
        self._server.should_exit = self._server.force_exit = True
        _abort_connections(self._server)
        await self._task

    def app(self) -> ASGIApp:
        """The MCP app behind the bearer check the contract requires (§1); uploads carry their own token."""
        inner = self.mcp.streamable_http_app()

        async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
                await inner(scope, receive, send)
                return
            headers = dict(scope["headers"])
            token = self.options.token  # read per request, so a test can revoke it mid-session
            if token and headers.get(b"authorization") != f"Bearer {token}".encode():
                await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
            version = headers.get(b"mcp-protocol-version", b"").decode() or None
            modern = version not in (None, *HANDSHAKE_PROTOCOL_VERSIONS)
            if scope["method"] == "POST" and modern == self.options.handshake_only:
                await self._refuse_era(scope, receive, send, version)
                return
            await inner(scope, receive, send)

        return guarded

    async def _refuse_era(self, scope: Scope, receive: Receive, send: Send, version: str | None) -> None:
        """Answer a request from the protocol era this backend does not serve, as such a server does."""
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        request = json.loads(body or b"{}")
        if self.options.handshake_only:
            # A handshake-era server has no session for a request that is not `initialize`.
            error = {"code": types.INVALID_REQUEST, "message": "Bad Request: No valid session ID provided"}
        else:
            requested = (request.get("params") or {}).get("protocolVersion") or version
            error = {
                "code": types.UNSUPPORTED_PROTOCOL_VERSION,
                "message": "Unsupported protocol version",
                "data": {"supported": list(MODERN_PROTOCOL_VERSIONS), "requested": requested},
            }
        reply = {"jsonrpc": "2.0", "id": request.get("id"), "error": error}
        await JSONResponse(reply, status_code=400)(scope, receive, send)

    def _upload_base(self) -> str:
        # `localhost` and `127.0.0.1` are different origins: the bridge must not send its token there.
        return (
            self.base_url.replace("127.0.0.1", "localhost")
            if self.options.cross_origin_uploads
            else self.base_url
        )

    def _record(self, ctx: Context, tool: str, arguments: dict[str, Any]) -> None:
        meta = dict(ctx.request_context.meta or {})
        self.calls.append({"tool": tool, "arguments": arguments, "meta": meta})
        if ctx.headers is not None:
            self.request_headers.append({k.lower(): v for k, v in ctx.headers.items()})

    def _build(self) -> MCPServer:
        opts = self.options
        mcp = MCPServer(name="reference-cassette", version="9.9.9", instructions=opts.instructions)
        lowlevel = mcp._lowlevel_server
        declared: dict[str, Any] = {}
        if opts.declare_contract:
            declared[EXTENSION_ID] = {"version": opts.contract_version}
            if opts.min_bridge_version:
                declared[EXTENSION_ID]["minBridgeVersion"] = opts.min_bridge_version
            lowlevel.extensions.update(declared)

        prepare = copy.deepcopy(PREPARE)
        if opts.bad_prepare:
            prepare["video"]["renditions"][0]["video"]["crf"] = "twenty"

        @mcp.tool(
            name="import_media",
            description="Import local media into the project.",
            meta={
                META_LOCAL_FILES: {
                    "pointers": ["/missing" if opts.bad_pointer else "/paths"],
                    "accept": MEDIA,
                    "prepare": prepare,
                }
            },
        )
        async def import_media(paths: list[str], ctx: Context, project_id: str = "") -> ImportResult:
            self._record(ctx, "import_media", {"paths": paths, "project_id": project_id})
            return ImportResult(status="ok", refs=paths, project_id=project_id)

        @mcp.tool(
            name="set_cover",
            description="Use a local image as the cover.",
            meta={META_LOCAL_FILES: {"pointers": ["/image"], "accept": ["image/*"]}},
        )
        async def set_cover(image: str, ctx: Context) -> dict[str, Any]:
            self._record(ctx, "set_cover", {"image": image})
            return {"status": "ok", "ref": image}

        @mcp.tool(name="slow_turn", description="A long turn that reports progress.")
        async def slow_turn(ctx: Context, steps: int = 3, delay: float = 0.02) -> dict[str, Any]:
            self._record(ctx, "slow_turn", {"steps": steps})
            for step in range(steps):
                await ctx.report_progress(step + 1, steps, f"step {step + 1}")
                await anyio.sleep(delay)
            return {"status": "completed", "steps": steps}

        @mcp.tool(name="silent_turn", description="Works without sending anything for a while.")
        async def silent_turn(ctx: Context, seconds: float = 1.0) -> dict[str, Any]:
            self._record(ctx, "silent_turn", {"seconds": seconds})
            await anyio.sleep(seconds)
            return {"status": "completed"}

        @mcp.tool(name="export_video", description="Render and hand back the file.")
        async def export_video(
            ctx: Context, name: str = "final", corrupt: bool = False
        ) -> types.CallToolResult:
            self._record(ctx, "export_video", {"name": name})
            data = b"FAKE-MP4-" * 4096
            file_id = uuid.uuid4().hex
            self.files[file_id] = data
            url = f"{self.base_url}/files/{file_id}"
            declared = {
                "url": url,
                "fileName": f"{name}.mp4",
                "size": len(data),
                "sha256": "0" * 64 if corrupt else hashlib.sha256(data).hexdigest(),
                "pointer": "/file",
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"exported {name}.mp4")],
                structured_content={"status": "exported", "file": url},
                _meta={META_DOWNLOADS: [declared]},
            )

        @mcp.tool(name="add_late_tool", description="Register another tool and announce the change.")
        async def add_late_tool(ctx: Context) -> dict[str, Any]:
            async def late_tool() -> dict[str, Any]:
                return {"status": "late"}

            mcp.add_tool(late_tool, name="late_tool", description="Appeared after a change.")
            await ctx.notify_tools_changed()
            return {"status": "ok"}

        if opts.upload_tools:

            @mcp.tool(
                name="cassette_upload_begin",
                description="Used by the local oh-my-cassette bridge.",
                meta={META_BRIDGE_ROLE: ROLE_UPLOAD_BEGIN},
            )
            async def upload_begin(
                files: list[dict[str, Any]], tool: str = "", arguments: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                uploads: list[dict[str, Any]] = []
                for item in files:
                    self.begun.append({"tool": tool, "arguments": arguments, **item})
                    if item["sha256"] in self.refs_by_sha:  # content identity: the source's sha256
                        uploads.append(
                            {"clientRef": item["clientRef"], "ref": self.refs_by_sha[item["sha256"]]}
                        )
                        continue
                    upload_id = uuid.uuid4().hex
                    self.uploads[upload_id] = {"file": item, "data": {}}
                    entry: dict[str, Any] = {"clientRef": item["clientRef"], "uploadId": upload_id}
                    if not opts.begin_without_puts:
                        entry["puts"] = [
                            {
                                "role": artifact["role"],
                                "url": f"{self._upload_base()}/uploads/{upload_id}/{artifact['role']}",
                                "headers": {
                                    "Content-Type": artifact["mimeType"],
                                    "X-Upload-Token": f"t-{upload_id}",
                                },
                            }
                            for artifact in item["artifacts"]
                        ]
                    uploads.append(entry)
                return {"uploads": uploads}

            @mcp.tool(
                name="cassette_upload_complete",
                description="Used by the local oh-my-cassette bridge.",
                meta={META_BRIDGE_ROLE: ROLE_UPLOAD_COMPLETE},
            )
            async def upload_complete(uploadIds: list[str]) -> dict[str, Any]:  # noqa: N803 (wire name)
                results: list[dict[str, Any]] = []
                for upload_id in uploadIds:
                    record = self.uploads.get(upload_id)
                    if record is None:
                        results.append(
                            {"uploadId": upload_id, "error": {"code": "unknown_upload", "message": "unknown"}}
                        )
                        continue
                    item, data = record["file"], record["data"]
                    if any(
                        hashlib.sha256(data.get(a["role"], b"")).hexdigest() != a["sha256"]
                        or a["role"] not in data
                        for a in item["artifacts"]
                    ):
                        results.append(
                            {
                                "uploadId": upload_id,
                                "error": {"code": "checksum", "message": "content mismatch"},
                            }
                        )
                        continue
                    ref = f"media:{item['sha256'][:16]}"
                    self.refs_by_sha[item["sha256"]] = ref
                    self.blobs[ref] = dict(data)
                    self.preparations[ref] = item.get("preparation")
                    results.append({"uploadId": upload_id, "ref": ref})
                return {"files": results}

        @mcp.custom_route("/uploads/{upload_id}/{role}", methods=["PUT"])
        async def put_upload(request: Request) -> Response:
            upload_id, role = request.path_params["upload_id"], request.path_params["role"]
            record = self.uploads.get(upload_id)
            self.put_headers.append({k.lower(): v for k, v in request.headers.items()})
            if record is None or request.headers.get("x-upload-token") != f"t-{upload_id}":
                return Response(status_code=403)
            artifact = next((a for a in record["file"]["artifacts"] if a["role"] == role), None)
            # Like a presigned URL, the target is bound to the Content-Type it was issued for.
            if artifact is None or request.headers.get("content-type") != artifact["mimeType"]:
                return Response(status_code=403)
            record["data"][role] = await request.body()
            return Response(status_code=200)

        @mcp.custom_route("/files/{file_id}", methods=["GET"])
        async def get_file(request: Request) -> Response:
            data = self.files.get(request.path_params["file_id"])
            if data is None:
                return Response(status_code=404)
            return Response(data, media_type="video/mp4")

        return mcp


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _abort_connections(server: uvicorn.Server) -> None:
    for connection in list(server.server_state.connections):
        transport = getattr(connection, "transport", None)
        if transport is not None:
            transport.abort()


@asynccontextmanager
async def serve_remote(remote: ReferenceRemote, port: int | None = None):
    """Run the reference backend on a real loopback port for the duration of the block."""
    port = port or free_port()
    remote.base_url = f"http://127.0.0.1:{port}"
    # sse-starlette ends every SSE stream in the process once any uvicorn server it spots is exiting;
    # these tests stop and start several servers in one process, so streams are closed explicitly instead.
    AppStatus.disable_automatic_graceful_drain()
    config = uvicorn.Config(
        remote.app(),
        host="127.0.0.1",
        port=port,
        log_level="critical",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    remote._server, remote._task = server, task
    try:
        for _ in range(250):
            if server.started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError(f"reference backend did not start on port {port}")
        remote.url = f"{remote.base_url}/mcp"
        yield remote.url
    finally:
        if not task.done():
            server.should_exit = True
            _abort_connections(server)  # a bridge may still hold a listen stream open
            await task
