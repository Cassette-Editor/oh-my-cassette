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

from cassette import jobs
from cassette.api_transport import ApiTransport

EXPORT_BYTES = b"FAKE_MP4_BYTES"


def _serve(handler_cls, monkeypatch, extra_rec=None):
    """Start handler_cls on an ephemeral port, point the transport env at it, return (server, rec)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    rec = {"requests": [], "put_count": 0, "init_count": 0, "complete_count": 0,
           "init_bodies": [], "complete_bodies": [], "upload_session_ids": [], "upload_project_ids": [],
           "auth_email": None, "resume_value": None, "run_input": None, "run_config": None,
           "thread_metadata": None, "export_session": None, "status_polls": 0, "cancel_posts": [],
           "media_ready_polls": 0}
    rec.update(extra_rec or {})
    server.rec = rec  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _, port = server.server_address
    monkeypatch.setenv("CASSETTE_API_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "e@x.io")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "pw")
    monkeypatch.setenv("CASSETTE_API_POLL_INTERVAL_SEC", "1")
    # Exercise the full run_job pipeline (auth→upload→run→export) in one call; the completion-review
    # gate (the browser-parity default) has its own dedicated test that unsets this.
    monkeypatch.setenv("CASSETTE_API_AUTO_EXPORT", "1")
    return server


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
            self.rec["upload_session_ids"].append(self.headers.get("x-session-id"))
            self.rec["upload_project_ids"].append(self.headers.get("x-project-id"))
            key = f"k-{self.rec['init_count']}"
            return self._json(200, {"key": key, "uploadUrl": f"http://{self.headers.get('Host')}/_put/{key}",
                                    "uploadAttemptId": f"att-{self.rec['init_count']}",
                                    "uploadContentType": body.get("mimeType") or "application/octet-stream",
                                    "storageBackend": "r2"})
        if path == "/api/media/upload/complete":
            self.rec["complete_count"] += 1
            self.rec["complete_bodies"].append(body)
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
        if path == "/api/media/operations/status":
            # Report every completed upload as fully ready so the readiness gate proceeds.
            self.rec["media_ready_polls"] += 1
            statuses = [{"mediaFileId": f"m-{i}", "fullyReady": True, "aiReady": True,
                         "exportReady": True, "analysisReady": True, "renderStatus": "completed",
                         "terminalState": "succeeded", "readinessPhase": "ready"}
                        for i in range(1, self.rec["complete_count"] + 1)]
            return self._json(200, {"statuses": statuses})
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
        "init_bodies": [], "complete_bodies": [], "upload_session_ids": [], "upload_project_ids": [],
        "auth_email": None, "resume_value": None,
        "run_input": None, "run_config": None, "thread_metadata": None, "export_session": None,
        "media_ready_polls": 0,
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _, port = server.server_address
    monkeypatch.setenv("CASSETTE_API_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "e@x.io")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "pw")
    monkeypatch.setenv("CASSETTE_API_POLL_INTERVAL_SEC", "1")
    monkeypatch.setenv("CASSETTE_API_AUTO_EXPORT", "1")  # full-pipeline tests; gate test unsets this
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
    # Uploaded media is linked to the agent by SESSION id (mediaSessionId == upload x-session-id),
    # and bound to the project by x-project-id — both must equal the run's session id.
    assert rec["upload_session_ids"] == ["sess"]
    assert rec["upload_project_ids"] == ["sess"]
    # Run input + the FULL configurable the graph requires (sessionContext + projectContext + runContext).
    assert rec["run_input"]["messages"][0] == {"type": "human", "content": job["prompt"]}
    configurable = rec["run_config"]["configurable"]
    assert configurable["sessionContext"]["projectId"] == "sess"
    assert configurable["sessionContext"]["mediaSessionId"] == "sess"  # == upload x-session-id
    assert "projectContext" in configurable
    conn = configurable["runContext"]["connectionState"]
    assert conn["mediaSessionId"] == "sess" and conn["projectId"] == "sess"
    # The lone editor_navigate interrupt resumed KEYED by toolCall.id with a schema-valid no-op.
    assert isinstance(rec["resume_value"], dict) and "call-1" in rec["resume_value"]
    nav = rec["resume_value"]["call-1"]["result"]
    assert nav["ok"] is True and nav["noOp"] is True and nav["newVersion"] == 0
    # Export targeted the stored project by session id.
    assert rec["export_session"] == "sess"
    # The run waited for media readiness before starting the agent (empty/blank-export guard).
    assert rec["media_ready_polls"] >= 1


def test_api_transport_dedupes_uploads_in_reused_session(cassette_env, mock_api, tmp_path):
    """A reused gateway session that edits then refines must not re-upload the same asset (which would
    accumulate duplicate media in the project) — matching the browser path's per-session dedupe."""
    asset = tmp_path / "clip.mp4"
    asset.write_bytes(b"x" * 64)
    base = {"session_hash": "reuse", "cassette_session_id": "reuse",
            "prompt": "edit", "asset_paths": [str(asset)], "timeout_sec": 60}
    ApiTransport().run_job({**base, "job_id": "job-a"})
    first_inits = mock_api.rec["init_count"]
    assert first_inits == 1
    ApiTransport().run_job({**base, "job_id": "job-b"})
    # The second job reused the already-uploaded asset — no new upload/init.
    assert mock_api.rec["init_count"] == first_inits


