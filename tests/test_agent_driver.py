"""Run driver scenarios: questions, answers, stop, conflicts, dropped streams, timeouts."""

from __future__ import annotations

import pytest
from tests.fake_cassette import ScriptedRun, text_clip

from oh_my_cassette.cassette.errors import HttpError

pytestmark = pytest.mark.anyio

QUESTION = {
    "interruptId": "call_q1",
    "kind": "ask_user",
    "payload": {
        "question": "Which storyline should the montage extend?",
        "reason": "Two storylines fit.",
        "choices": [{"id": "main", "label": "Main storyline"}, {"id": "alt", "label": "Alternate"}],
        "allowsFreeText": False,
        "toolCallId": "call_q1",
    },
}


async def _create(mcp_client):
    created = await mcp_client.call_tool("cassette_project", {"action": "create"})
    assert not created.is_error
    return created.structured_content["project_id"]


async def test_needs_input_then_answer(client, fake):
    mcp_client, app = client
    await _create(mcp_client)
    fake.script(ScriptedRun(events=[("interrupt_created", QUESTION)]))
    run = await mcp_client.call_tool("cassette_run", {"message": "make a montage"})
    out = run.structured_content
    assert out["status"] == "needs_input"
    assert out["question"]["choices"][1]["id"] == "alt"
    assert out["question"]["allows_free_text"] is False
    assert "cassette_answer" in out["next"]

    # a new request while a question is pending is refused with the question repeated
    again = await mcp_client.call_tool("cassette_run", {"message": "something else"})
    assert again.structured_content["status"] == "needs_input"
    assert "note" in again.structured_content

    bad = await mcp_client.call_tool("cassette_answer", {"choice_id": "nope"})
    assert bad.structured_content["status"] == "failed"
    assert bad.structured_content["error"]["code"] == "invalid_answer"

    fake.sessions[out_session_id(fake)].pending_after_resume = [
        ("project_commit", {"label": "montage"}),
        ("run_terminal", {"status": "completed", "finalText": "done"}),
    ]
    fake.runs[out["run_id"]]["_scripted"].commit_clips = [text_clip("c1", "Main", 0, 30)]
    answered = await mcp_client.call_tool("cassette_answer", {"choice_id": "main"})
    result = answered.structured_content
    assert result["status"] == "completed"
    assert result["version_to"] == 1 and result["committed_versions"] == [1]
    resume = [c for c in fake.command_log if c["command"]["type"] == "resume"][-1]
    assert resume["command"]["response"] == {"selectedChoiceId": "main", "freeText": None}


def out_session_id(fake):
    return next(iter(fake.sessions))


async def test_decline_lets_the_agent_continue(client, fake):
    mcp_client, _ = client
    await _create(mcp_client)
    fake.script(ScriptedRun(events=[("interrupt_created", QUESTION)]))
    run = await mcp_client.call_tool("cassette_run", {"message": "montage"})
    assert run.structured_content["status"] == "needs_input"
    declined = await mcp_client.call_tool("cassette_answer", {"decline": True})
    assert declined.structured_content["status"] == "completed"
    resume = next(c for c in fake.command_log if c["command"]["type"] == "resume")
    assert resume["command"]["response"] == {"action": "cancel"}


async def test_stop_keeps_committed_edits(client, fake):
    mcp_client, _ = client
    await _create(mcp_client)
    fake.script(
        ScriptedRun(
            events=[("project_commit", {"label": "first"})], commit_clips=[text_clip("c1", "A", 0, 30)]
        )
    )
    running = await mcp_client.call_tool("cassette_run", {"message": "go", "wait_sec": 1})
    assert running.structured_content["status"] == "timeout"
    assert "cassette_status" in running.structured_content["next"]
    stopped = await mcp_client.call_tool("cassette_stop", {})
    out = stopped.structured_content
    assert out["status"] == "aborted"
    assert out["version_to"] == 1  # the commit before the stop survives
    stop = next(c for c in fake.command_log if c["command"]["type"] == "stop")
    assert stop["command"]["reason"] == "user_stop"


