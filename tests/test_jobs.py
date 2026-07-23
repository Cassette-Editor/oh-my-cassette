from __future__ import annotations

from cassette import jobs
from cassette.errors import CassetteError


def test_job_create_status_cancel(cassette_env):
    job = jobs.create_job("sess", "prompt text", "instruction", ["/tmp/a.mp4"], {"url": "https://example.test"})
    assert job["status"] == "queued"
    loaded = jobs.load_job(job["job_id"])
    assert loaded["prompt"] == "prompt text"
    assert loaded["prompt_redacted"].startswith("<redacted:")

    listed = jobs.list_jobs("sess")
    assert listed[0]["job_id"] == job["job_id"]
    assert "prompt" not in listed[0]
    assert "asset_paths" not in listed[0]

    cancelled = jobs.request_cancel(job["job_id"])
    assert cancelled["status"] == "cancel_requested"
    assert cancelled["finished_at"] is None
    assert jobs.is_cancel_requested(job["job_id"]) is True


def test_resume_worker_persists_response_before_process_spawn(cassette_env, monkeypatch):
    job = jobs.create_job("sess", "prompt", "instruction", [], {})
    jobs.update_job(job["job_id"], status="needs_user")

    class Process:
        pid = 12345

    def spawn(*_args, **_kwargs):
        observed = jobs.load_job(job["job_id"])
        assert observed["status"] == "running"
        assert observed["resume_request"] == {"response": "Use blue"}
        return Process()

    monkeypatch.setattr(jobs.subprocess, "Popen", spawn)
    started = jobs.start_worker(job["job_id"], action="resume", response="Use blue")
    assert started["worker_pid"] == 12345
    assert started["resume_request"] == {"response": "Use blue"}


def test_job_id_cannot_escape_jobs_directory(cassette_env):
    try:
        jobs.load_job("../../credentials")
    except CassetteError as exc:
        assert exc.code == "invalid_job_id"
    else:
        raise AssertionError("path traversal job ID was accepted")


def test_create_job_defaults_to_try_session_namespace(cassette_env):
    """New sessions live in the try-session-* namespace (token-free editor deep links); explicit
    ids — including pre-existing un-prefixed ones — are preserved verbatim."""
    defaulted = jobs.create_job("h4sh", "prompt", None, [], {})
    assert defaulted["cassette_session_id"] == "try-session-h4sh"
    explicit = jobs.create_job("h4sh", "prompt", None, [], {"cassette_session_id": "legacy-id"})
    assert explicit["cassette_session_id"] == "legacy-id"
