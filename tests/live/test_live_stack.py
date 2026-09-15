"""Runs against a real local Cassette-Editor stack.

    cd ~/Cassette-Editor && AGENT_AUTH_ENABLED=false bun run dev:lambda
    cd ~/oh-my-cassette && RUN_CASSETTE_LIVE=1 uv run pytest tests/live -q -rs

Set RUN_CASSETTE_LIVE_EXPORT=1 to also render (uses the configured Remotion provider, which may cost money).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from mcp.client import Client

from oh_my_cassette.server import build_server
from oh_my_cassette.settings import Settings

pytestmark = [pytest.mark.live, pytest.mark.anyio]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
async def live(tmp_path):
    settings = Settings(
        api_url=os.environ.get("CASSETTE_API_URL", "http://127.0.0.1:8787"),
        web_url=os.environ.get("CASSETTE_WEB_URL", "http://127.0.0.1:8080"),
        home=tmp_path / "home",
        run_timeout_sec=900,
        export_timeout_sec=1200,
    )
    work = tmp_path / "work"
    work.mkdir()
    server, app = build_server(settings, cwd=str(work))
    async with Client(server) as client:
        yield client, app, work


async def test_live_project_chat_import_edit_history(live):
    client, app, work = live
    created = await client.call_tool(
        "cassette_project", {"action": "create", "title": "oh-my-cassette live test"}
    )
    assert created.structured_content["status"] == "ok", created.content
    project_id = created.structured_content["project_id"]

    pong = await client.call_tool(
        "cassette_run", {"message": "Reply with exactly the single word: pong", "mode": "chat"}
    )
    assert pong.structured_content["status"] == "completed"
    assert "pong" in (pong.structured_content["final_text"] or "").lower()

    shutil.copy(FIXTURES / "tone.wav", work / "tone.wav")
    shutil.copy(FIXTURES / "slide.png", work / "slide.png")
    imported = await client.call_tool("cassette_import", {"paths": ["tone.wav", "slide.png"]})
    assert imported.structured_content["status"] == "ok", imported.structured_content
    assert imported.structured_content["imported"] == 2

    edit = await client.call_tool(
        "cassette_run",
        {
            "message": "Add a title text that says HELLO at the start of the timeline for 2 seconds, then show slide.png for 2 seconds after it.",
            "mode": "auto",
        },
    )
    out = edit.structured_content
    assert out["status"] in ("completed", "not_done"), out
    if out["status"] == "completed":
        assert out["version_to"] > out["version_from"]
        assert "HELLO" in out["timeline"]
        undone = await client.call_tool("cassette_history", {"action": "undo"})
        assert undone.structured_content["status"] == "ok"
        redone = await client.call_tool("cassette_history", {"action": "redo"})
        assert redone.structured_content["status"] == "ok"
        sheet = await client.call_tool("cassette_timeline", {"contact_sheet": True})
        assert sheet.structured_content["contact_sheet"]["tiles"] >= 1
        if os.environ.get("RUN_CASSETTE_LIVE_EXPORT") == "1":
            exported = await client.call_tool("cassette_export", {"file_name": "live-test"})
            assert exported.structured_content["status"] == "ok", exported.structured_content
            assert Path(exported.structured_content["file"]).stat().st_size > 0
    assert app.state.get_project(project_id) is not None
