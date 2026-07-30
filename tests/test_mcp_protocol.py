from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from cassette import register
from mcp_plugin.server import mcp


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "cassette_ingest_media",
    "cassette_list_assets",
    "cassette_make_prompt",
    "cassette_match_bgm",
    "cassette_match_exact_bgm",
    "jamendo_music_matcher",
    "cassette_answer_question",
    "cassette_run_job",
    "cassette_job_status",
    "cassette_review_completion",
    "cassette_cancel_job",
    "cassette_timeline",
    "cassette_edit",
    "cassette_config",
    "cassette_login",
}


def _server_parameters(environment: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_plugin.server"],
        cwd=str(ROOT),
        env=environment,
    )


def _launcher_parameters(environment: dict[str, str], project: Path) -> StdioServerParameters:
    launcher_environment = dict(environment)
    launcher_environment.update(
        {
            "CASSETTE_MCP_SKIP_BOOTSTRAP": "1",
            "CASSETTE_MCP_PYTHON": sys.executable,
        }
    )
    return StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "scripts" / "run_local_mcp.py")],
        cwd=str(project),
        env=launcher_environment,
    )


def _environment(tmp_path: Path, project: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "CASSETTE_AUTH_EMAIL",
        "CASSETTE_AUTH_ACCOUNT",
        "CASSETTE_EMAIL",
        "CASSETTE_AUTH_PASSWORD",
        "CASSETTE_PASSWORD",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "CASSETTE_CONFIG_HOME": str(tmp_path / "config"),
            "CASSETTE_DATA_HOME": str(tmp_path / "data"),
            "CASSETTE_PROJECT_ROOT": str(project),
            "CASSETTE_RUNTIME_ADAPTER": "mcp",
            "CASSETTE_TRANSPORT": "api",
        }
    )
    return environment


def _speak_raw(environment: dict[str, str], requests: list[dict], timeout: float = 30.0) -> dict[int, dict]:
    """Exchange raw JSON-RPC with the real server process, bypassing ClientSession.

    ClientSession can only ever send the revision the installed SDK was built for, so
    pinning the negotiated ceiling -- and describing what a newer-revision client meets --
    has to happen on the wire.  Replies are collected off a reader thread so a request the
    server never answers fails on the timeout instead of blocking the suite forever.
    """
    process = subprocess.Popen(
        [sys.executable, "-m", "mcp_plugin.server"],
        cwd=str(ROOT),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True).start()
    replies: dict[int, dict] = {}
    try:
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        outstanding = {request["id"] for request in requests if "id" in request}
        deadline = time.time() + timeout
        while outstanding and time.time() < deadline:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:  # stdout is protocol-only, but never trust it blindly
                continue
            identifier = message.get("id")
            if identifier in outstanding:
                replies[identifier] = message
                outstanding.discard(identifier)
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.terminate()
        process.wait(timeout=15)
    return replies


def _initialize_request(protocol_version: str, identifier: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "era-probe", "version": "1"},
        },
    }


