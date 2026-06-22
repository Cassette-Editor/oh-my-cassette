"""End-to-end exercise of ApiTransport against a mock Cassette API.

Stands up a stdlib HTTP server implementing the Cassette server contract and drives the full
ApiTransport.run_job orchestration through it: auth -> media upload (init/PUT/complete) ->
LangGraph thread + run -> editor_navigate headless interrupt + KEYED resume -> render-from-stored-
project export -> download to disk -> 6-key result. This validates the request/response wire format
and the interrupt loop offline (no live Cassette, no Playwright), which is otherwise only verifiable
during live bring-up.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cassette.api_transport import ApiTransport

EXPORT_BYTES = b"FAKE_MP4_BYTES"


class _MockCassetteAPI(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    @property
    def rec(self) -> dict:
        return self.server.rec  # type: ignore[attr-defined]

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, code: int, data: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):  # presigned upload target
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.rec["put_count"] += 1
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._body()
        self.rec["requests"].append(("POST", path))

        if path == "/api/agent-auth/verify":
            self.rec["auth_email"] = body.get("email")
            return self._json(200, {
                "user": {"id": "u1", "email": body.get("email")},
                "session": {"access_token": "tok-123", "refresh_token": "r", "expires_in": 3600, "expires_at": 0},
                "sessionExpiry": 0, "isFullUser": True,
            })
        if path == "/api/media/upload/init":
            self.rec["init_count"] += 1
            self.rec["init_bodies"].append(body)
            key = f"k-{self.rec['init_count']}"
            return self._json(200, {"key": key, "uploadUrl": f"http://{self.headers.get('Host')}/_put/{key}",
                                    "storageBackend": "r2"})
        if path == "/api/media/upload/complete":
            self.rec["complete_count"] += 1
            return self._json(200, {"mediaFileId": f"m-{self.rec['complete_count']}", "uploadStatus": "completed"})
        if path == "/api/langgraph/threads":
            self.rec["thread_metadata"] = body.get("metadata")
            return self._json(200, {"thread_id": "th-1"})
        if path == "/api/langgraph/threads/th-1/runs":
            if isinstance(body.get("command"), dict):
                self.rec["resume_value"] = body["command"].get("resume")
                return self._json(200, {"run_id": "r-2", "status": "pending"})
            self.rec["run_input"] = body.get("input")
            self.rec["run_config"] = body.get("config")
            return self._json(200, {"run_id": "r-1", "status": "pending"})
        if path.startswith("/api/export/projects/") and path.endswith("/jobs"):
            self.rec["export_session"] = path.split("/api/export/projects/", 1)[1].rsplit("/jobs", 1)[0]
            return self._json(202, {"jobId": "ej-1", "status": "queued", "statusUrl": "/api/export/jobs/ej-1"})
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        self.rec["requests"].append(("GET", path))

        if path == "/api/media/upload/status":
            return self._json(200, {"uploadStatus": "completed"})
        if path == "/api/langgraph/threads/th-1/runs/r-1":
            return self._json(200, {"run_id": "r-1", "status": "interrupted"})
        if path == "/api/langgraph/threads/th-1/runs/r-2":
            return self._json(200, {"run_id": "r-2", "status": "success"})
        if path == "/api/langgraph/threads/th-1/state":
            # Only editor_navigate (the sole browser-target tool) interrupts a headless run.
            return self._json(200, {"values": {}, "tasks": [{"interrupts": [
                {"id": "int-1", "value": {"type": "tool", "toolCall": {"id": "call-1", "name": "editor_navigate", "args": {}}}},
            ]}]})
        if path == "/api/export/jobs/ej-1":
            return self._json(200, {"jobId": "ej-1", "status": "done", "fileUrl": "/api/export/jobs/ej-1/file"})
        if path == "/api/export/jobs/ej-1/file":
            return self._bytes(200, EXPORT_BYTES, "video/mp4")
        return self._json(404, {"error": "not found"})


@pytest.fixture
def mock_api(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockCassetteAPI)
    server.rec = {  # type: ignore[attr-defined]
        "requests": [], "put_count": 0, "init_count": 0, "complete_count": 0,
        "init_bodies": [], "auth_email": None, "resume_value": None,
        "run_input": None, "run_config": None, "thread_metadata": None, "export_session": None,
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _, port = server.server_address
    monkeypatch.setenv("CASSETTE_API_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "e@x.io")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "pw")
    monkeypatch.setenv("CASSETTE_API_POLL_INTERVAL_SEC", "1")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_api_transport_run_job_end_to_end(cassette_env, mock_api, tmp_path):
    asset = tmp_path / "clip.mp4"
    asset.write_bytes(b"x" * 64)
    job = {
        "job_id": "job-e2e",
        "session_hash": "sess",
        "cassette_session_id": "sess",
        "prompt": "make a short captioned video",
        "asset_paths": [str(asset)],
        "timeout_sec": 60,
        "model_selection": {},
        "cassette_language": "en",
    }

    result = ApiTransport().run_job(job)
    rec = mock_api.rec

    # Terminal success with a real on-disk export (so notifier delivers it).
    assert result["status"] == "succeeded", result["errors"]
    assert set(result) >= {"status", "outputs", "questions", "errors", "quality", "final_screenshot"}
    assert result["outputs"], "expected a deliverable output"
    out = result["outputs"][0]
    assert out["kind"] == "video"
    assert Path(out["local_path"]).exists()
    assert Path(out["local_path"]).read_bytes() == EXPORT_BYTES
    assert result["quality"]["export_completed"] is True
    assert result["quality"]["local_output_count"] == 1

    # Auth happened with the configured account.
    assert rec["auth_email"] == "e@x.io"
    # Media uploaded via init -> PUT -> complete exactly once.
    assert (rec["init_count"], rec["put_count"], rec["complete_count"]) == (1, 1, 1)
    assert rec["init_bodies"][0]["fileName"] == "clip.mp4"
    assert rec["init_bodies"][0]["mimeType"] == "video/mp4"
    # Run input + sessionContext shape the agent expects.
    assert rec["run_input"]["messages"][0] == {"type": "human", "content": job["prompt"]}
    assert rec["run_config"]["configurable"]["sessionContext"]["projectId"] == "sess"
    # The lone editor_navigate interrupt resumed KEYED by toolCall.id with a schema-valid no-op.
    assert isinstance(rec["resume_value"], dict) and "call-1" in rec["resume_value"]
    nav = rec["resume_value"]["call-1"]["result"]
    assert nav["ok"] is True and nav["noOp"] is True and nav["newVersion"] == 0
    # Export targeted the stored project by session id.
    assert rec["export_session"] == "sess"


def test_api_transport_forbidden_surfaces_full_access_hint(cassette_env, monkeypatch):
    """A 403 on an account-scoped call yields a clear 'forbidden' error, not an opaque failure."""
    class _Forbidden(_MockCassetteAPI):
        def do_POST(self):
            path = self.path.split("?", 1)[0]
            self._body()
            if path == "/api/agent-auth/verify":
                return self._json(200, {"session": {"access_token": "tok"}, "isFullUser": False})
            if path == "/api/media/upload/init":
                return self._json(403, {"error": "forbidden"})
            return self._json(404, {"error": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Forbidden)
    server.rec = {"requests": [], "put_count": 0, "init_count": 0, "complete_count": 0, "init_bodies": []}  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _, port = server.server_address
    monkeypatch.setenv("CASSETTE_API_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "e@x.io")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "pw")
    try:
        asset = Path(cassette_env["source_root"]) / "clip.mp4"
        asset.write_bytes(b"x" * 16)
        result = ApiTransport().run_job({
            "job_id": "job-403", "session_hash": "s", "cassette_session_id": "s",
            "prompt": "edit", "asset_paths": [str(asset)], "timeout_sec": 30,
        })
        assert result["status"] == "failed"
        assert result["errors"][0]["code"] == "forbidden"
        assert "full API access" in result["errors"][0]["message"]
    finally:
        server.shutdown()
        server.server_close()
