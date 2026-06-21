from __future__ import annotations

import json
import time

import pytest

from cassette import jobs, notifier, tools
from web_demo import deepseek_client, session_store


def test_web_notifier_writes_gateway_text_to_outbox():
    session_store.reset_all()
    session_id = "web_outbox_test"
    session_store.ensure_session(session_id)

    result = notifier.notify_gateway_text({"platform": "web", "chat_id": session_id}, "hello web", reason="test")

    assert result["status"] == "sent"
    events = session_store.get_events(session_id)
    assert events[-1]["text"] == "hello web"
    assert events[-1]["kind"] == "test"


def test_web_run_job_uses_gateway_background_path(cassette_env, monkeypatch):
    session_id = "web_run_job_live"
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    tools.cassette_ingest_media({
        "source_path": str(media),
        "session_id": session_id,
        "platform": "web",
        "chat_id": session_id,
        "user_id": session_id,
    })
    observed = {}

    def fake_start(job):
        observed["delivery"] = dict(job.get("delivery") or {})
        job["status"] = "running"
        job["started_at"] = jobs.now_iso()
        job["worker_kind"] = "thread"
        jobs.save_job(job)
        return job

    monkeypatch.setattr(tools, "_start_inprocess_cassette_job", fake_start)
    payload = json.loads(tools.cassette_run_job({
        "prompt": "internal",
        "chat_message": "请剪成 10 秒",
        "session_id": session_id,
        "wait": True,
    }))

    assert payload["ok"] is True
    assert payload["data"]["background"] is True
    assert observed["delivery"]["platform"] == "web"
    assert observed["delivery"]["chat_id"] == session_id


def test_deepseek_tool_arguments_are_forced_to_current_web_session(cassette_env):
    session_a = "web_scope_a"
    session_b = "web_scope_b"
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    tools.cassette_ingest_media({"source_path": str(media), "session_id": session_a})

    result = json.loads(deepseek_client._execute_tool(
        session_a,
        "cassette_list_assets",
        json.dumps({"session_id": session_b}),
    ))

    assert result["ok"] is True
    manifest_data = result["data"]["manifest"]
    assert manifest_data["session_hash"] == tools.manifest.resolve_session_hash(session_id=session_a)
    assert len(manifest_data["assets"]) == 1


def test_deepseek_tool_execution_logs_error_code(cassette_env, monkeypatch):
    del cassette_env
    captured = []
    monkeypatch.setattr(
        deepseek_client.logging_utils,
        "log_event",
        lambda event, **fields: captured.append((event, fields)),
    )

    payload = json.loads(deepseek_client._execute_tool(
        "web_bgm_log",
        "cassette_match_exact_bgm",
        json.dumps({"instruction": "剪一个美食视频"}),
    ))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_required_arg"
    assert captured[-1][0] == "deepseek_tool_executed"
    assert captured[-1][1]["tool"] == "cassette_match_exact_bgm"
    assert captured[-1][1]["ok"] is False
    assert captured[-1][1]["error_code"] == "missing_required_arg"


def test_web_flow_cancelled_token_stays_cancelled_after_new_flow():
    session_store.reset_all()
    session_id = "web_cancel_token"
    session_store.ensure_session(session_id)

    first_token = session_store.begin_flow(session_id, "llm")
    assert first_token
    assert session_store.cancel_flow(session_id) is True
    second_token = session_store.begin_flow(session_id, "llm")

    assert second_token
    assert second_token != first_token
    assert session_store.is_flow_cancelled(session_id, first_token) is True
    assert session_store.is_flow_cancelled(session_id, second_token) is False
    session_store.end_flow(session_id, first_token)
    assert session_store.is_flow_active(session_id) is True
    session_store.end_flow(session_id, second_token)
    assert session_store.is_flow_active(session_id) is False


