from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
        assert by_name["cassette_run_job"].inputSchema["properties"]["wait"]["default"] is False
        assert "wait_for_change_sec" in by_name["cassette_job_status"].inputSchema["properties"]
        assert {"job_id", "response"} <= set(by_name["cassette_answer_question"].inputSchema["properties"])
        assert set(by_name["cassette_ingest_media"].inputSchema["properties"]["media_type"]["anyOf"][0]["enum"]) == {
            "video", "image", "audio", "file", "unknown"
        }
        assert set(by_name["cassette_review_completion"].inputSchema["properties"]["decision"]["enum"]) == {
            "export", "continue", "needs_user", "failed"
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
                assert session_id.startswith("mcp_")
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
                "full_api_access": True,
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
                            "messages": [
                                {"type": "assistant", "content": "The edit is complete and ready for review."}
                            ]
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
