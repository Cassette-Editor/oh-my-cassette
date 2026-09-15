from __future__ import annotations

import os
import stat

from tests.fake_cassette import empty_document, text_clip, video_clip

from oh_my_cassette.render.digest import timeline_delta, timeline_digest
from oh_my_cassette.state import ProjectRecord, StateStore


def test_state_store_round_trip_and_permissions(tmp_path):
    store = StateStore(tmp_path / "home")
    assert store.list_projects() == []
    store.put_project(ProjectRecord(project_id="p1", chat_session_id="cs1", title="one"))
    store.bind_cwd("/work/a", "p1")
    assert store.project_for_cwd("/work/a").project_id == "p1"
    assert store.project_for_cwd("/work/b") is None
    store.update_project("p1", media={"m1": "/tmp/x.mp4"}, event_cursor=7)
    reloaded = StateStore(tmp_path / "home").get_project("p1")
    assert reloaded.media == {"m1": "/tmp/x.mp4"} and reloaded.event_cursor == 7
    mode = stat.S_IMODE(os.stat(tmp_path / "home" / "state.json").st_mode)
    assert mode == 0o600
    store.forget_project("p1")
    assert store.get_project("p1") is None and store.project_for_cwd("/work/a") is None


def test_state_store_survives_corrupt_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "state.json").write_text("{not json", "utf-8")
    store = StateStore(home)
    assert store.list_projects() == []
    store.put_project(ProjectRecord(project_id="p1", chat_session_id="cs1"))
    assert StateStore(home).get_project("p1") is not None


def _doc_with(clips):
    doc = empty_document("p")
    for clip in clips:
        doc["entities"]["clips"][clip["id"]] = clip
        doc["entities"]["sequences"]["seq_main"]["clipIds"].append(clip["id"])
    return doc


def test_digest_is_bounded_and_names_media():
    clips = [video_clip(f"v{i}", "m1", i * 30, 30) for i in range(40)]
    doc = _doc_with(clips)
    doc["version"] = 9
    text = timeline_digest(doc, {"m1": "beach.mp4"})
    lines = text.splitlines()
    assert lines[0].startswith("v9 · Main Timeline · 1920x1080 @ 30/1 fps · 40.00s (1200 frames)")
    assert "video beach.mp4" in text
    assert "… 28 more clips" in text
    assert len(lines) <= 45


def test_delta_summary():
    before = _doc_with([text_clip("a", "A", 0, 30)])
    after = _doc_with([text_clip("a", "A", 0, 60), text_clip("b", "B", 60, 30)])
    after["version"] = 2
    delta = timeline_delta(before, after)
    assert delta.added == ["b"] and delta.changed == ["a"] and delta.removed == []
    summary = delta.summary(before, after)
    assert summary.startswith('v0→v2: added b (text "B"); changed a; duration 30→90 frames')
    assert timeline_delta(before, before).summary() == "v0→v0: no timeline change"