def test_deepseek_mock_tool_loop_starts_web_job(cassette_env, monkeypatch):
    session_store.reset_all()
    session_id = "web_deepseek_loop"
    session_store.ensure_session(session_id)
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    tools.cassette_ingest_media({
        "source_path": str(media),
        "session_id": session_id,
        "platform": "web",
        "chat_id": session_id,
        "user_id": session_id,
    })

    calls = iter([
        {
            "id": "call_list",
            "type": "function",
            "function": {"name": "cassette_list_assets", "arguments": "{}"},
        },
        {
            "id": "call_prompt",
            "type": "function",
            "function": {"name": "cassette_make_prompt", "arguments": json.dumps({"instruction": "剪成 10 秒"})},
        },
        {
            "id": "call_run",
            "type": "function",
            "function": {
                "name": "cassette_run_job",
                "arguments": json.dumps({"prompt": "internal", "chat_message": "请剪成 10 秒", "wait": True}),
            },
        },
    ])
    observed = {}

    def fake_post(messages, api_key):
        del api_key
        try:
            tool_call = next(calls)
        except StopIteration:
            return {"choices": [{"message": {"content": "Cassette 任务已开始。"}}]}
        return {"choices": [{"message": {"content": None, "tool_calls": [tool_call]}}]}

    def fake_start(job):
        observed["job"] = job
        job["status"] = "running"
        job["started_at"] = jobs.now_iso()
        job["worker_kind"] = "thread"
        jobs.save_job(job)
        return job

    monkeypatch.setattr(deepseek_client, "_post_chat_completion", fake_post)
    monkeypatch.setattr(tools, "_start_inprocess_cassette_job", fake_start)

    result = deepseek_client.run_turn(session_id, "请开始剪辑", api_key_override="test-key")

    assert result["tool_call_count"] == 3
    assert observed["job"]["cassette_session_id"] == session_id
    assert observed["job"]["delivery"]["platform"] == "web"
    assert session_store.get_events(session_id)[-1]["text"] == "Cassette 任务已开始。"


