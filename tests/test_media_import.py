from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
pytestmark = pytest.mark.anyio


async def _project(mcp_client):
    created = await mcp_client.call_tool("cassette_project", {"action": "create"})
    return created.structured_content["project_id"]


async def test_audio_and_image_import_end_to_end(client, fake, workdir):
    mcp_client, app = client
    project_id = await _project(mcp_client)
    shutil.copy(FIXTURES / "tone.wav", workdir / "tone.wav")
    shutil.copy(FIXTURES / "slide.png", workdir / "slide.png")
    (workdir / "notes.txt").write_text("not media")
    result = await mcp_client.call_tool(
        "cassette_import", {"paths": ["tone.wav", str(workdir / "slide.png"), "missing.mp3", "notes.txt"]}
    )
    out = result.structured_content
    assert out["status"] == "partial"
    by_name = {i["name"]: i for i in out["items"]}
    assert by_name["tone.wav"]["status"] == "ready" and by_name["tone.wav"]["kind"] == "audio"
    assert by_name["slide.png"]["status"] == "ready" and by_name["slide.png"]["kind"] == "image"
    assert by_name["missing.mp3"]["reason"] == "file_not_found"
    assert by_name["notes.txt"]["reason"] == "unsupported_type"
    assert out["imported"] == 2
    assert {f["name"] for f in out["library"]} == {"tone.wav", "slide.png"}

    # bytes really went through the presigned PUT and the register/complete handshake
    upload = next(iter(fake.uploads.values()))
    assert upload["status"] == "queued"
    assert all(t["objectId"] in fake.objects for t in upload["targets"])
    record = app.state.get_project(project_id)
    assert set(record.media.values()) == {str(workdir / "tone.wav"), str(workdir / "slide.png")}

    # re-importing the same content is a local no-op reported as duplicate
    again = await mcp_client.call_tool("cassette_import", {"paths": ["tone.wav"]})
    assert again.structured_content["items"][0]["status"] == "duplicate"
    assert again.structured_content["status"] == "ok"
    assert len(fake.uploads) == 2


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)
async def test_video_is_rejected_until_backend_accepts_ffmpeg_preparation(client, fake, workdir):
    mcp_client, _ = client
    await _project(mcp_client)
    shutil.copy(FIXTURES / "clip.mp4", workdir / "clip.mp4")
    result = await mcp_client.call_tool("cassette_import", {"paths": ["clip.mp4"]})
    item = result.structured_content["items"][0]
    assert item["status"] == "failed"
    assert item["reason"].startswith("video_import_requires_backend_update")
    assert result.structured_content["status"] == "failed"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)
async def test_video_import_with_ffmpeg_preparation(client, fake, workdir):
    mcp_client, _ = client
    await _project(mcp_client)
    fake.reject_video_preparation = False
    shutil.copy(FIXTURES / "clip.mp4", workdir / "clip.mp4")
    result = await mcp_client.call_tool("cassette_import", {"paths": ["clip.mp4"]})
    item = result.structured_content["items"][0]
    assert item["status"] == "ready", item
    upload = next(iter(fake.uploads.values()))
    assert sorted(a["role"] for a in upload["artifacts"]) == ["canonical", "original", "preview"]
    register = upload
    assert all(t["objectId"] in fake.objects for t in register["targets"])