async def test_status_reattaches_to_a_running_run(client, fake):
    mcp_client, _ = client
    await _create(mcp_client)
    fake.script(
        ScriptedRun(
            events=[("tool_call_started", {"toolCallId": "t", "toolName": "read_resource", "argsHash": "h"})]
        )
    )
    first = await mcp_client.call_tool("cassette_run", {"message": "go", "wait_sec": 1})
    assert first.structured_content["status"] == "timeout"
    run_id = first.structured_content["run_id"]
    peek = await mcp_client.call_tool("cassette_status", {"wait_sec": 0})
    assert peek.structured_content["status"] == "running"
    fake.finish_run(
        run_id,
        [
            (
                "run_terminal",
                {
                    "status": "not_done",
                    "finalText": "gave up",
                    "terminalReason": "resource_exhausted",
                    "budget": {"name": "model_calls", "limit": 10, "observed": 10},
                },
            )
        ],
    )
    followed = await mcp_client.call_tool("cassette_status", {})
    out = followed.structured_content
    assert out["status"] == "not_done" and out["terminal_reason"] == "resource_exhausted"
    assert out["budget"]["name"] == "model_calls"
    assert out["last_run_id"] == run_id
    idle = await mcp_client.call_tool("cassette_status", {})
    assert idle.structured_content["status"] == "idle"
    assert idle.structured_content["last_run"]["status"] == "not_done"


async def test_control_revision_conflict_is_retried_once(client, fake):
    mcp_client, _ = client
    await _create(mcp_client)
    fake.force_conflict_once = True
    run = await mcp_client.call_tool("cassette_run", {"message": "hi", "mode": "chat"})
    assert run.structured_content["status"] == "completed"
    starts = [c for c in fake.command_log if c["command"]["type"] == "start"]
    assert len(starts) == 2 and starts[1]["expectedRevision"] == starts[0]["expectedRevision"] + 1


async def test_dropped_stream_recovers_from_the_run_row(client, fake):
    mcp_client, _ = client
    await _create(mcp_client)
    fake.script(
        ScriptedRun(
            events=[
                ("project_commit", {"label": "x"}),
                ("run_terminal", {"status": "completed", "finalText": "ok"}),
            ],
            commit_clips=[text_clip("c1", "A", 0, 30)],
            close_stream_early=True,
        )
    )
    run = await mcp_client.call_tool("cassette_run", {"message": "go"})
    out = run.structured_content
    assert out["status"] == "completed" and out["committed_versions"] == [1]


async def test_run_failed_carries_error(client, fake):
    mcp_client, _ = client
    await _create(mcp_client)
    fake.script(
        ScriptedRun(
            events=[
                (
                    "run_failed",
                    {
                        "errorCode": "provider_unavailable",
                        "message": "upstream down",
                        "retryable": True,
                        "assistantMessageId": None,
                        "failureArtifactId": None,
                    },
                )
            ]
        )
    )
    run = await mcp_client.call_tool("cassette_run", {"message": "go"})
    out = run.structured_content
    assert out["status"] == "failed"
    assert out["error"] == {"code": "provider_unavailable", "message": "upstream down", "retryable": True}


async def test_model_and_effort_validation(client, fake):
    mcp_client, _ = client
    await _create(mcp_client)
    bad = await mcp_client.call_tool("cassette_run", {"message": "go", "model": "gpt-9"})
    assert (
        bad.structured_content["status"] == "failed"
        and bad.structured_content["error"]["code"] == "bad_request"
    )
    ok = await mcp_client.call_tool(
        "cassette_run", {"message": "go", "model": "sol", "effort": "high", "unattended": True}
    )
    assert ok.structured_content["status"] == "completed"
    start = next(c for c in fake.command_log if c["command"]["type"] == "start")["command"]["request"]
    assert start["selectedModelId"] == "openai/gpt-5.6-sol"
    assert start["selectedReasoningEffort"] == "high"
    assert start["interactionPolicy"] == "non_interactive"


async def test_unauthorized_backend_surfaces_a_typed_error(app, fake):
    fake.auth_required = True
    with pytest.raises(HttpError) as exc:
        await app.create_project("x")
    assert exc.value.status == 401 and exc.value.code == "unauthorized"
