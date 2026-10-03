"""The bridge against a running Cassette MCP service: open, import, a turn, export, revert.

    cd ~/Cassette-Editor && bun run dev:lambda        # the backend
    cd ~/Cassette-Editor && bun run dev:mcp          # the MCP service on 127.0.0.1:8790
    cd ~/oh-my-cassette && RUN_CASSETTE_LIVE=1 CASSETTE_AUTH_TOKEN=... uv run pytest tests/live -q -rs

CASSETTE_MCP_URL selects another service. The export renders through the backend's configured
render provider, which may cost money. Needs ffmpeg with libx265 to make the HEVC and HDR samples.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import anyio
import pytest
from tests.bridge_support import bridge_host

from oh_my_cassette.bridge.settings import DEFAULT_MCP_URL, BridgeSettings

pytestmark = [pytest.mark.live, pytest.mark.anyio]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TURN_ENDED = {"completed", "incomplete", "plan_ready", "needs_input", "paused", "failed", "stopped", "busy"}
STEP_LIMIT_SEC = 1800


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)


def _samples(media: Path) -> list[str]:
    """clip.mp4 as shipped, plus an HEVC Matroska file and a 10-bit PQ HDR file made here."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if " libx265 " not in encoders:
        pytest.skip("this ffmpeg has no libx265 encoder")
    shutil.copy(FIXTURES / "clip.mp4", media / "clip.mp4")
    video = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=3"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
    x265 = ["-c:v", "libx265", "-x265-params", "log-level=none"]
    _ffmpeg(*video, *tone, *x265, "-c:a", "aac", str(media / "hevc.mkv"))
    pq = (
        "format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv"
    )
    _ffmpeg(*video, "-vf", pq, *x265, "-tag:v", "hvc1", str(media / "hdr.mp4"))
    return ["media/clip.mp4", "media/hevc.mkv", "media/hdr.mp4"]


async def _call(host, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One tool call; the bridge's own `preparing` answer is waited out with the same arguments."""
    while True:
        result = await host.call_tool(tool, arguments)
        assert not result.is_error, (tool, result.content)
        content = result.structured_content or {}
        if content.get("status") != "preparing":
            return content


@pytest.fixture
def settings(tmp_path: Path) -> BridgeSettings:
    workspace = tmp_path / "workspace"
    (workspace / "media").mkdir(parents=True)
    return BridgeSettings(
        mcp_url=os.environ.get("CASSETTE_MCP_URL") or DEFAULT_MCP_URL,
        workspace=workspace.resolve(),
        auth_token=os.environ.get("CASSETTE_AUTH_TOKEN") or None,
        temp_dir=tmp_path / "bridge-temp",
    )


async def test_open_import_turn_export_and_revert(settings):
    files = _samples(settings.workspace / "media")
    async with bridge_host(settings) as (_, host):
        with anyio.fail_after(STEP_LIMIT_SEC):
            opened = await _call(host, "cassette_project_open", {"title": "oh-my-cassette live test"})
        project_id = opened["project_id"]
        assert opened["created"] is True

        with anyio.fail_after(STEP_LIMIT_SEC):
            imported = await _call(host, "cassette_import", {"project_id": project_id, "files": files})
            while imported["status"] == "processing":  # the backend's bounded wait: call again
                imported = await _call(host, "cassette_import", {"project_id": project_id, "files": files})
        assert [file["status"] for file in imported["files"]] == ["published"] * len(files), imported

        message = "Put the three imported videos on the timeline, one after another."
        with anyio.fail_after(STEP_LIMIT_SEC):
            turn = await _call(
                host, "cassette_turn", {"project_id": project_id, "message": message, "effort": "high"}
            )
            while turn["status"] == "running":
                again = {"project_id": project_id, "run_id": turn["run_id"], "cursor": turn["cursor"]}
                turn = await _call(host, "cassette_turn", again)
        assert turn["status"] in TURN_ENDED and turn["run_id"] and turn["chat_session_id"], turn
        assert turn["status"] == "completed", turn
        edited = await _call(host, "cassette_project_open", {"project_id": project_id})
        assert edited["timeline"]["clips"] >= len(files)

        with anyio.fail_after(STEP_LIMIT_SEC):
            exported = await _call(host, "cassette_export", {"project_id": project_id})
            while exported["status"] == "running":
                exported = await _call(
                    host, "cassette_export", {"project_id": project_id, "job_id": exported["job_id"]}
                )
        assert exported["status"] == "completed", exported
        local = Path(exported["file"])  # the bridge downloaded it and put its local path here
        assert local.is_file() and local.parent == settings.downloads.resolve()
        if exported.get("sha256"):
            assert hashlib.sha256(local.read_bytes()).hexdigest() == exported["sha256"]

        reverted = await _call(
            host,
            "cassette_undo",
            {"project_id": project_id, "action": "revert_run", "run_id": turn["run_id"]},
        )
        assert reverted["status"] == "moved"
        after = await _call(host, "cassette_project_open", {"project_id": project_id})
        assert after["timeline"]["clips"] == opened["timeline"]["clips"]
    assert list(settings.temp_root.iterdir()) == []