def test_deepseek_runtime_env_uses_process_env_only(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=from-hermes-file\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_ENV_FILE", str(env_file))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert deepseek_client.api_key_from_runtime() == ""

    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-process-env")
    assert deepseek_client.api_key_from_runtime() == "from-process-env"


def test_web_api_upload_records_assets(cassette_env):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    session_id = client.post("/api/sessions").json()["session_id"]
    response = client.post(
        "/api/uploads",
        data={"session_id": session_id, "client_event_id": "local-upload-test"},
        files=[("files", ("clip.mp4", b"video", "video/mp4"))],
    )

    assert response.status_code == 200
    assets = client.get(f"/api/assets?session_id={session_id}").json()
    assert len(assets["data"]["manifest"]["assets"]) == 1
    events = client.get(f"/api/events?session_id={session_id}&after=0").json()["events"]
    assert any("已保存素材" in event.get("text", "") for event in events)
    assert any(event.get("client_event_id") == "local-upload-test" for event in events)


def test_web_api_language_switch_changes_local_reply(cassette_env):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    session_id = client.post("/api/sessions").json()["session_id"]
    language_response = client.post(f"/api/sessions/{session_id}/language", json={"language": "en"})
    message_response = client.post("/api/messages", json={"session_id": session_id, "text": "hello", "language": "en"})

    assert language_response.status_code == 200
    assert language_response.json()["language"] == "en"
    assert message_response.status_code == 200
    events = client.get(f"/api/events?session_id={session_id}&after=0").json()["events"]
    assert any("Please upload video" in event.get("text", "") for event in events)


def test_web_api_rewrite_runs_deepseek_in_background(cassette_env, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    session_id = client.post("/api/sessions").json()["session_id"]

    def fake_ingest(event, gateway):
        del event, gateway
        return {"action": "rewrite", "reason": "test_rewrite", "text": "internal prompt"}

    def fake_run_turn(run_session_id, prompt_text, *, api_key_override="", flow_token=None):
        assert run_session_id == session_id
        assert prompt_text == "internal prompt"
        assert api_key_override == ""
        assert flow_token
        session_store.add_event(session_id, role="assistant", text="background done", kind="message")
        return {"content": "background done", "tool_call_count": 0}

    monkeypatch.setattr(tools, "ingest_gateway_media", fake_ingest)
    monkeypatch.setattr(deepseek_client, "run_turn", fake_run_turn)

    response = client.post("/api/messages", json={"session_id": session_id, "text": "/edit add captions"})

    assert response.status_code == 200
    assert response.json()["action"] == "llm_background"
    deadline = time.time() + 3
    events = []
    while time.time() < deadline:
        events = client.get(f"/api/events?session_id={session_id}&after=0").json()["events"]
        if any(event.get("text") == "background done" for event in events):
            break
        time.sleep(0.05)
    assert any(event.get("text") == "正在提交任务" for event in events)
    assert any(event.get("text") == "background done" for event in events)


def test_web_rejects_message_while_llm_flow_active(cassette_env, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo import server as web_server

    session_store.reset_all()
    client = TestClient(web_server.app)
    session_id = client.post("/api/sessions").json()["session_id"]

    monkeypatch.setattr(tools, "ingest_gateway_media", lambda event, gateway: {"action": "rewrite", "reason": "test", "text": "internal"})
    monkeypatch.setattr(web_server, "_submit_llm_background", lambda session_id, prompt_text, api_key_override, flow_token: None)

    first = client.post("/api/messages", json={"session_id": session_id, "text": "/edit first"})
    second = client.post("/api/messages", json={"session_id": session_id, "text": "再剪一个版本"})

    assert first.status_code == 200
    assert first.json()["action"] == "llm_background"
    assert second.status_code == 200
    assert second.json()["result"]["reason"] == "web_session_flow_busy"
    events = client.get(f"/api/events?session_id={session_id}&after=0").json()["events"]
    assert [event["role"] for event in events[:3]] == ["user", "assistant", "user"]
    assert events[1]["text"] == "正在提交任务"
    assert events[-1]["text"] == "请使用/cut命令终止当前流程或剪辑任务后再尝试开始新的剪辑任务"


def test_web_cut_clears_pending_llm_flow(cassette_env, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo import server as web_server

    session_store.reset_all()
    client = TestClient(web_server.app)
    session_id = client.post("/api/sessions").json()["session_id"]

    monkeypatch.setattr(tools, "ingest_gateway_media", lambda event, gateway: {"action": "rewrite", "reason": "test", "text": "internal"})
    monkeypatch.setattr(web_server, "_submit_llm_background", lambda session_id, prompt_text, api_key_override, flow_token: None)

    client.post("/api/messages", json={"session_id": session_id, "text": "/edit first"})
    assert session_store.is_flow_active(session_id) is True

    cut = client.post("/api/messages", json={"session_id": session_id, "text": "/cut"})

    assert cut.status_code == 200
    assert cut.json()["result"]["reason"] == "web_cut_requested"
    assert session_store.is_flow_active(session_id) is False
    events = client.get(f"/api/events?session_id={session_id}&after=0").json()["events"]
    assert any("已请求停止当前 Cassette 流程或剪辑任务" in event.get("text", "") for event in events)


def test_web_rejects_message_while_job_active(cassette_env):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    session_id = client.post("/api/sessions").json()["session_id"]
    session_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    job = jobs.create_job(
        session_hash,
        "prompt",
        "instruction",
        [],
        {"cassette_session_id": session_id, "delivery": {"platform": "web", "chat_id": session_id}},
    )
    job["status"] = "running"
    jobs.save_job(job)

    response = client.post("/api/messages", json={"session_id": session_id, "text": "再剪一个版本"})

    assert response.status_code == 200
    assert response.json()["result"]["reason"] == "web_session_flow_busy"
    events = client.get(f"/api/events?session_id={session_id}&after=0").json()["events"]
    assert events[-1]["job_id"] == job["job_id"]
    assert events[-1]["text"] == "请使用/cut命令终止当前流程或剪辑任务后再尝试开始新的剪辑任务"


def test_web_skip_choice_reply_is_bridged_to_llm_history(cassette_env, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    session_id = client.post("/api/sessions").json()["session_id"]
    session_store.set_llm_messages(session_id, [{"role": "assistant", "content": "请选择当前 Cassette 会话使用的模型，回复序号即可："}])

    def fake_ingest(event, gateway):
        gateway.adapters["web"].send(event.source.chat_id, "已选择模型：DeepSeek V4 Flash。请选择思考程度，回复序号即可：")
        return {"action": "skip", "reason": "cassette_model_thinking_choice_requested", "reply_sent": True}

    monkeypatch.setattr(tools, "ingest_gateway_media", fake_ingest)

    response = client.post("/api/messages", json={"session_id": session_id, "text": "1"})

    assert response.status_code == 200
    assert response.json()["action"] == "skip"
    history = session_store.get_llm_messages(session_id)
    assert history[-2:] == [
        {"role": "user", "content": "1"},
        {"role": "assistant", "content": "已选择模型：DeepSeek V4 Flash。请选择思考程度，回复序号即可："},
    ]


def test_web_jobs_expose_owned_job_log(cassette_env):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    session_id = client.post("/api/sessions").json()["session_id"]
    session_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    output_path = tools.manifest.get_asset_root() / "exports" / "web-job-log.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"export")
    prompt = "SECRET INTERNAL PROMPT THAT MUST NOT APPEAR"
    job = jobs.create_job(
        session_hash,
        prompt,
        "add captions",
        [],
        {"cassette_session_id": session_id, "delivery": {"platform": "web", "chat_id": session_id}},
    )
    job["status"] = "running"
    job["current_stage"] = "editing"
    job["progress_events"] = [{"status": "running", "summary": "Cassette is editing the timeline."}]
    job["browser_events"] = [{"event": "click", "summary": "Clicked export button"}]
    job["errors"] = [{"code": "sample_warning", "message": "diagnostic only"}]
    job["outputs"] = [{"local_path": str(output_path), "label": "export"}]
    jobs.save_job(job)

    jobs_response = client.get(f"/api/jobs?session_id={session_id}")

    assert jobs_response.status_code == 200
    visible_jobs = jobs_response.json()["data"]["jobs"]
    assert [visible["job_id"] for visible in visible_jobs] == [job["job_id"]]
    assert visible_jobs[0]["log_url"].endswith(f"/log?session_id={session_id}")
    log_response = client.get(visible_jobs[0]["log_url"])
    assert log_response.status_code == 200
    log_text = log_response.text
    assert job["job_id"] in log_text
    assert "prompt_redacted: <redacted:" in log_text
    assert prompt not in log_text
    assert "[progress_events]" in log_text
    assert "Cassette is editing the timeline." in log_text
    assert "[browser_events]" in log_text
    assert "web-job-log.mp4" in log_text
    assert str(output_path) not in log_text


def test_web_jobs_filter_and_reject_non_web_jobs(cassette_env):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    session_id = "web_job_boundary"
    session_store.ensure_session(session_id)
    session_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    web_job = jobs.create_job(
        session_hash,
        "web prompt",
        "web instruction",
        [],
        {"cassette_session_id": session_id, "delivery": {"platform": "web", "chat_id": session_id}},
    )
    telegram_job = jobs.create_job(
        session_hash,
        "telegram prompt",
        "telegram instruction",
        [],
        {"cassette_session_id": session_id, "delivery": {"platform": "telegram", "chat_id": "telegram-chat"}},
    )

    jobs_response = client.get(f"/api/jobs?session_id={session_id}&limit=10")
    non_web_log_response = client.get(f"/api/jobs/{telegram_job['job_id']}/log?session_id={session_id}")
    non_web_cancel_response = client.post(f"/api/jobs/{telegram_job['job_id']}/cancel", json={"session_id": session_id})

    assert jobs_response.status_code == 200
    visible_ids = [job["job_id"] for job in jobs_response.json()["data"]["jobs"]]
    assert visible_ids == [web_job["job_id"]]
    assert "log_url" in jobs_response.json()["data"]["jobs"][0]
    assert non_web_log_response.status_code == 403
    assert non_web_cancel_response.status_code == 403


def test_web_jobs_reconcile_stale_running_web_job(cassette_env):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    session_id = client.post("/api/sessions").json()["session_id"]
    session_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    web_job = jobs.create_job(
        session_hash,
        "web prompt",
        "web instruction",
        [],
        {"cassette_session_id": session_id, "delivery": {"platform": "web", "chat_id": session_id}, "timeout_sec": 1},
    )
    web_job["status"] = "running"
    web_job["started_at"] = "2020-01-01T00:00:00Z"
    web_job["current_stage"] = "upload"
    jobs.save_job(web_job)
    non_web_job = jobs.create_job(
        session_hash,
        "telegram prompt",
        "telegram instruction",
        [],
        {"cassette_session_id": session_id, "delivery": {"platform": "telegram", "chat_id": "telegram-chat"}, "timeout_sec": 1},
    )
    non_web_job["status"] = "running"
    non_web_job["started_at"] = "2020-01-01T00:00:00Z"
    jobs.save_job(non_web_job)

    response = client.get(f"/api/jobs?session_id={session_id}&limit=10")

    assert response.status_code == 200
    visible_jobs = response.json()["data"]["jobs"]
    assert [job["job_id"] for job in visible_jobs] == [web_job["job_id"]]
    assert visible_jobs[0]["status"] == "timed_out"
    saved_web_job = jobs.load_job(web_job["job_id"])
    saved_non_web_job = jobs.load_job(non_web_job["job_id"])
    assert saved_web_job["status"] == "timed_out"
    assert saved_web_job["errors"][-1]["code"] == "web_demo_job_timeout"
    assert saved_non_web_job["status"] == "running"
    events = client.get(f"/api/events?session_id={session_id}&after=0").json()["events"]
    assert any(event.get("job_id") == web_job["job_id"] and event.get("kind") == "error" for event in events)


def test_web_session_creation_cleans_previous_web_session(cassette_env):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    old_session = client.post("/api/sessions").json()["session_id"]
    upload_response = client.post(
        "/api/uploads",
        data={"session_id": old_session},
        files=[("files", ("clip.mp4", b"video", "video/mp4"))],
    )
    assert upload_response.status_code == 200
    session_hash = tools.manifest.resolve_session_hash(session_id=old_session)
    upload_dir = tools.manifest.get_asset_root() / "web_uploads" / old_session
    session_dir = tools.manifest.get_session_dir(session_hash)
    output_path = tools.manifest.get_asset_root() / "exports" / "web-cleanup-output.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"export")
    job = jobs.create_job(
        session_hash,
        "prompt",
        "instruction",
        [],
        {"cassette_session_id": old_session, "delivery": {"platform": "web", "chat_id": old_session}},
    )
    job["status"] = "succeeded"
    job["outputs"] = [{"local_path": str(output_path)}]
    jobs.save_job(job)
    job_path = jobs.get_jobs_dir() / f"{job['job_id']}.json"

    response = client.post("/api/sessions", json={"cleanup_session_id": old_session, "language": "en"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] != old_session
    assert payload["language"] == "en"
    assert payload["cleanup"]["ok"] is True
    assert not upload_dir.exists()
    assert not session_dir.exists()
    assert not output_path.exists()
    assert not job_path.exists()
    assert client.get(f"/api/events?session_id={old_session}&after=0").status_code == 400


def test_web_cleanup_ignores_non_web_jobs(cassette_env):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    session_id = "web_boundary_cleanup"
    session_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    output_path = tools.manifest.get_asset_root() / "exports" / "non-web-output.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"export")
    job = jobs.create_job(
        session_hash,
        "prompt",
        "instruction",
        [],
        {"cassette_session_id": session_id, "delivery": {"platform": "telegram", "chat_id": "telegram-chat"}},
    )
    job["status"] = "succeeded"
    job["outputs"] = [{"local_path": str(output_path)}]
    jobs.save_job(job)
    job_path = jobs.get_jobs_dir() / f"{job['job_id']}.json"

    response = client.post(f"/api/sessions/{session_id}/cleanup")

    assert response.status_code == 200
    assert job_path.exists()
    assert output_path.exists()


def test_web_cleanup_cancels_active_web_job_without_deleting_record(cassette_env):
    fastapi = pytest.importorskip("fastapi")
    del fastapi
    from fastapi.testclient import TestClient
    from web_demo.server import app

    session_store.reset_all()
    client = TestClient(app)
    session_id = "web_active_cleanup"
    session_hash = tools.manifest.resolve_session_hash(session_id=session_id)
    job = jobs.create_job(
        session_hash,
        "prompt",
        "instruction",
        [],
        {"cassette_session_id": session_id, "delivery": {"platform": "web", "chat_id": session_id}},
    )
    job["status"] = "running"
    jobs.save_job(job)
    job_path = jobs.get_jobs_dir() / f"{job['job_id']}.json"

    response = client.post(f"/api/sessions/{session_id}/cleanup")

    assert response.status_code == 200
    assert job_path.exists()
    assert jobs.load_job(job["job_id"])["status"] == "cancel_requested"
