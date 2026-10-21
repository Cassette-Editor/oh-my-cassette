"""A reference Cassette backend that implements the bridge contract (`oh_my_cassette.contract`).

It is the executable form of the contract: the bridge tests run against it over real HTTP, and
`oh-my-cassette check` must pass on it. `RemoteOptions` switches single contract features off, or
makes the service misbehave the way a real one can, so the tests can prove the bridge and the
checker notice.

Like the Cassette MCP service it serves only the 2026-07-28 protocol and answers a handshake-era
`initialize` with -32022; `handshake_only` turns it into a backend that predates that protocol.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
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
    META_REQUEST_ID,
    ROLE_UPLOAD_BEGIN,
    ROLE_UPLOAD_COMPLETE,
    ROLE_UPLOAD_PARTS,
)

MEDIA = ["video/*", "audio/*", "image/*"]
REFERENCE_TOKEN = "reference-token"
# The rates a source of variable or unknown rate snaps to, in the order the Cassette backend lists them.
SEQUENCE_RATES = [(24, 1), (24000, 1001), (25, 1), (30, 1), (30000, 1001), (50, 1), (60, 1), (60000, 1001)]


def _rendition(
    role: str, long_edge: int, short_edge: int, max_rate: int, crf: int, preset: str, bitrate: int
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
            "maxFrameRate": max_rate,
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
        "variableFrameRate": {
            "rates": [{"num": num, "den": den} for num, den in SEQUENCE_RATES],
            "fallback": {"num": 30, "den": 1},
        },
        "renditions": [
            _rendition("canonical", 2560, 1080, 60, 20, "medium", 192000),
            _rendition("preview", 1280, 720, 30, 23, "fast", 128000),
        ],
    },
    "audio": {
        "accept": ["mp3", "wav", "m4a", "aac", "ogg", "oga", "opus", "flac"],
        "convert": {"container": "flac", "mimeType": "audio/flac", "extension": "flac"},
    },
    "image": {
        "accept": ["jpg", "jpeg", "jfif", "png", "apng", "gif", "webp", "bmp", "avif"],
        "convertOnIntake": ["heic", "heif", "tif", "tiff"],
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
    begin_refusal: dict[str, Any] | None = None  # upload begin answers this typed error instead
    token: str | None = REFERENCE_TOKEN
    handshake_only: bool = False
    instructions: str | None = "Reference Cassette backend: import local media with import_media."
    request_id_pointer: str = "/client_request_id"
    # Uploads: artifacts larger than `part_size` go up in parts, signed `part_batch` at a time.
    part_size: int | None = None
    part_batch: int = 32
    parts_tool: bool = True
    fail_part_once: int | None = None  # that part's first PUT answers 503
    expire_part_once: int | None = None  # that part's first URL answers 403, as an expired signature does
    # Misbehaviour a test switches on mid-session.
    drop_turn_once: bool = False  # the first turn acted on loses its connection before it answers
    refusals: list[tuple[int, int | None]] = field(
        default_factory=list
    )  # next tools/call: (status, Retry-After)
    cut_downloads: int = 0  # the next downloads stop halfway through the body
    ignore_ranges: bool = False  # downloads always answer 200 with the whole file


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
    part_headers: list[dict[str, str]] = field(default_factory=list)
    signed: list[dict[str, Any]] = field(default_factory=list)
    request_headers: list[dict[str, str]] = field(default_factory=list)
    refused: list[int] = field(default_factory=list)
    turns: dict[str, dict[str, Any]] = field(default_factory=dict)
    acted: list[str] = field(default_factory=list)
    download_headers: list[dict[str, str]] = field(default_factory=list)
    _failed_parts: set[tuple[str, str, int, str]] = field(default_factory=set, repr=False)
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
        """The MCP app behind the bearer check the contract requires; uploads carry their own token."""
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
                await self._refuse_era(scope, await _body(receive), send, version)
                return
            if scope["method"] == "POST" and self.options.refusals:
                body = await _body(receive)
                if json.loads(body or b"{}").get("method") == "tools/call":
                    await self._refuse_call(scope, receive, send)
                    return
                receive = _replay(body, receive)
            await inner(scope, receive, send)

        return guarded

    async def _refuse_call(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Turn a call away before it runs, as the Cassette service does when it is busy or draining."""
        status, wait = self.options.refusals.pop(0)
        self.refused.append(status)
        headers = {"Retry-After": str(wait)} if wait is not None else {}
        error = "rate_limited" if status == 429 else "backend_unavailable"
        reply = {"error": error, "message": "the call was not taken"}
        await JSONResponse(reply, status_code=status, headers=headers)(scope, receive, send)

    async def _refuse_era(self, scope: Scope, body: bytes, send: Send, version: str | None) -> None:
        """Answer a request from the protocol era this backend does not serve, as such a server does."""
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
        await JSONResponse(reply, status_code=400)(scope, _replay(b"", None), send)

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

        @mcp.tool(
            name="run_turn",
            description="Act on one message, once per request id.",
            meta={META_REQUEST_ID: opts.request_id_pointer},
        )
        async def run_turn(message: str, ctx: Context, client_request_id: str = "") -> dict[str, Any]:
            self._record(ctx, "run_turn", {"message": message, "client_request_id": client_request_id})
            if client_request_id not in self.turns:
                self.acted.append(client_request_id)
                self.turns[client_request_id] = {
                    "status": "done",
                    "message": message,
                    "turn": len(self.acted),
                }
                if self.options.drop_turn_once:
                    self.options.drop_turn_once = False
                    assert self._server is not None
                    _abort_connections(self._server)  # the work is done; its answer never leaves
                    await anyio.sleep(5)
            return self.turns[client_request_id]

        if opts.upload_tools:

            @mcp.tool(
                name="cassette_upload_begin",
                description="Used by the local oh-my-cassette bridge.",
                meta={META_BRIDGE_ROLE: ROLE_UPLOAD_BEGIN},
            )
            async def upload_begin(
                files: list[dict[str, Any]], tool: str = "", arguments: dict[str, Any] | None = None
            ) -> types.CallToolResult:
                if opts.begin_refusal:
                    return types.CallToolResult(
                        content=[
                            types.TextContent(type="text", text=json.dumps({"error": opts.begin_refusal}))
                        ],
                        structured_content={"error": opts.begin_refusal},
                        is_error=True,
                    )
                uploads: list[dict[str, Any]] = []
                for item in files:
                    self.begun.append({"tool": tool, "arguments": arguments, **item})
                    if item["sha256"] in self.refs_by_sha:  # content identity: the source's sha256
                        uploads.append(
                            {"clientRef": item["clientRef"], "ref": self.refs_by_sha[item["sha256"]]}
                        )
                        continue
                    upload_id = uuid.uuid4().hex
                    record: dict[str, Any] = {"file": item, "data": {}, "plans": {}, "parts": {}, "sigs": {}}
                    self.uploads[upload_id] = record
                    entry: dict[str, Any] = {"clientRef": item["clientRef"], "uploadId": upload_id}
                    if not opts.begin_without_puts:
                        entry["puts"] = []
                        for artifact in item["artifacts"]:
                            put: dict[str, Any] = {
                                "role": artifact["role"],
                                "url": f"{self._upload_base()}/uploads/{upload_id}/{artifact['role']}",
                                "headers": {
                                    "Content-Type": artifact["mimeType"],
                                    "X-Upload-Token": f"t-{upload_id}",
                                },
                            }
                            if opts.part_size and artifact["size"] > opts.part_size:
                                plan = (opts.part_size, math.ceil(artifact["size"] / opts.part_size))
                                record["plans"][artifact["role"]] = plan
                                put["multipart"] = {"partSize": plan[0], "partCount": plan[1]}
                            entry["puts"].append(put)
                    uploads.append(entry)
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=json.dumps({"uploads": uploads}))],
                    structured_content={"uploads": uploads},
                )

            if opts.parts_tool:

                @mcp.tool(
                    name="cassette_upload_parts",
                    description="Used by the local oh-my-cassette bridge.",
                    meta={META_BRIDGE_ROLE: ROLE_UPLOAD_PARTS},
                )
                async def upload_parts(
                    uploadId: str,  # noqa: N803 (wire name)
                    role: str,
                    partNumbers: list[int] | None = None,  # noqa: N803 (wire name)
                ) -> dict[str, Any]:
                    record = self.uploads[uploadId]
                    size, count = record["plans"][role]
                    held = record["parts"].setdefault(role, {})
                    self.signed.append({"role": role, "partNumbers": partNumbers})
                    wanted = [n for n in partNumbers or range(1, count + 1) if n <= count and n not in held]
                    parts = []
                    for number in wanted[: opts.part_batch]:
                        sig = uuid.uuid4().hex[:12]
                        record["sigs"].setdefault((role, number), set()).add(sig)
                        url = f"{self._upload_base()}/parts/{uploadId}/{role}/{number}?sig={sig}"
                        parts.append({"partNumber": number, "url": url})
                    return {
                        "partSize": size,
                        "partCount": count,
                        "completed": sorted(held),
                        "parts": parts,
                        "expiresAt": "2099-01-01T00:00:00.000Z",
                    }

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
                    for role, (_, count) in record["plans"].items():  # finish the uploads sent in parts
                        held = record["parts"].get(role, {})
                        if set(held) == set(range(1, count + 1)):
                            data[role] = b"".join(held[n] for n in range(1, count + 1))
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

        @mcp.custom_route("/parts/{upload_id}/{role}/{number}", methods=["PUT"])
        async def put_part(request: Request) -> Response:
            upload_id, role = request.path_params["upload_id"], request.path_params["role"]
            number, sig = int(request.path_params["number"]), request.query_params.get("sig", "")
            self.part_headers.append({k.lower(): v for k, v in request.headers.items()})
            record = self.uploads.get(upload_id)
            if record is None or sig not in record["sigs"].get((role, number), set()):
                return Response(status_code=403)
            body = await request.body()
            once = {"expired": self.options.expire_part_once, "failed": self.options.fail_part_once}
            for kind, status in (("expired", 403), ("failed", 503)):
                key = (upload_id, role, number, kind)
                if once[kind] == number and key not in self._failed_parts:
                    self._failed_parts.add(key)
                    return Response(status_code=status, headers={"Retry-After": "0"})
            size, count = record["plans"][role]
            artifact = next(a for a in record["file"]["artifacts"] if a["role"] == role)
            if len(body) != min(size, artifact["size"] - (number - 1) * size):
                return Response(status_code=400)
            record["parts"].setdefault(role, {})[number] = body
            return Response(status_code=200, headers={"ETag": f'"{hashlib.md5(body).hexdigest()}"'})

        @mcp.custom_route("/files/{file_id}", methods=["GET"])
        async def get_file(request: Request) -> Response:
            """Ranged like the Cassette download proxy: `Range: bytes=N-`, `If-Range` against a strong ETag."""
            data = self.files.get(request.path_params["file_id"])
            self.download_headers.append({k.lower(): v for k, v in request.headers.items()})
            if data is None:
                return Response(status_code=404)
            etag = f'"{hashlib.sha256(data).hexdigest()[:16]}"'
            headers = {"ETag": etag, "Accept-Ranges": "bytes"}
            start, status = 0, 200
            wanted = re.fullmatch(r"bytes=(\d+)-", request.headers.get("range", ""))
            if wanted and not self.options.ignore_ranges and request.headers.get("if-range", etag) == etag:
                start = int(wanted.group(1))
                if start >= len(data):
                    return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
                status = 206
                headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
            if self.options.cut_downloads:
                self.options.cut_downloads -= 1
                return _CutResponse(data[start:], status, headers)
            return Response(data[start:], status_code=status, headers=headers, media_type="video/mp4")

        return mcp


class _CutResponse:
    """A response that declares its whole length and stops halfway, like a connection dropped mid-download."""

    def __init__(self, body: bytes, status: int, headers: dict[str, str]) -> None:
        self.body, self.status, self.headers = body, status, headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = {**self.headers, "Content-Length": str(len(self.body)), "Content-Type": "video/mp4"}
        raw = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
        await send({"type": "http.response.start", "status": self.status, "headers": raw})
        await send(
            {"type": "http.response.body", "body": self.body[: len(self.body) // 2], "more_body": True}
        )
        raise ConnectionAbortedError("the download was cut short")


async def _body(receive: Receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            return body


def _replay(body: bytes, receive: Receive | None) -> Receive:
    """A `receive` that hands out an already read request body once, then waits on the real one."""
    sent = False

    async def replay() -> Any:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if receive is None:
            return {"type": "http.disconnect"}
        return await receive()

    return replay


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
