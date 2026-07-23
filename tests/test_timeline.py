"""Self-checks for the CTL/diff renderers: golden digest, determinism, caps, gateway profile."""

from __future__ import annotations

import copy

from cassette import timeline


def _doc(clips: list[dict], tracks: list[dict] | None = None, version: int = 42) -> dict:
    tracks = tracks or [
        {"id": "t-v1", "name": "Video 1", "type": "video", "muted": False, "locked": False, "visible": True},
        {"id": "t-a1", "name": "Audio 1", "type": "audio", "muted": False, "locked": False, "visible": True},
    ]
    return {
        "schemaVersion": 2,
        "projectId": "try-session-7fq",
        "version": version,
        "sequenceTimebase": {"num": 30, "den": 1},
        "fps": 30,
        "compositionWidth": 1920,
        "compositionHeight": 1080,
        "entities": {
            "tracks": {t["id"]: t for t in tracks},
            "clips": {c["id"]: c for c in clips},
            "transitions": {},
        },
        "order": {"trackIds": [t["id"] for t in tracks], "clipIds": [c["id"] for c in clips], "transitionIds": []},
    }


def _clip(cid: str, name: str, track: str, start_s: float, dur_s: float, **extra) -> dict:
    return {
        "id": cid,
        "name": name,
        "type": extra.pop("type", "video"),
        "trackId": track,
        "startFrame": int(start_s * 30),
        "durationInFrames": int(dur_s * 30),
        **extra,
    }


def _sample_doc() -> dict:
    return _doc(
        [
            _clip("c1", "intro.mp4", "t-v1", 0, 7.2),
            _clip("c2", "beach.mp4", "t-v1", 7.2, 13.8, speed=1.2),
            _clip("c3", "drone.mp4", "t-v1", 21.0, 37.4),
            _clip("c4", "Summer 2026", "t-v1", 0.5, 2.5, type="text", text="Summer 2026"),
            _clip("c5", "bgm.mp3", "t-a1", 0, 58.4, type="audio", volume=0.6),
        ]
    )


def test_ctl_golden_shape():
    ctl = timeline.render_ctl(_sample_doc())
    lines = ctl.splitlines()
    assert lines[0] == "TIMELINE try-session-7fq v42 16:9 1080p30 00:58.4"
    assert any("[A" in ln and "intro.mp4" in ln and "00:00.0-00:07.2" in ln for ln in lines)
    assert any("beach.mp4" in ln and "speed 1.2x" in ln for ln in lines)
    assert any('"Summer 2026"' in ln for ln in lines)
    assert any("bgm.mp3" in ln and "vol 0.6" in ln for ln in lines)
    assert len(lines) <= timeline.CTL_MAX_LINES


def test_ctl_deterministic_and_letters_positional():
    a = timeline.render_ctl(_sample_doc())
    b = timeline.render_ctl(_sample_doc())
    assert a == b
    letters = timeline.clip_letters(_sample_doc())
    # Timeline order on V1: intro(0) < text(0.5) < beach(7.2) < drone(21)
    assert letters["c1"] == "A" and letters["c4"] == "B" and letters["c2"] == "C" and letters["c3"] == "D"
    assert letters["c5"] == "A"  # audio track letters are per-track


def test_ctl_elides_long_tracks_and_respects_cap():
    clips = [_clip(f"c{i}", f"clip{i}.mp4", "t-v1", i * 2.0, 2.0) for i in range(24)]
    ctl = timeline.render_ctl(_doc(clips))
    assert len(ctl.splitlines()) <= timeline.CTL_MAX_LINES
    assert "+" in ctl and "detail=track:V1" in ctl
    expanded = timeline.render_ctl(_doc(clips), detail="track:V1")
    assert "clip23.mp4" in expanded or len(expanded.splitlines()) == timeline.CTL_MAX_LINES


def test_gateway_profile_has_no_column_padding():
    text = timeline.render_ctl_gateway(_sample_doc())
    lines = text.splitlines()
    assert lines[0].startswith("TIMELINE v42")
    assert not any("  " in ln for ln in lines[1:]), "gateway profile must not rely on column alignment"
    assert any("→" in ln for ln in lines[1:])


def test_delta_add_change_remove():
    before = _sample_doc()
    after = copy.deepcopy(before)
    # trim the tail clip so the total duration really changes: drone 37.4s -> 34.9s
    after["entities"]["clips"]["c3"]["durationInFrames"] = int(34.9 * 30)
    # and retime beach
    after["entities"]["clips"]["c2"]["durationInFrames"] = int(11.3 * 30)
    # add a title
    new_title = _clip("c6", "Sunset", "t-v1", 9.0, 2.5, type="text", text="Sunset")
    after["entities"]["clips"]["c6"] = new_title
    # remove the bgm
    del after["entities"]["clips"]["c5"]
    after["version"] = 43

    delta = timeline.render_delta(before, after)
    lines = delta.splitlines()
    assert lines[0].startswith("CHANGES v42 -> v43")
    assert any(ln.startswith("~") and "beach.mp4" in ln for ln in lines)
    assert any(ln.startswith("+") and '"Sunset"' in ln for ln in lines)
    assert any(ln.startswith("-") and "bgm.mp3" in ln for ln in lines)
    assert any(ln.startswith("total ") for ln in lines)
    assert len(lines) <= timeline.DELTA_MAX_LINES + 1  # +1 for the total line


def test_delta_caps_and_no_change():
    before = _sample_doc()
    after = copy.deepcopy(before)
    for i in range(30):
        after["entities"]["clips"][f"n{i}"] = _clip(f"n{i}", f"new{i}.mp4", "t-v1", 60 + i, 1.0)
    capped = timeline.render_delta(before, after)
    assert len(capped.splitlines()) <= timeline.DELTA_MAX_LINES + 1
    assert "more changes" in capped
    assert "no clip changes" in timeline.render_delta(before, copy.deepcopy(before))


def test_plan_review_block_bounded():
    doc = _sample_doc()
    block = timeline.plan_review_block(
        {"reviewMarkdown": "1. Trim beach\n2. Add title\n3. Remove whoosh", "generativeMoments": [{"id": "g1"}]},
        doc,
    )
    assert block.startswith("PLAN REVIEW")
    assert "Current: 5 clips, v42, 00:58.4" in block
    assert "1 generative moment" in block
    long = timeline.plan_review_block({"reviewMarkdown": "x" * 2000}, doc)
    assert len(long) <= 700
