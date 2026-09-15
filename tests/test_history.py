from __future__ import annotations

import pytest
from tests.fake_cassette import ScriptedRun, text_clip

pytestmark = pytest.mark.anyio


async def test_undo_redo_and_restore(client, fake):
    mcp_client, _ = client
    await mcp_client.call_tool("cassette_project", {"action": "create"})
    at_start = await mcp_client.call_tool("cassette_history", {"action": "undo"})
    assert (
        at_start.structured_content["status"] == "rejected"
        and at_start.structured_content["reason"] == "at_start"
    )

    for i in (1, 2):
        fake.script(
            ScriptedRun(
                events=[
                    ("project_commit", {"label": f"edit {i}"}),
                    ("run_terminal", {"status": "completed", "finalText": "ok"}),
                ],
                commit_clips=[text_clip(f"c{i}", f"T{i}", (i - 1) * 30, 30)],
            )
        )
        await mcp_client.call_tool("cassette_run", {"message": f"edit {i}"})

    listed = await mcp_client.call_tool("cassette_history", {"action": "list"})
    groups = listed.structured_content["groups"]
    assert [g["label"] for g in groups] == ["edit 1", "edit 2"] and all(g["applied"] for g in groups)
    assert listed.structured_content["version"] == 2

    undone = await mcp_client.call_tool("cassette_history", {"action": "undo"})
    out = undone.structured_content
    assert out["status"] == "ok" and out["version_from"] == 2 and out["version_to"] == 3
    assert "removed c2" in out["timeline_delta"] and "c1" in out["timeline"]

    redone = await mcp_client.call_tool("cassette_history", {"action": "redo"})
    assert "added c2" in redone.structured_content["timeline_delta"]

    restored = await mcp_client.call_tool(
        "cassette_history", {"action": "restore_before", "group_id": groups[0]["group_id"]}
    )
    assert restored.structured_content["status"] == "ok"
    assert "0 clips" in restored.structured_content["timeline"]

    missing = await mcp_client.call_tool("cassette_history", {"action": "restore_before"})
    assert missing.structured_content["status"] == "failed"


async def test_history_is_locked_while_a_run_is_active(client, fake):
    mcp_client, _ = client
    await mcp_client.call_tool("cassette_project", {"action": "create"})
    fake.script(
        ScriptedRun(
            events=[
                ("project_commit", {"label": "one"}),
                ("run_terminal", {"status": "completed", "finalText": "ok"}),
            ],
            commit_clips=[text_clip("c1", "T", 0, 30)],
        )
    )
    await mcp_client.call_tool("cassette_run", {"message": "one"})
    fake.script(ScriptedRun(events=[]))  # stays running
    await mcp_client.call_tool("cassette_run", {"message": "two", "wait_sec": 1})
    locked = await mcp_client.call_tool("cassette_history", {"action": "undo"})
    assert locked.structured_content["status"] == "failed"
    assert locked.structured_content["error"]["code"] == "project_timeline_locked"