def test_api_transport_records_run_progress(cassette_env, mock_api, tmp_path):
    """The run writes stage/telemetry into the job record (current_stage, stage_timings,
    progress_events) so status polls and _job_report are not frozen and empty."""
    asset = Path(cassette_env["source_root"]) / "clip.mp4"
    asset.write_bytes(b"x" * 32)
    job = jobs.create_job(session_hash="prog", prompt="edit", instruction=None,
                          asset_paths=[str(asset)], options={"cassette_session_id": "prog"})
    job["asset_paths"] = [str(asset)]
    job["prompt"] = "edit"
    ApiTransport().run_job(job)
    saved = jobs.load_job(job["job_id"])
    assert saved.get("current_stage")          # a live stage was recorded
    assert saved.get("progress_events")        # at least one structured progress event
    assert isinstance(saved.get("stage_timings"), dict) and saved["stage_timings"]


def test_api_transport_completion_review_gate(cassette_env, mock_api, monkeypatch, tmp_path):
    """By default (browser parity) a successful agent run does NOT auto-export — it returns needs_user
    with completion_review_required so the Hermes supervisor decides; cassette_review_completion then
    drives ApiTransport.export()."""
    monkeypatch.delenv("CASSETTE_API_AUTO_EXPORT", raising=False)
    asset = tmp_path / "clip.mp4"
    asset.write_bytes(b"x" * 64)
    job = {
        "job_id": "job-review-gate", "session_hash": "sess", "cassette_session_id": "sess",
        "prompt": "make a short captioned video", "asset_paths": [str(asset)], "timeout_sec": 60,
    }
    result = ApiTransport().run_job(job)
    # The run committed the edit but export is gated on Hermes review.
    assert result["status"] == "needs_user"
    assert result["quality"]["completion_review_required"] is True
    assert any(q.get("reason") == "completion_requires_hermes_review" for q in result["questions"])
    assert not any(p.startswith("/api/export/projects/") for _, p in mock_api.rec["requests"])

    # The reviewed export then renders + downloads the video.
    job.update(result)
    export_result = ApiTransport().export(job, {"decision": "export", "reason": "looks complete"})
    assert export_result["status"] == "succeeded", export_result["errors"]
    assert export_result["outputs"] and Path(export_result["outputs"][0]["local_path"]).read_bytes() == EXPORT_BYTES
    assert export_result["quality"]["completion_source"] == "hermes_completion_review"
    assert mock_api.rec["export_session"] == "sess"


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


class _ProcessingThenReadyAPI(_MockCassetteAPI):
    """upload/complete returns uploadStatus='processing'; the status endpoint reports 'processing'
    for the first two polls then 'completed' — exercising the media-processing wait loop."""
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/media/upload/complete":
            self._body()
            self.rec["complete_count"] += 1
            return self._json(200, {"mediaFileId": "m-1", "uploadStatus": "processing"})
        return super().do_POST()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/media/upload/status":
            self.rec["status_polls"] += 1
            self.rec["requests"].append(("GET", path))
            if self.rec["status_polls"] < 3:
                return self._json(202, {"uploadStatus": "processing"})
            return self._json(200, {"uploadStatus": "completed"})
        return super().do_GET()


