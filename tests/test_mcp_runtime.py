from __future__ import annotations

import json

import runtime_config
from cassette.core import jobs, tools
from mcp_plugin.models import SessionPhase
from mcp_plugin.runtime import LocalMcpRuntime


def _runtime(tmp_path, monkeypatch):
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


def test_api_auth_token_satisfies_mcp_preflight_and_is_redacted(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    runtime_config.credentials_path().unlink()
    monkeypatch.setenv("CASSETTE_AUTH_TOKEN", "private-pre-issued-token")

    assert runtime._auth_error(session_id="session") is None
    assert "private-pre-issued-token" not in runtime._redact({"debug": "using private-pre-issued-token"})["debug"]


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


def test_the_auth_gate_checks_only_that_credentials_exist(tmp_path, monkeypatch):
    # The plugin serves agent accounts, and every tool it exposes — export included — is an
    # operation they are entitled to. This seeds the access-level field an older setup wrote,
    # at its most restrictive, to pin that no gate reads it back: an account that is configured
    # is never turned away here.
    runtime = _runtime(tmp_path, monkeypatch)
    runtime_config.write_protected_json(
        runtime_config.credentials_path(),
        {"email": "private@example.test", "password": "private-password", "full_api_access": False},
    )
    result = runtime.run_job({"prompt": "edit", "session_id": "session", "wait": False})
    assert result.ok is False
    # It fails for the ordinary reason instead: no session has been opened yet.
    assert result.error.code == "invalid_transition"


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


def test_detached_worker_can_import_root_level_runtime_config(tmp_path):
    """The detached worker must read the MCP protected config, not ~/.hermes/.env.

    Python seeds sys.path with core/, so without the worker's own fix `import
    runtime_config` fails, api_transport._env swallows it, and credentials silently
    come from the legacy Hermes dotenv — a reset password then appears to do nothing.
    """
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    probe = tmp_path / "probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            import json, runpy, sys
            # Reproduce the detached spawn: only core/ on the path, exactly as
            # subprocess.Popen([python, ".../core/worker.py"]) leaves it.
            sys.path[:] = [r{str(root / "core")!r}] + [p for p in sys.path if p not in (r{str(root)!r}, "")]
            runpy.run_path(r{str(root / "core" / "worker.py")!r}, run_name="not_main")
            import runtime_config
            print(json.dumps({{"ok": True, "has_mcp_env_value": hasattr(runtime_config, "mcp_env_value")}}))
            """
        ).strip()
    )
    proc = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"worker bootstrap failed:\n{proc.stderr}"
    assert "ModuleNotFoundError" not in proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] and payload["has_mcp_env_value"]


def test_job_change_marker_ignores_bare_updated_at_bumps():
    """A write that only moves updated_at is not a change worth waking the long poll for."""
    from mcp_plugin.runtime import LocalMcpRuntime

    base = {"status": "running", "current_stage": "editing", "updated_at": "2026-07-24T07:20:00Z"}
    touched = {**base, "updated_at": "2026-07-24T07:20:06Z"}
    real = {**base, "current_stage": "rendering"}

    marker = LocalMcpRuntime._job_change_marker
    assert marker(touched) == marker(base), "heartbeat-only write must not count as a change"
    assert marker(real) != marker(base), "a real stage change must still wake the poll"


def test_slim_unsettled_job_drops_bulk_but_keeps_progress():
    """Mid-render the agent keeps its bearings; the deliverables are dropped until it settles."""
    from mcp_plugin.runtime import LocalMcpRuntime

    payload = {
        "data": {
            "job": {
                "job_id": "j1",
                "status": "running",
                "current_stage": "editing",
                "plan_progress": [{"step": 3}],
                "editor_url": "https://example/editor",
                "progress_events": [{"summary": "x"} for _ in range(10)],
                "timeline_delta": {"clips": list(range(24))},
                "report": {"lines": ["a"]},
                "chat_message": "long agent prose",
                "message": "long agent prose",
                "stage_timings": {"editing": 42},
                "quality": {"current_stage": "editing", "progress_summary": "8/12", "timeline_ctl": "BIG"},
            }
        }
    }
    LocalMcpRuntime._slim_unsettled_job(payload)
    job = payload["data"]["job"]

    for dropped in ("progress_events", "timeline_delta", "report", "chat_message", "message", "stage_timings"):
        assert dropped not in job, f"{dropped} should not survive a mid-render poll"
    assert job["quality"] == {"current_stage": "editing", "progress_summary": "8/12"}
    for kept in ("job_id", "status", "current_stage", "plan_progress", "editor_url"):
        assert kept in job, f"{kept} is how the agent reports progress"


def test_slim_unsettled_job_tolerates_missing_or_odd_payloads():
    from mcp_plugin.runtime import LocalMcpRuntime

    for payload in ({}, {"data": None}, {"data": {}}, {"data": {"job": "not-a-dict"}}):
        LocalMcpRuntime._slim_unsettled_job(payload)  # must not raise


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


def test_mcp_ingest_mints_agent_session_ids(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    captured: dict = {}

    def fake_invoke(tool, args, *, session_id, roots):
        captured["session_id"] = session_id
        return {"ok": True, "data": {}}

    monkeypatch.setattr(runtime, "_invoke_core", fake_invoke)
    envelope = runtime.ingest_media({"source_path": "unused"}, roots=[])
    assert envelope.ok
    assert captured["session_id"].startswith("agent-session-")
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


def test_stale_password_failure_carries_the_reset_password_command(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    result = runtime._envelope_from_core(
        {"ok": False, "error": {"code": "auth_failed", "message": "Cassette sign-in failed (HTTP 401)"}},
        session_id="session",
    )
    assert result.ok is False
    assert result.error.details["reset_password_command"].endswith("setup_local_mcp.py --reset-password")
    # The setup script cannot see the host's environment, so only this side can tell the user
    # whether to rewrite the private config or fix env vars.
    assert result.error.details["credential_source"] == "local_config"


def test_core_auth_failure_also_carries_the_reset_password_command(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    result = runtime._envelope_from_core(
        {"ok": False, "error": {"code": "cassette_auth_failed", "message": "login timed out"}},
        session_id="session",
    )
    assert result.error.details["reset_password_command"].endswith("--reset-password")


def test_failed_job_status_surfaces_the_reset_password_command(tmp_path, monkeypatch):
    # cassette_run_job defaults to wait=False and cassette_job_status reports even a failed job
    # as ok=True, so _failure never runs here. This is the ordinary way a user meets a dead
    # password, and without the annotation it is the one path offering no way out.
    runtime = _runtime(tmp_path, monkeypatch)
    job = jobs.create_job(
        session_hash="hash",
        prompt="edit",
        instruction=None,
        asset_paths=[],
        options={"cassette_session_id": "session"},
    )
    jobs.update_job(
        job["job_id"],
        status="failed",
        errors=[{"code": "auth_failed", "message": "Cassette sign-in failed (HTTP 401)"}],
    )

    result = runtime.job_status({"job_id": job["job_id"], "limit": 10})

    assert result.ok is True
    errors = result.data["job"]["errors"]
    assert errors[-1]["details"]["reset_password_command"].endswith("--reset-password")


def test_successful_job_status_is_not_annotated(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    job = jobs.create_job(
        session_hash="hash",
        prompt="edit",
        instruction=None,
        asset_paths=[],
        options={"cassette_session_id": "session"},
    )
    jobs.update_job(job["job_id"], status="running")

    result = runtime.job_status({"job_id": job["job_id"], "limit": 10})

    assert result.ok is True
    assert result.data["job"].get("errors") in (None, [])