@pytest.mark.parametrize("requested", ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"])
def test_protocol_negotiation_answers_every_revision_the_runtime_supports(tmp_path, requested):
    # The ceiling is a shipped artifact of the pinned SDK, not a constant: while the lock
    # sat on mcp 1.12.4 this server answered a 2025-11-25 client with 2025-06-18 and no
    # host ever said so out loud. Asserting the echo is what makes a silent downgrade fail.
    project = tmp_path / "project"
    project.mkdir()
    reply = _speak_raw(_environment(tmp_path, project), [_initialize_request(requested)]).get(1)
    assert reply is not None, f"the server never answered an initialize for {requested}"
    assert reply["result"]["protocolVersion"] == requested


def test_a_newer_revision_client_is_downgraded_rather_than_refused(tmp_path):
    # A 2026-07-28 client that still probes with initialize for backward compatibility must
    # land on the ceiling this runtime supports, not an error -- that downgrade is the only
    # reason Codex can adopt rmcp 3.0.0 before this plugin migrates to the v2 SDK.
    project = tmp_path / "project"
    project.mkdir()
    reply = _speak_raw(_environment(tmp_path, project), [_initialize_request("2026-07-28")]).get(1)
    assert reply is not None, "the server never answered a newer-revision initialize"
    assert reply["result"]["protocolVersion"] == types.LATEST_PROTOCOL_VERSION


def test_a_stateless_2026_request_meets_a_structured_error_not_a_hang(tmp_path):
    # 2026-07-28 removes the initialize handshake and carries the protocol version in _meta
    # instead. Until this runtime moves to the v2 SDK it cannot serve that shape -- what it
    # MUST do is refuse in JSON-RPC rather than hang a host that opened with it, because a
    # stdio host has no timeout of its own to fall back on.
    # When the v2 migration lands this assertion flips to a real tools/list result.
    project = tmp_path / "project"
    project.mkdir()
    stateless = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    reply = _speak_raw(_environment(tmp_path, project), [stateless], timeout=20.0).get(7)
    assert reply is not None, "a stateless 2026-07-28 request left the host waiting forever"
    assert reply["error"]["code"] == -32602


def test_mcp_lists_exactly_the_hermes_tools_with_flat_structured_schemas():
    class Context:
        def __init__(self):
            self.tools = []

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_command(self, *_args, **_kwargs):
            pass

        def register_hook(self, *_args, **_kwargs):
            pass

        def register_skill(self, *_args, **_kwargs):
            pass

    hermes = Context()
    register(hermes)

    async def inspect():
        listed = await mcp.list_tools()
        assert {tool.name for tool in listed} == {tool["name"] for tool in hermes.tools} == EXPECTED_TOOLS
        by_name = {tool.name: tool for tool in listed}
        assert "request" not in by_name["cassette_run_job"].inputSchema["properties"]
        # The call IS the wait: it returns on a terminal phase and streams progress meanwhile,
        # so the host makes one call per turn instead of a cassette_job_status poll loop.
        assert by_name["cassette_run_job"].inputSchema["properties"]["wait"]["default"] is True
        assert set(by_name["cassette_config"].inputSchema["properties"]["thinking_level"]["anyOf"][0]["enum"]) == {
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        }
        assert "wait_for_change_sec" in by_name["cassette_job_status"].inputSchema["properties"]
        assert {"job_id", "response"} <= set(by_name["cassette_answer_question"].inputSchema["properties"])
        assert set(by_name["cassette_ingest_media"].inputSchema["properties"]["media_type"]["anyOf"][0]["enum"]) == {
            "video",
            "image",
            "audio",
            "file",
            "unknown",
        }
        assert set(by_name["cassette_review_completion"].inputSchema["properties"]["decision"]["enum"]) == {
            "export",
            "continue",
            "needs_user",
            "failed",
        }
        assert all(tool.outputSchema for tool in listed)

    asyncio.run(inspect())


def test_manifest_launcher_initializes_real_stdio_server(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    environment = _environment(tmp_path, project)

    async def exercise():
        async with stdio_client(_launcher_parameters(environment, project)) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "cassette"
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS

    asyncio.run(exercise())


def test_real_stdio_process_initializes_and_calls_every_tool(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    media = project / "sample.mp4"
    media.write_bytes((ROOT / "tests" / "fixtures" / "sample.mp4").read_bytes())
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    escaped = project / "escaped.mp4"
    escaped.symlink_to(outside)
    environment = _environment(tmp_path, project)

    async def exercise():
        async with stdio_client(_server_parameters(environment)) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "cassette"
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS

                ingest = await session.call_tool("cassette_ingest_media", {"source_path": str(media)})
                assert ingest.structuredContent["ok"] is True
                session_id = ingest.structuredContent["session_id"]
                assert session_id.startswith("agent-session-")
                rejected = await session.call_tool(
                    "cassette_ingest_media", {"source_path": str(escaped), "session_id": session_id}
                )
                assert rejected.structuredContent["error"]["code"] == "source_path_not_allowed"
                invalid = await session.call_tool(
                    "cassette_ingest_media",
                    {"source_path": str(media), "media_type": "document"},
                )
                assert invalid.structuredContent["ok"] is False
                assert invalid.structuredContent["error"]["code"] == "validation_error"
                serialized_invalid = json.dumps(invalid.structuredContent)
                assert "document" not in serialized_invalid

                calls = {
                    "cassette_list_assets": {"session_id": session_id},
                    "cassette_make_prompt": {"instruction": "make it concise", "session_id": session_id},
                    "cassette_match_bgm": {"session_id": session_id, "instruction": "", "search_queries": ["calm"]},
                    "cassette_match_exact_bgm": {"session_id": session_id, "instruction": "edit", "title": ""},
                    "jamendo_music_matcher": {"userQuery": "", "searchTerms": []},
                    "cassette_answer_question": {"question": "Should Cassette continue?"},
                    "cassette_run_job": {"prompt": "edit", "session_id": session_id},
                    "cassette_job_status": {"job_id": "missing"},
                    "cassette_review_completion": {
                        "job_id": "missing",
                        "decision": "export",
                        "reason": "test",
                    },
                    "cassette_cancel_job": {"job_id": "missing"},
                    "cassette_timeline": {"session_id": session_id},
                    "cassette_edit": {
                        "session_id": session_id,
                        "tool_name": "timeline_trim",
                        "input": {"clipId": "c1"},
                    },
                    "cassette_config": {"session_id": session_id, "model": "GPT-5.4 Mini"},
                    # The one login mode that issues no HTTP request at all, so this offline
                    # sweep can cover the tool without reaching the real Cassette API.
                    "cassette_login": {"email": "person@example.test", "request_new_password": True},
                }
                seen = {"cassette_ingest_media"}
                results = {}
                for name, arguments in calls.items():
                    results[name] = await session.call_tool(name, arguments)
                    seen.add(name)
                assert seen == EXPECTED_TOOLS
                assert results["cassette_list_assets"].structuredContent["ok"] is True
                assert results["cassette_list_assets"].structuredContent["phase"] == "guided_choices"
                assert results["cassette_make_prompt"].structuredContent["ok"] is True
                assert results["cassette_answer_question"].structuredContent["ok"] is True
                assert results["cassette_run_job"].structuredContent["error"]["code"] == "auth_required"
                assert results["cassette_config"].structuredContent["ok"] is True
                assert results["cassette_config"].structuredContent["data"]["model"] == "GPT-5.4 Mini"
                assert results["cassette_login"].structuredContent["error"]["code"] == "auth_confirm_required"
                command = results["cassette_run_job"].structuredContent["error"]["details"]["setup_command"]
                assert command.endswith("scripts/setup_local_mcp.py")

    asyncio.run(exercise())


def _write_job(path: Path, job: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".job.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(job, handle)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def test_protocol_restart_long_poll_and_resource_link(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    environment = _environment(tmp_path, project)
    data = Path(environment["CASSETTE_DATA_HOME"]) / "cassette"
    job_id = "cassette_20260716_010203_abc123"
    session_id = "handoff-session"
    output = data / "exports" / job_id / "edited.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"validated-video")
    job_path = data / "jobs" / f"{job_id}.json"
    job = {
        "job_id": job_id,
        "cassette_session_id": session_id,
        "session_hash": "hash",
        "status": "running",
        "created_at": "2026-07-16T00:00:00Z",
        "updated_at": "2026-07-16T00:00:00Z",
        "outputs": [],
        "questions": [],
        "errors": [],
        "quality": {},
    }
    _write_job(job_path, job)

    async def first_process():
        async with stdio_client(_server_parameters(environment)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def complete_job():
                    await asyncio.sleep(0.3)
                    completed = {
                        **job,
                        "status": "succeeded",
                        "updated_at": "2026-07-16T00:00:01Z",
                        "outputs": [{"local_path": str(output), "kind": "video"}],
                        "quality": {"export_completed": True},
                    }
                    _write_job(job_path, completed)

                task = asyncio.create_task(complete_job())
                started = time.monotonic()
                result = await session.call_tool(
                    "cassette_job_status",
                    {"job_id": job_id, "wait_for_change_sec": 2},
                )
                await task
                assert time.monotonic() - started < 1.8
                assert result.structuredContent["phase"] == "exported"
                artifact = result.structuredContent["artifacts"][0]
                assert artifact["path"] == str(output.resolve())
                assert artifact["uri"] == output.resolve().as_uri()
                assert artifact["size"] == len(b"validated-video")
                assert any(isinstance(block, types.ResourceLink) for block in result.content)

    async def restarted_process():
        async with stdio_client(_server_parameters(environment)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("cassette_job_status", {"job_id": job_id})
                assert result.structuredContent["phase"] == "exported"
                assert result.structuredContent["job_id"] == job_id
                assert result.structuredContent["session_id"] == session_id

    asyncio.run(first_process())
    asyncio.run(restarted_process())


def test_protocol_successfully_reviews_and_cancels_persisted_jobs(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    environment = _environment(tmp_path, project)
    config = Path(environment["CASSETTE_CONFIG_HOME"])
    config.mkdir(mode=0o700)
    credentials = config / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "email": "protocol@example.test",
                "password": "protocol-private-password",
            }
        ),
        encoding="utf-8",
    )
    credentials.chmod(0o600)
    jobs_dir = Path(environment["CASSETTE_DATA_HOME"]) / "cassette" / "jobs"
    review_id = "cassette_20260716_010203_abc124"
    cancel_id = "cassette_20260716_010203_abc125"
    common = {
        "cassette_session_id": "protocol-state-session",
        "session_hash": "hash",
        "created_at": "2026-07-16T00:00:00Z",
        "updated_at": "2026-07-16T00:00:00Z",
        "outputs": [],
        "questions": [],
        "errors": [],
    }
    _write_job(
        jobs_dir / f"{review_id}.json",
        {
            **common,
            "job_id": review_id,
            "status": "needs_user",
            "quality": {"completion_review_required": True},
        },
    )
    _write_job(
        jobs_dir / f"{cancel_id}.json",
        {**common, "job_id": cancel_id, "status": "running", "quality": {}},
    )

    async def exercise():
        async with stdio_client(_server_parameters(environment)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                reviewed = await session.call_tool(
                    "cassette_review_completion",
                    {
                        "job_id": review_id,
                        "decision": "failed",
                        "reason": "Deterministic protocol review found the edit incomplete.",
                    },
                )
                assert reviewed.structuredContent["ok"] is True
                assert reviewed.structuredContent["phase"] == "failed"
                assert "Hermes" not in json.dumps(reviewed.structuredContent)

                cancelled = await session.call_tool("cassette_cancel_job", {"job_id": cancel_id})
                assert cancelled.structuredContent["ok"] is True
                assert cancelled.structuredContent["data"]["status"] == "cancel_requested"
                assert cancelled.structuredContent["job_id"] == cancel_id

    asyncio.run(exercise())


class _ResumeProtocolApi(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    @property
    def record(self):
        return self.server.record  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _json(self, status: int, value: dict):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._body()
        if path == "/api/agent-auth/verify":
            return self._json(200, {"session": {"access_token": "ephemeral"}, "isFullUser": True})
        if path == "/api/langgraph/threads":
            return self._json(200, {"thread_id": "protocol-thread"})
        if path == "/api/langgraph/threads/protocol-thread/runs":
            if body.get("command"):
                self.record["response"] = body["command"]["resume"]
                return self._json(200, {"run_id": "protocol-resumed"})
            return self._json(200, {"run_id": "protocol-initial"})
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.endswith("/runs/protocol-initial"):
            return self._json(200, {"status": "interrupted"})
        if path.endswith("/runs/protocol-resumed"):
            return self._json(200, {"status": "success"})
        if path == "/api/langgraph/threads/protocol-thread/state":
            if self.record.get("response"):
                return self._json(
                    200,
                    {
                        "values": {
                            "messages": [{"type": "assistant", "content": "The edit is complete and ready for review."}]
                        },
                        "tasks": [],
                    },
                )
            return self._json(
                200,
                {
                    "values": {},
                    "tasks": [
                        {
                            "interrupts": [
                                {
                                    "id": "protocol-ask",
                                    "value": {
                                        "type": "ask_user",
                                        "prompt": "You must choose a title color.",
                                    },
                                }
                            ]
                        }
                    ],
                },
            )
        return self._json(404, {"error": "not found"})


def test_real_protocol_resumes_api_job_after_mcp_host_restart(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ResumeProtocolApi)
    server.record = {"response": None}  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _, port = server.server_address
    project = tmp_path / "project"
    project.mkdir()
    environment = _environment(tmp_path, project)
    environment.update(
        {
            "CASSETTE_API_URL": f"http://127.0.0.1:{port}",
            "CASSETTE_AUTH_EMAIL": "acceptance@example.test",
            "CASSETTE_AUTH_PASSWORD": "ephemeral-only",
            "CASSETTE_API_AUTO_EXPORT": "0",
            "CASSETTE_MIN_BROWSER_TIMEOUT_SEC": "0",
        }
    )

    async def initial_host() -> str:
        async with stdio_client(_server_parameters(environment)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                prepared = await session.call_tool(
                    "cassette_make_prompt",
                    {
                        "instruction": "add a title",
                        "session_id": "restart-session",
                        "requires_assets": False,
                    },
                )
                assert prepared.structuredContent["phase"] == "ready"
                result = await session.call_tool(
                    "cassette_run_job",
                    {
                        "prompt": prepared.structuredContent["data"]["prompt"],
                        "session_id": "restart-session",
                        "wait": True,
                        "export": True,  # explicit export intent keeps the completion-review gate
                    },
                )
                assert result.structuredContent["phase"] == "needs_user"
                return result.structuredContent["job_id"]

    async def restarted_host(job_id: str):
        async with stdio_client(_server_parameters(environment)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                resumed = await session.call_tool(
                    "cassette_answer_question",
                    {"job_id": job_id, "response": "Use blue"},
                )
                assert resumed.structuredContent["ok"] is True
                assert resumed.structuredContent["phase"] in {"running", "review_required"}
                deadline = time.monotonic() + 10
                status = resumed
                while time.monotonic() < deadline and status.structuredContent["phase"] == "running":
                    status = await session.call_tool(
                        "cassette_job_status",
                        {"job_id": job_id, "wait_for_change_sec": 2},
                    )
                assert status.structuredContent["phase"] == "review_required"

    try:
        job_id = asyncio.run(initial_host())
        asyncio.run(restarted_host(job_id))
        assert server.record["response"] == {"action": "respond", "userResponse": "Use blue"}
    finally:
        server.shutdown()
        server.server_close()


def test_job_status_elicits_needs_user_answer_when_client_supports_it():
    from types import SimpleNamespace

    from mcp_plugin import server as server_module
    from mcp_plugin.models import SessionPhase, ToolEnvelope

    pending = ToolEnvelope(
        ok=True,
        data={"job": {"questions": [{"question": "Which aspect ratio should the export use?"}]}},
        session_id="sess",
        job_id="cassette_job",
        phase=SessionPhase.NEEDS_USER,
        next_action="ask the user",
    )
    answered = ToolEnvelope(
        ok=True,
        session_id="sess",
        job_id="cassette_job",
        phase=SessionPhase.RUNNING,
        next_action="poll",
    )

    class Runtime:
        def answer_question(self, args):
            assert args == {"job_id": "cassette_job", "response": "16:9"}
            return answered

    elicited = []

    def _context(capability, action="accept", response="16:9"):
        class Ctx:
            session = SimpleNamespace(
                client_params=SimpleNamespace(capabilities=SimpleNamespace(elicitation=capability))
            )
            request_context = SimpleNamespace(lifespan_context=SimpleNamespace(runtime=Runtime()))

            async def elicit(self, message, schema):
                elicited.append(message)
                return SimpleNamespace(action=action, data=SimpleNamespace(response=response))

        return Ctx()

    result = asyncio.run(server_module._maybe_elicit_needs_user(_context(object()), pending))
    assert result is answered
    assert elicited == ["Which aspect ratio should the export use?"]

    # No client capability: untouched envelope, no elicitation round-trip.
    elicited.clear()
    result = asyncio.run(server_module._maybe_elicit_needs_user(_context(None), pending))
    assert result is pending and elicited == []

    # A declined elicitation keeps the documented tool round-trip.
    result = asyncio.run(server_module._maybe_elicit_needs_user(_context(object(), action="decline"), pending))
    assert result is pending

    # Non-needs_user envelopes are never elicited.
    elicited.clear()
    result = asyncio.run(server_module._maybe_elicit_needs_user(_context(object()), answered))
    assert result is answered and elicited == []


class _LoginApi(BaseHTTPRequestHandler):
    """The two unauthenticated agent-auth routes cassette_login uses, over a real socket.

    Scripted per test through `server.script`, and every request is recorded so a test can
    assert that a mode which must send nothing really sent nothing.
    """

    def log_message(self, *_args):
        pass

    @property
    def script(self):
        return self.server.script  # type: ignore[attr-defined]

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        self.script["requests"].append({"path": path, "body": body})
        status, payload, retry_after = self.script.get(path, (404, {"error": "not found"}, None))
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(encoded)


def _login_environment(tmp_path: Path, script: dict) -> tuple[dict[str, str], ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoginApi)
    script.setdefault("requests", [])
    server.script = script  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _, port = server.server_address
    project = tmp_path / "project"
    project.mkdir()
    environment = _environment(tmp_path, project)
    environment["CASSETTE_API_URL"] = f"http://127.0.0.1:{port}"
    return environment, server


def _call_login(environment: dict[str, str], arguments: dict) -> dict:
    async def exercise() -> dict:
        async with stdio_client(_server_parameters(environment)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("cassette_login", arguments)
                return result.structuredContent

    return asyncio.run(exercise())


def _seed_stored_credentials(environment: dict[str, str], password: str) -> Path:
    config = Path(environment["CASSETTE_CONFIG_HOME"])
    config.mkdir(mode=0o700, exist_ok=True)
    path = config / "credentials.json"
    path.write_text(
        json.dumps({"email": "stored@example.test", "password": password, "verified_at": "2026-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_protocol_login_verifies_then_stores_credentials_privately(tmp_path):
    password = "protocol-emailed-password"
    environment, server = _login_environment(
        tmp_path,
        {"/api/agent-auth/verify": (200, {"session": {"access_token": "ephemeral"}}, None)},
    )
    try:
        envelope = _call_login(environment, {"email": "person@example.test", "password": f"  {password}  "})
    finally:
        server.shutdown()

    assert envelope["ok"] is True
    assert envelope["data"]["stored"] is True
    # The pasted value is already in the transcript; the envelope must not add another copy,
    # and neither must the redaction placeholder be needed to achieve that.
    assert password not in json.dumps(envelope)
    stored_path = Path(envelope["data"]["credentials_path"])
    assert stored_path.stat().st_mode & 0o777 == 0o600
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    # Exact key set: no token, and no api_url that could silently disagree with settings.json.
    assert set(stored) == {"email", "password", "verified_at"}
    # Whitespace is stripped on write because load_credentials() strips on read; otherwise a
    # pasted password with a trailing space would verify here and fail on the next call.
    assert stored["password"] == password
    assert server.script["requests"] == [
        {"path": "/api/agent-auth/verify", "body": {"email": "person@example.test", "password": password}}
    ]


def test_protocol_login_clears_auth_required_for_the_other_tools(tmp_path):
    environment, server = _login_environment(
        tmp_path,
        {"/api/agent-auth/verify": (200, {"session": {"access_token": "ephemeral"}}, None)},
    )

    # cassette_timeline reaches the transport, which resolves credentials itself. Probing with
    # it therefore proves the stored file is picked up all the way down, not just past the
    # runtime's gate. Both errors after sign-in come from the stub API having no project.
    probe = ("cassette_timeline", {"session_id": "login-session"})
    auth_codes = {"auth_missing_credentials", "auth_required", "auth_failed"}

    async def exercise():
        async with stdio_client(_server_parameters(environment)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                before = await session.call_tool(*probe)
                assert before.structuredContent["error"]["code"] == "auth_missing_credentials"
                signed_in = await session.call_tool(
                    "cassette_login", {"email": "person@example.test", "password": "protocol-emailed-password"}
                )
                assert signed_in.structuredContent["ok"] is True
                # Same process, same session: credentials are re-read on every call, so the
                # machine stops being unauthenticated without a host restart.
                after = await session.call_tool(*probe)
                assert after.structuredContent["error"]["code"] not in auth_codes

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()


def test_protocol_login_rejects_a_stale_password_and_keeps_the_working_one(tmp_path):
    environment, server = _login_environment(
        tmp_path, {"/api/agent-auth/verify": (401, {"error": "Invalid credentials"}, None)}
    )
    stored_path = _seed_stored_credentials(environment, "still-works")
    before = stored_path.read_bytes()
    try:
        envelope = _call_login(environment, {"email": "person@example.test", "password": "stale-paste"})
    finally:
        server.shutdown()

    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "auth_invalid_password"
    # Verify-before-write: a bad paste must not cost the user a credential that still works.
    assert stored_path.read_bytes() == before
    serialized = json.dumps(envelope)
    assert "stale-paste" not in serialized
    assert "still-works" not in serialized


def test_protocol_login_reports_an_address_without_access(tmp_path):
    environment, server = _login_environment(tmp_path, {"/api/agent-auth/verify": (403, {"error": "Forbidden"}, None)})
    try:
        envelope = _call_login(environment, {"email": "outsider@example.test", "password": "any-password"})
    finally:
        server.shutdown()

    assert envelope["error"]["code"] == "auth_not_authorized"
    assert not (Path(environment["CASSETTE_CONFIG_HOME"]) / "credentials.json").exists()


def test_protocol_login_surfaces_the_retry_window_when_rate_limited(tmp_path):
    environment, server = _login_environment(
        tmp_path, {"/api/agent-auth/verify": (429, {"error": "Too many requests"}, 90)}
    )
    try:
        envelope = _call_login(environment, {"email": "person@example.test", "password": "any-password"})
    finally:
        server.shutdown()

    assert envelope["error"]["code"] == "auth_rate_limited"
    # A number, not prose: the agent has to be able to tell the user how long to wait.
    assert envelope["error"]["details"]["retry_after_sec"] == 90


def test_protocol_login_refuses_to_replace_a_password_without_confirmation(tmp_path):
    environment, server = _login_environment(tmp_path, {})
    try:
        envelope = _call_login(environment, {"email": "person@example.test", "request_new_password": True})
    finally:
        server.shutdown()

    assert envelope["error"]["code"] == "auth_confirm_required"
    assert envelope["error"]["details"]["replaces_existing"] is True
    # The point of the guard: with a real server listening, nothing reached it. The account
    # password is only safe if the refusal happens before the request, not after.
    assert server.script["requests"] == []


def test_protocol_login_requests_a_new_password_and_asks_for_it_back(tmp_path):
    environment, server = _login_environment(tmp_path, {"/api/agent-auth/request-code": (200, {"sent": True}, None)})
    stored_path = _seed_stored_credentials(environment, "about-to-die")
    try:
        envelope = _call_login(
            environment,
            {"email": "person@example.test", "request_new_password": True, "confirm_replace": True},
        )
    finally:
        server.shutdown()

    # ok=False on purpose: the caller asked to be signed in, and it is not — the replacement
    # exists only in the user's inbox, so the agent still has to collect it.
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "auth_password_emailed"
    assert envelope["error"]["details"]["replaces_existing"] is True
    assert server.script["requests"] == [
        {"path": "/api/agent-auth/request-code", "body": {"email": "person@example.test"}}
    ]
    # The reset does not sign this machine in, so the dead password stays on disk until the
    # user pastes the new one. auth_failed on the next real call is what surfaces it.
    assert json.loads(stored_path.read_text(encoding="utf-8"))["password"] == "about-to-die"


def test_protocol_login_reset_reports_an_unlisted_address_without_claiming_a_replacement(tmp_path):
    # The backend answers 200 sent=false for an address it will not confirm, and touches no
    # password in that case, so this is the one negative that must not warn about replacement.
    environment, server = _login_environment(
        tmp_path, {"/api/agent-auth/request-code": (200, {"sent": False, "reason": "not_allowed"}, None)}
    )
    try:
        envelope = _call_login(
            environment,
            {"email": "outsider@example.test", "request_new_password": True, "confirm_replace": True},
        )
    finally:
        server.shutdown()

    assert envelope["error"]["code"] == "auth_not_authorized"
    assert "password_replaced" not in envelope["error"]["details"]


def test_protocol_login_reset_does_not_declare_the_password_dead_when_delivery_fails(tmp_path):
    # The server mails the replacement before it stores it, so a 500 leaves the previous
    # password working. Warning that it died would send the user off to burn the hourly limit
    # on a replacement they do not need.
    environment, server = _login_environment(
        tmp_path, {"/api/agent-auth/request-code": (500, {"error": "email provider unavailable"}, None)}
    )
    try:
        envelope = _call_login(
            environment,
            {"email": "person@example.test", "request_new_password": True, "confirm_replace": True},
        )
    finally:
        server.shutdown()

    assert envelope["error"]["code"] == "auth_password_request_failed"
    assert "password_replaced" not in envelope["error"]["details"]
    assert "still work" in envelope["error"]["message"]


def test_protocol_login_refuses_when_credentials_come_from_the_environment(tmp_path):
    environment, server = _login_environment(tmp_path, {})
    environment.update({"CASSETTE_AUTH_EMAIL": "env@example.test", "CASSETTE_AUTH_PASSWORD": "from-env"})
    try:
        envelope = _call_login(environment, {"email": "person@example.test", "password": "pasted"})
    finally:
        server.shutdown()

    assert envelope["error"]["code"] == "auth_env_precedence"
    assert envelope["error"]["details"]["credential_source"] == "environment"
    # Writing the file would leave those variables shadowing it, so refuse before verifying.
    assert server.script["requests"] == []
    assert not (Path(environment["CASSETTE_CONFIG_HOME"]) / "credentials.json").exists()


def test_protocol_login_rejects_incoherent_argument_combinations(tmp_path):
    environment, server = _login_environment(tmp_path, {})

    async def exercise():
        async with stdio_client(_server_parameters(environment)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for arguments in (
                    {"email": "person@example.test"},  # neither a password nor a reset request
                    {"email": "   "},  # blank after stripping
                    {  # both modes at once: which one would win is not something to guess
                        "email": "person@example.test",
                        "password": "pasted",
                        "request_new_password": True,
                    },
                ):
                    result = await session.call_tool("cassette_login", arguments)
                    assert result.structuredContent["ok"] is False
                    assert result.structuredContent["error"]["code"] == "validation_error"
                    assert "pasted" not in json.dumps(result.structuredContent)

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()

    assert server.script["requests"] == []


def test_protocol_auth_required_labels_every_recovery_option(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    environment = _environment(tmp_path, project)

    async def exercise() -> dict:
        async with stdio_client(_server_parameters(environment)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("cassette_run_job", {"prompt": "edit", "session_id": "no-creds"})
                return result.structuredContent

    envelope = asyncio.run(exercise())
    assert envelope["error"]["code"] == "auth_required"
    details = envelope["error"]["details"]
    recovery = details["recovery"]
    # Each option carries the condition it answers, so the agent relays a decision rather than
    # reading out a menu of two commands and letting the user guess.
    assert [entry["when"] for entry in recovery] == [
        "the user has the generated password from their Cassette email",
        "the user no longer has that password",
        "the user would rather not paste a password into this conversation",
    ]
    assert "cassette_login" in recovery[0]["action"]
    assert "confirm_replace=true" in recovery[1]["action"]
    # Only the destructive option states a consequence, and it names the blast radius.
    assert "every machine" in recovery[1]["consequence"]
    assert recovery[2]["action"].endswith("scripts/setup_local_mcp.py")
    # The flat keys stay for hosts and skills pinned to the older shape.
    assert details["setup_command"].endswith("scripts/setup_local_mcp.py")
    assert "--reset-password" in details["reset_password_command"]


def test_protocol_initialize_reports_whether_this_machine_is_signed_in(tmp_path):
    # Without this the agent cannot know to offer sign-in until an edit has already failed,
    # which is exactly the moment the user has just uploaded four clips. It has to arrive in
    # the initialize reply, so it is asserted on the wire rather than on the FastMCP object.
    project = tmp_path / "project"
    project.mkdir()
    environment = _environment(tmp_path, project)

    def instructions() -> str:
        reply = _speak_raw(environment, [_initialize_request("2025-11-25")]).get(1)
        assert reply is not None, "the server never answered initialize"
        return reply["result"].get("instructions") or ""

    unconfigured = instructions()
    assert "had no Cassette credentials stored" in unconfigured
    assert "offer to sign in" in unconfigured

    _seed_stored_credentials(environment, "protocol-stored-password")
    configured = instructions()
    assert "was signed in to Cassette" in configured
    assert "verified 2026-01-01T00:00:00Z" in configured
    # It lands in the model's context on every single run, so it carries no address and,
    # obviously, no password.
    assert "stored@example.test" not in configured
    assert "protocol-stored-password" not in configured


def test_protocol_login_reports_an_unexpected_verify_status_without_guessing(tmp_path):
    # A 500 says nothing about the password, so it must not be reported as a rejected one --
    # that would send the user to burn a reset over a server-side blip.
    environment, server = _login_environment(tmp_path, {"/api/agent-auth/verify": (500, {"error": "upstream"}, None)})
    stored_path = _seed_stored_credentials(environment, "still-works")
    before = stored_path.read_bytes()
    try:
        envelope = _call_login(environment, {"email": "person@example.test", "password": "probably-fine"})
    finally:
        server.shutdown()

    assert envelope["error"]["code"] == "auth_verify_failed"
    assert stored_path.read_bytes() == before


def test_protocol_login_reset_surfaces_the_hourly_limit(tmp_path):
    # request-code allows three an hour and spends an attempt even for an address with no
    # access, so the wait has to reach the user as a number rather than "try again later".
    environment, server = _login_environment(
        tmp_path, {"/api/agent-auth/request-code": (429, {"error": "Too many password requests"}, 1800)}
    )
    try:
        envelope = _call_login(
            environment,
            {"email": "person@example.test", "request_new_password": True, "confirm_replace": True},
        )
    finally:
        server.shutdown()

    assert envelope["error"]["code"] == "auth_rate_limited"
    assert envelope["error"]["details"]["retry_after_sec"] == 1800
    # Rate limiting happens before the password is touched, so this must not warn about one.
    assert "password_replaced" not in envelope["error"]["details"]


def test_protocol_login_reports_an_unreachable_api_without_touching_credentials(tmp_path):
    # No server at all: the failure has to arrive as a typed envelope rather than a traceback,
    # and a network blip must never cost a working stored password.
    project = tmp_path / "project"
    project.mkdir()
    environment = _environment(tmp_path, project)
    # A port nothing is listening on. Bound and released so the number is real but dead.
    closed = ThreadingHTTPServer(("127.0.0.1", 0), _LoginApi)
    port = closed.server_address[1]
    closed.server_close()
    environment["CASSETTE_API_URL"] = f"http://127.0.0.1:{port}"
    stored_path = _seed_stored_credentials(environment, "still-works")
    before = stored_path.read_bytes()

    envelope = _call_login(environment, {"email": "person@example.test", "password": "pasted"})

    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "cassette_unreachable"
    assert stored_path.read_bytes() == before
    assert "pasted" not in json.dumps(envelope)