def test_api_transport_waits_for_media_processing(cassette_env, monkeypatch, tmp_path):
    server = _serve(_ProcessingThenReadyAPI, monkeypatch)
    try:
        asset = tmp_path / "clip.mp4"
        asset.write_bytes(b"x" * 64)
        result = ApiTransport().run_job({
            "job_id": "job-proc", "session_hash": "s", "cassette_session_id": "s",
            "prompt": "edit", "asset_paths": [str(asset)], "timeout_sec": 60,
        })
        assert result["status"] == "succeeded", result["errors"]
        # The processing poll actually ran (it is the reason the loop exists).
        assert server.rec["status_polls"] >= 3
    finally:
        server.shutdown()
        server.server_close()


class _MediaReadyAfterPollsAPI(_MockCassetteAPI):
    """Media is not-ready for the first two readiness polls, then fully ready."""
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/media/operations/status":
            self.rec["media_ready_polls"] += 1
            ready = self.rec["media_ready_polls"] >= 3
            statuses = [{"mediaFileId": "m-1", "fullyReady": ready, "aiReady": ready,
                         "exportReady": ready, "renderStatus": "completed" if ready else "processing",
                         "terminalState": "succeeded" if ready else "processing",
                         "readinessPhase": "ready" if ready else "missing_embeddings"}]
            return self._json(200, {"statuses": statuses})
        return super().do_GET()


def test_api_transport_waits_for_media_full_readiness(cassette_env, monkeypatch, tmp_path):
    server = _serve(_MediaReadyAfterPollsAPI, monkeypatch)
    try:
        asset = tmp_path / "clip.mp4"
        asset.write_bytes(b"x" * 64)
        result = ApiTransport().run_job({
            "job_id": "job-ready", "session_hash": "s", "cassette_session_id": "s",
            "prompt": "edit", "asset_paths": [str(asset)], "timeout_sec": 120,
        })
        assert result["status"] == "succeeded", result["errors"]
        # It kept polling until media became fully ready (agent + render), not just upload-complete.
        assert server.rec["media_ready_polls"] >= 3
    finally:
        server.shutdown()
        server.server_close()


class _MediaFailsAPI(_MockCassetteAPI):
    """A required media derivative fails processing."""
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/media/operations/status":
            return self._json(200, {"statuses": [{
                "mediaFileId": "m-1", "fullyReady": False, "renderStatus": "failed",
                "terminalState": "failed", "errorMessage": "render-source transcode failed"}]})
        return super().do_GET()


def test_api_transport_surfaces_media_processing_failure(cassette_env, monkeypatch, tmp_path):
    server = _serve(_MediaFailsAPI, monkeypatch)
    try:
        asset = tmp_path / "clip.mp4"
        asset.write_bytes(b"x" * 64)
        result = ApiTransport().run_job({
            "job_id": "job-mediafail", "session_hash": "s", "cassette_session_id": "s",
            "prompt": "edit", "asset_paths": [str(asset)], "timeout_sec": 120,
        })
        assert result["status"] == "failed"
        assert result["errors"][0]["code"] == "media_processing_failed"
        # The run never started for un-renderable media.
        assert not any(p.startswith("/api/langgraph/threads/th-1/runs") for _, p in server.rec["requests"])
    finally:
        server.shutdown()
        server.server_close()


class _NeverStartsAPI(_MockCassetteAPI):
    """The run is created but the queue never drains it — status stays 'pending' forever."""
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/langgraph/threads/th-1/runs/"):
            self.rec["requests"].append(("GET", path))
            return self._json(200, {"run_id": "r-1", "status": "pending"})
        return super().do_GET()


def test_api_transport_fails_fast_when_run_never_starts(cassette_env, monkeypatch, tmp_path):
    monkeypatch.setenv("CASSETTE_API_RUN_START_TIMEOUT_SEC", "2")
    server = _serve(_NeverStartsAPI, monkeypatch)
    try:
        asset = tmp_path / "clip.mp4"
        asset.write_bytes(b"x" * 64)
        result = ApiTransport().run_job({
            "job_id": "job-stall", "session_hash": "s", "cassette_session_id": "s",
            "prompt": "edit", "asset_paths": [str(asset)], "timeout_sec": 600,
        })
        # A stalled queue is reported quickly and clearly, not after the full 600s job timeout.
        assert result["status"] == "failed"
        assert result["errors"][0]["code"] == "agent_run_not_started"
        # No export was attempted for a run that never started.
        assert not any(p.startswith("/api/export/projects/") for _, p in server.rec["requests"])
    finally:
        server.shutdown()
        server.server_close()


