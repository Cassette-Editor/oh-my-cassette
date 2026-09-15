"""End-to-end through the MCP client against the fake backend."""

from __future__ import annotations

import json

import pytest
from tests.fake_cassette import ScriptedRun, text_clip

from oh_my_cassette.server import TOOL_NAMES

pytestmark = pytest.mark.anyio


async def test_lists_exactly_the_public_tools(client):
    mcp_client, _ = client
    tools = await mcp_client.list_tools()
    assert tuple(t.name for t in tools.tools) == TOOL_NAMES
    for tool in tools.tools:
        assert tool.description and len(tool.description) > 40
        assert tool.output_schema is not None


async def test_create_project_run_and_timeline(client, fake):
    mcp_client, app = client
    created = await mcp_client.call_tool("cassette_project", {"action": "create", "title": "demo"})
    assert not created.is_error
    body = created.structured_content
    assert body["status"] == "ok" and body["project_id"].startswith("try-session-")
    assert body["editor_url"].startswith("http://fake.web/try?projectSessionId=")

    fake.script(
        ScriptedRun(
            events=[
                ("tool_call_started", {"toolCallId": "c1", "toolName": "timeline_edit", "argsHash": "h"}),
                ("project_commit", {"label": "add title"}),
                ("run_terminal", {"status": "completed", "finalText": "Added a title."}),
            ],
            commit_clips=[text_clip("clip_title", "Hello", 0, 90)],
        )
    )
    run = await mcp_client.call_tool("cassette_run", {"message": "add a title that says Hello"})
    assert not run.is_error, run.content
    out = run.structured_content
    assert out["status"] == "completed"
    assert out["final_text"] == "Added a title."
    assert (out["version_from"], out["version_to"]) == (0, 1)
    assert "clip_title" in out["timeline"] and 'text "Hello"' in out["timeline"]
    assert out["tool_calls"] == ["timeline_edit"]
    assert out["committed_versions"] == [1]

    timeline = await mcp_client.call_tool("cassette_timeline", {})
    assert timeline.structured_content["version"] == 1
    assert "v1 ·" in timeline.structured_content["timeline"]

    # the start request carried the verbatim message and a stable idempotency key
    start = next(c for c in fake.command_log if c["command"]["type"] == "start")
    assert start["command"]["request"]["message"]["content"] == "add a title that says Hello"
    assert start["command"]["request"]["idempotencyKey"].startswith("omc-")
    assert start["command"]["request"]["mediaSessionId"] == body["project_id"]
    assert json.loads(run.content[0].text)["status"] == "completed"
