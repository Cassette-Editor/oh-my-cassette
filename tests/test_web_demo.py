from __future__ import annotations

import json

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
        data={"session_id": session_id},
        files=[("files", ("clip.mp4", b"video", "video/mp4"))],
    )

    assert response.status_code == 200
    assets = client.get(f"/api/assets?session_id={session_id}").json()
    assert len(assets["data"]["manifest"]["assets"]) == 1
    events = client.get(f"/api/events?session_id={session_id}&after=0").json()["events"]
    assert any("已保存素材" in event.get("text", "") for event in events)