class _CancelAwareAPI(_MockCassetteAPI):
    """Records run-cancel POSTs and keeps the run 'running' so a cancel check can fire."""
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.endswith("/cancel"):
            self._body()
            self.rec["cancel_posts"].append(path)
            return self._json(202, {"ok": True})
        return super().do_POST()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/langgraph/threads/th-1/runs/r-1":
            self.rec["requests"].append(("GET", path))
            return self._json(200, {"run_id": "r-1", "status": "running"})
        return super().do_GET()


def test_api_transport_run_job_honors_cancel_request(cassette_env, monkeypatch, tmp_path):
    server = _serve(_CancelAwareAPI, monkeypatch)
    try:
        asset = Path(cassette_env["source_root"]) / "clip.mp4"
        asset.write_bytes(b"x" * 16)
        job = jobs.create_job(session_hash="s", prompt="edit", instruction=None,
                              asset_paths=[str(asset)], options={"cassette_session_id": "s"})
        jobs.request_cancel(job["job_id"])  # user hit /cut before we reached the run loop
        job["asset_paths"] = [str(asset)]
        job["prompt"] = "edit"

        result = ApiTransport().run_job(job)
        # The cancel is honored with a terminal 'cancelled' status (not overwritten by a run result),
        # and no export is performed for a cancelled job.
        assert result["status"] == "cancelled"
        assert not any(p.startswith("/api/export/projects/") for _, p in server.rec["requests"])
    finally:
        server.shutdown()
        server.server_close()


def test_export_overrides_stale_prior_quality(cassette_env, monkeypatch, tmp_path):
    """A Hermes-reviewed re-export that now succeeds must report export_pending=False even if the
    job's PRIOR quality (from a succeeded-but-export-pending run) recorded export_pending=True."""
    server = _serve(_MockCassetteAPI, monkeypatch)
    try:
        job = {
            "job_id": "job-review", "session_hash": "sess", "cassette_session_id": "sess",
            "questions": [{"question": "prior?", "requires_user": False, "reason": "x", "answer": "y"}],
            "errors": [],
            # Stale quality from the earlier run: export never finished.
            "quality": {"transport": "api", "completion_observed": True, "export_completed": False,
                        "export_pending": True, "output_link_count": 0, "local_output_count": 0,
                        "risk": "medium", "progress_summary": "edit committed earlier"},
        }
        result = ApiTransport().export(job, {"decision": "export", "reason": "looks done", "summary": "ship it"})
        assert result["status"] == "succeeded", result["errors"]
        q = result["quality"]
        # Fresh outcome wins over the stale prior metrics.
        assert q["export_pending"] is False
        assert q["export_completed"] is True
        assert q["output_link_count"] == 1 and q["local_output_count"] == 1
        assert q["risk"] == "low"
        # Descriptive prior context + review decision are preserved.
        assert result["questions"] == job["questions"]
        assert q["completion_source"] == "hermes_completion_review"
        assert q["completion_review"]["decision"] == "export"
    finally:
        server.shutdown()
        server.server_close()


def test_await_run_cancels_the_server_side_run(cassette_env, monkeypatch):
    server = _serve(_CancelAwareAPI, monkeypatch)
    try:
        t = ApiTransport()
        t._authenticate()
        # Simulate a cancel that arrives once the run loop is already polling.
        monkeypatch.setattr(t, "_cancelled", lambda job_id: True)
        import pytest as _pytest
        from cassette.api_transport import _JobCancelled
        with _pytest.raises(_JobCancelled):
            t._await_run("th-1", "r-1", deadline=__import__("time").monotonic() + 30, job_id="job-x")
        # It best-effort cancels the run server-side rather than just abandoning it locally.
        assert server.rec["cancel_posts"] == ["/api/langgraph/threads/th-1/runs/r-1/cancel"]
    finally:
        server.shutdown()
        server.server_close()
