from __future__ import annotations

import json

import pytest
from tests.fake_cassette import ScriptedRun, empty_document, text_clip

from oh_my_cassette.cassette.errors import CassetteError
from oh_my_cassette.cassette.export import build_manifest, render_bundle

pytestmark = pytest.mark.anyio


def test_manifest_projection_single_sequence():
    doc = empty_document("p1")
    clip = text_clip("t1", "Hi", 10, 50)
    doc["entities"]["clips"]["t1"] = clip
    doc["entities"]["sequences"]["seq_main"]["clipIds"].append("t1")
    manifest = build_manifest("p1", doc, "my export")
    assert manifest["outputFileName"] == "my_export.mp4"
    assert manifest["totalFrames"] == 60
    assert manifest["width"] == 1920 and manifest["height"] == 1080
    assert manifest["tracks"][0]["isMain"] is True
    assert manifest["renderBundle"]["rootSequenceId"] == "seq_main"
    assert manifest["renderBundle"]["sequences"]["seq_main"]["clips"][0]["id"] == "t1"
    assert manifest["blobAssets"] == []


def test_manifest_rejects_empty_timeline():
    with pytest.raises(CassetteError) as exc:
        build_manifest("p1", empty_document("p1"), "x")
    assert exc.value.code == "timeline_empty"


def test_render_bundle_follows_nested_sequences():
    doc = empty_document("p1")
    doc["entities"]["sequences"]["seq_child"] = {
        **doc["entities"]["sequences"]["seq_main"],
        "id": "seq_child",
        "kind": "nested",
        "displayName": "Child",
        "compositionWidth": 640,
        "compositionHeight": 360,
        "trackIds": [],
        "clipIds": [],
        "mainTrackId": "v1",
    }
    nested = {
        "id": "n1",
        "sequenceId": "seq_main",
        "trackId": "v1",
        "displayName": "nested",
        "type": "nested-sequence",
        "startFrame": 0,
        "durationInFrames": 30,
        "connection": {"kind": "free"},
        "source": {"kind": "nested-sequence", "sequenceId": "seq_child"},
    }
    doc["entities"]["clips"]["n1"] = nested
    doc["entities"]["sequences"]["seq_main"]["clipIds"].append("n1")
    bundle = render_bundle(doc)
    assert set(bundle["sequences"]) == {"seq_main", "seq_child"}
    clip = bundle["sequences"]["seq_main"]["clips"][0]
    assert clip["visual"] == {"sourceWidth": 640, "sourceHeight": 360}


async def test_export_tool_downloads_the_render(client, fake, workdir):
    mcp_client, _ = client
    await mcp_client.call_tool("cassette_project", {"action": "create"})
    fake.script(
        ScriptedRun(
            events=[("project_commit", {}), ("run_terminal", {"status": "completed", "finalText": "ok"})],
            commit_clips=[text_clip("t1", "Hi", 0, 60)],
        )
    )
    await mcp_client.call_tool("cassette_run", {"message": "title"})
    result = await mcp_client.call_tool("cassette_export", {"file_name": "final cut"})
    out = result.structured_content
    assert out["status"] == "ok", out
    assert out["file"] == str(workdir / "exports" / "final_cut.mp4")
    assert out["bytes"] > 0 and out["total_frames"] == 60
    sent = fake.export_manifests[0]
    assert (
        sent["projectId"].startswith("try-session-") and sent["renderBundle"]["rootSequenceId"] == "seq_main"
    )
    assert json.dumps(sent)  # serialisable


async def test_export_refuses_empty_timeline(client):
    mcp_client, _ = client
    await mcp_client.call_tool("cassette_project", {"action": "create"})
    result = await mcp_client.call_tool("cassette_export", {})
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["error"]["code"] == "timeline_empty"


async def test_export_retries_once_on_transient_storage_error(client, fake, workdir):
    mcp_client, _ = client
    fake.fail_first_export = "object_write_unavailable"
    created = await mcp_client.call_tool("cassette_project", {"action": "create"})
    project_id = created.structured_content["project_id"]
    fake.seed_project(project_id, [text_clip("t1", "Hi", 0, 30)])
    result = await mcp_client.call_tool("cassette_export", {"file_name": "retry"})
    out = result.structured_content
    assert out["status"] == "ok", out
    assert len(fake.export_jobs) == 2
    assert [j["status"] for j in fake.export_jobs.values()] == ["error", "done"]
