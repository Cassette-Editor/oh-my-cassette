from __future__ import annotations

import json

import runtime_config
from cassette import jobs, tools
from cassette.transport import BrowserTransport
from mcp_plugin.models import SessionPhase
from mcp_plugin.runtime import LocalMcpRuntime


def _runtime(tmp_path, monkeypatch, *, full_api_access=True):
    monkeypatch.setenv("CASSETTE_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CASSETTE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CASSETTE_ASSET_ROOT", str(tmp_path / "data" / "cassette"))
    monkeypatch.setenv("CASSETTE_RUNTIME_ADAPTER", "mcp")
    monkeypatch.setenv("CASSETTE_TRANSPORT", "api")
    for key in (
        "CASSETTE_AUTH_EMAIL",
        "CASSETTE_AUTH_PASSWORD",
        "CASSETTE_AUTH_ACCOUNT",
        "CASSETTE_EMAIL",
        "CASSETTE_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    runtime_config.write_protected_json(
        runtime_config.credentials_path(),
        {
            "email": "private@example.test",
            "password": "private-password",
            "full_api_access": full_api_access,
        },
    )
    return LocalMcpRuntime(runtime_config.configure_mcp_process_environment())


def test_mcp_envelope_redacts_local_credentials(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    result = runtime._envelope_from_core(  # exercise the final MCP boundary, not only log redaction
        {
            "ok": False,
            "error": {
                "code": "synthetic",
                "message": "private@example.test used private-password",
                "details": {"debug": "private-password"},
                "recoverable": True,
            },
        },
        session_id="session",
    )
    serialized = result.model_dump_json()
    assert "private@example.test" not in serialized
    assert "private-password" not in serialized
    assert serialized.count("<redacted>") >= 2


def test_mcp_rejects_output_outside_job_export_directory(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not-an-export")
    job = jobs.create_job(
        session_hash="hash",
        prompt="edit",
        instruction=None,
        asset_paths=[],
        options={"cassette_session_id": "session"},
    )
    job.update(
        {
            "status": "succeeded",
            "outputs": [{"local_path": str(outside), "kind": "video"}],
            "quality": {"export_completed": True},
        }
    )
    jobs.save_job(job)
    result = runtime.job_status({"job_id": job["job_id"], "limit": 10})
    assert result.ok is False
    assert result.error.code == "output_path_not_allowed"
    assert result.phase == SessionPhase.EXPORTED


def test_mcp_rejects_symlinked_job_export_directory(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    job = jobs.create_job(
        session_hash="hash",
        prompt="edit",
        instruction=None,
        asset_paths=[],
        options={"cassette_session_id": "session"},
    )
    outside = tmp_path / "outside-export"
    outside.mkdir()
    exported = outside / "edited.mp4"
    exported.write_bytes(b"not-contained")
    linked_root = runtime_config.asset_root() / "exports" / job["job_id"]
    linked_root.parent.mkdir(parents=True, exist_ok=True)
    linked_root.symlink_to(outside, target_is_directory=True)
    job.update(
        {
            "status": "succeeded",
            "outputs": [{"local_path": str(linked_root / "edited.mp4"), "kind": "video"}],
            "quality": {"export_completed": True},
        }
    )
    jobs.save_job(job)

    result = runtime.job_status({"job_id": job["job_id"], "limit": 10})
    assert result.ok is False
    assert result.error.code == "output_path_not_allowed"
    assert "symlink" in result.error.details["reason"]


def test_mcp_normalizes_legacy_completion_labels_at_public_boundary(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    normalized = runtime._redact(
        {
            "reason": "completion_requires_hermes_review",
            "quality": {"completion_source": "hermes_completion_review"},
        }
    )
    assert normalized == {
        "reason": "completion_requires_review",
        "quality": {"completion_source": "completion_review"},
    }


def test_completion_review_requires_review_phase_before_auth(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    job = jobs.create_job(
        session_hash="hash",
        prompt="edit",
        instruction=None,
        asset_paths=[],
        options={"cassette_session_id": "session"},
    )
    jobs.update_job(job["job_id"], status="running")
    result = runtime.review_completion({"job_id": job["job_id"], "decision": "export", "reason": "looks done"})
    assert result.ok is False
    assert result.error.code == "invalid_transition"
    assert result.phase == SessionPhase.RUNNING


def test_free_api_account_returns_explicit_browser_setup_path(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, full_api_access=False)
    result = runtime.run_job({"prompt": "edit", "session_id": "session", "wait": False})
    assert result.ok is False
    assert result.error.code == "api_access_unavailable"
    assert result.error.details["browser_setup_command"].endswith("setup_local_mcp.py --with-browser")


def test_run_job_requires_session_and_typed_ready_phase(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)

    missing_session = runtime.run_job({"prompt": "edit", "wait": False})
    assert missing_session.ok is False
    assert missing_session.error.code == "session_id_required"
    assert missing_session.phase == SessionPhase.NEW

    not_ready = runtime.run_job({"prompt": "edit", "session_id": "session", "wait": False})
    assert not_ready.ok is False
    assert not_ready.error.code == "invalid_transition"
    assert not_ready.phase == SessionPhase.NEW


def test_browser_resume_after_process_restart_has_typed_error():
    result = BrowserTransport().resume(
        {
            "job_id": "missing-browser-session",
            "cassette_session_id": "session",
            "status": "needs_user",
            "questions": [],
            "errors": [],
            "quality": {},
        },
        "continue",
    )
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "browser_session_lost"


def test_mcp_browser_resume_after_restart_returns_typed_envelope(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("CASSETTE_TRANSPORT", "browser")
    job = jobs.create_job(
        session_hash="hash",
        prompt="edit",
        instruction=None,
        asset_paths=[],
        options={"cassette_session_id": "browser-session"},
    )
    job.update(
        {
            "status": "needs_user",
            "questions": [{"question": "Choose a title", "requires_user": True}],
            "quality": {},
        }
    )
    jobs.save_job(job)

    result = runtime.answer_question({"job_id": job["job_id"], "response": "Blue"})
    assert result.ok is False
    assert result.error.code == "browser_session_lost"
    assert result.phase == SessionPhase.FAILED


def test_direct_core_and_mcp_adapter_preserve_ingest_success_contract(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    source = project / "clip.mp4"
    source.write_bytes(b"clip")
    monkeypatch.setenv("CASSETTE_PROJECT_ROOT", str(project))

    direct = json.loads(tools.cassette_ingest_media({"source_path": str(source), "session_id": "direct"}))
    adapted = runtime.ingest_media({"source_path": str(source), "session_id": "adapted"}, [project])
    assert direct["ok"] is adapted.ok is True
    assert set(direct["data"]) <= set(adapted.data)
    assert adapted.phase == SessionPhase.ASSETS_READY

    generated = runtime.make_prompt({"instruction": "Make it concise", "session_id": "adapted"})
    assert generated.ok is True
    assert generated.data["prompt"].startswith("You are the user's Codex or Claude host agent")
    assert "You are Hermes" not in generated.data["prompt"]


def test_direct_core_and_mcp_adapter_preserve_semantic_validation_error(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    direct = json.loads(
        tools.cassette_make_prompt({"instruction": "", "session_id": "validation-session", "requires_assets": False})
    )
    adapted = runtime.make_prompt({"instruction": "", "session_id": "validation-session", "requires_assets": False})
    assert direct["ok"] is adapted.ok is False
    assert direct["error"]["code"] == adapted.error.code == "missing_required_arg"


def test_job_status_long_poll_reports_wait_ticks_and_survives_tick_errors(tmp_path, monkeypatch):
    import mcp_plugin.runtime as runtime_module

    runtime = _runtime(tmp_path, monkeypatch)
    job = jobs.create_job("tick-hash", "prompt", "instruction", [], {"cassette_session_id": "tick-session"})
    jobs.update_job(job["job_id"], status="running", current_stage="editing")
    monkeypatch.setattr(runtime_module, "WAIT_TICK_SEC", 0.05)

    ticks = []
    envelope = runtime.job_status(
        {"job_id": job["job_id"], "wait_for_change_sec": 0.4},
        on_wait_tick=lambda elapsed, total, stage: ticks.append((elapsed, total, stage)),
    )
    assert envelope.ok is True
    assert envelope.phase is SessionPhase.RUNNING
    assert ticks, "expected at least one progress tick during the long poll"
    assert all(total == 0.4 and stage == "editing" for _elapsed, total, stage in ticks)

    def broken_tick(elapsed, total, stage):
        raise RuntimeError("client went away")

    envelope = runtime.job_status(
        {"job_id": job["job_id"], "wait_for_change_sec": 0.2},
        on_wait_tick=broken_tick,
    )
    assert envelope.ok is True


def test_mcp_ingest_mints_try_session_ids(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    captured: dict = {}

    def fake_invoke(tool, args, *, session_id, roots):
        captured["session_id"] = session_id
        return {"ok": True, "data": {}}

    monkeypatch.setattr(runtime, "_invoke_core", fake_invoke)
    envelope = runtime.ingest_media({"source_path": "unused"}, roots=[])
    assert envelope.ok
    assert captured["session_id"].startswith("try-session-")
    # Explicit session ids are never rewritten.
    envelope = runtime.ingest_media({"source_path": "unused", "session_id": "legacy"}, roots=[])
    assert envelope.ok
    assert captured["session_id"] == "legacy"


def test_run_job_multi_turn_phase_gating(tmp_path, monkeypatch):
    """A settled turn (succeeded/guided) may start the next run; in-flight phases refuse."""
    runtime = _runtime(tmp_path, monkeypatch)

    runtime.state.transition("session", SessionPhase.GUIDED_CHOICES)
    from_guided = runtime.run_job({"message": "turn", "session_id": "session", "wait": False})
    assert from_guided.error is None or from_guided.error.code != "invalid_transition"

    runtime.state.transition("session2", SessionPhase.RUNNING)
    mid_run = runtime.run_job({"message": "turn", "session_id": "session2", "wait": False})
    assert mid_run.ok is False
    assert mid_run.error.code == "invalid_transition"

    runtime.state.transition("session3", SessionPhase.SUCCEEDED)
    next_turn = runtime.run_job({"message": "turn two", "session_id": "session3", "wait": False})
    assert next_turn.error is None or next_turn.error.code != "invalid_transition"

    runtime.state.transition("session4", SessionPhase.NEEDS_USER)
    blocked = runtime.run_job({"message": "turn", "session_id": "session4", "wait": False})
    assert blocked.ok is False
    assert blocked.error.code == "invalid_transition"
