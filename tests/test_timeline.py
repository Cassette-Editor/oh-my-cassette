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


# Hrefs generated by repo B's REAL encoder (packages/shared/src/storyboard-link.ts
# buildStoryboardHref) so the Python decoder is pinned to the actual wire contract.
_STORYBOARD_FIXTURES = [
    (
        "media://storyboard/c5aa1fac-0f47-4e82-835c-541ce9516c29?i=0&role=hook&d=4&look=manual&cov=source&hero=1"
        "&s=18&e=22&p=Open+on+the+wave-crash+laughter+moment+%28grab+attention%29",
        {
            "index": 0,
            "mediaFileId": "c5aa1fac-0f47-4e82-835c-541ce9516c29",
            "startSec": 18,
            "endSec": 22,
            "role": "hook",
            "durationSec": 4,
            "look": "manual",
            "hero": True,
            "coverage": "source",
            "purpose": "Open on the wave-crash laughter moment (grab attention)",
        },
    ),
    (
        "media://storyboard/none?i=1&role=b_roll&d=3&look=manual&cov=generated&p=Cover+the+declared+source+gap",
        {
            "index": 1,
            "mediaFileId": None,
            "startSec": None,
            "endSec": None,
            "role": "b_roll",
            "durationSec": 3,
            "look": "manual",
            "hero": False,
            "coverage": "generated",
            "purpose": "Cover the declared source gap",
        },
    ),
    (
        "media://storyboard/clip%20with%20spaces%20%26%20unicode%20%E6%A0%87%E9%A2%98.mp4?i=2&role=develop&d=2.25"
        "&look=restyle&cov=mixed&s=0.5&e=2.75&p=%E9%9B%A8%E5%A4%9C%E9%9C%93%E8%99%B9+look+%E2%80%94+50%25+intensity",
        {
            "index": 2,
            "mediaFileId": "clip with spaces & unicode 标题.mp4",
            "startSec": 0.5,
            "endSec": 2.75,
            "role": "develop",
            "durationSec": 2.25,
            "look": "restyle",
            "hero": False,
            "coverage": "mixed",
            "purpose": "雨夜霓虹 look — 50% intensity",
        },
    ),
]


def test_parse_storyboard_href_matches_real_encoder():
    for href, expected in _STORYBOARD_FIXTURES:
        assert timeline.parse_storyboard_href(href) == expected
    # Sanitizer-emitted single-slash form decodes identically.
    single = _STORYBOARD_FIXTURES[0][0].replace("media://", "media:/")
    assert timeline.parse_storyboard_href(single) == _STORYBOARD_FIXTURES[0][1]
    assert timeline.parse_storyboard_href("media://storyboard/abc?i=0") is None  # role is required
    assert timeline.parse_storyboard_href("https://example.com/x") is None
    assert timeline.parse_storyboard_href("") is None


def _storyboard_markdown() -> str:
    cards = " ".join(
        f"[{label}]({href})"
        for label, (href, _) in zip(["Hook · 0:18–0:22", "B Roll · 3s", "Develop · 0:00–0:02"], _STORYBOARD_FIXTURES)
    )
    return f"## Beat board\n{cards}\n\n## Generative moments\n- **Restyle** · 0:00–0:02 [Source]({_STORYBOARD_FIXTURES[2][0]})"


def test_storyboard_frames_decodes_in_markdown_order():
    frames = timeline.storyboard_frames(_storyboard_markdown())
    assert [f["role"] for f in frames] == ["hook", "b_roll", "develop", "develop"]
    assert frames[0]["hero"] is True and frames[1]["mediaFileId"] is None


def test_plan_review_block_replaces_storyboard_links_with_labels():
    block = timeline.plan_review_block({"reviewMarkdown": _storyboard_markdown()}, _sample_doc())
    assert "media://storyboard" not in block and "media:/storyboard" not in block
    assert "Hook · 0:18–0:22" in block
    assert "B Roll · 3s" in block


def test_contact_sheet_from_data_uri_posters(tmp_path, monkeypatch):
    import base64
    import shutil
    import subprocess

    import pytest as _pytest

    from cassette import tools

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        _pytest.skip("ffmpeg not installed")
    monkeypatch.setenv("CASSETTE_ASSET_ROOT", str(tmp_path))
    # Render two tiny real jpegs via ffmpeg so the tile input is a decodable poster.
    poster = tmp_path / "poster.jpg"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x36:d=1", "-frames:v", "1", str(poster)],
        capture_output=True,
        timeout=30,
        check=True,
    )
    data_uri = "data:image/jpeg;base64," + base64.b64encode(poster.read_bytes()).decode()
    doc = _sample_doc()
    for clip in doc["entities"]["clips"].values():
        clip["thumbnail"] = data_uri

    sheet = tools.build_contact_sheet(doc, "try-session-abc")
    assert sheet is not None
    from pathlib import Path

    out = Path(sheet)
    assert out.exists() and out.stat().st_size > 0
    assert out.parent == tmp_path / "previews" / "try-session-abc"
    assert out.name == "sheet-v42.jpg"


def test_contact_sheet_skips_without_posters(tmp_path, monkeypatch):
    from cassette import tools

    monkeypatch.setenv("CASSETTE_ASSET_ROOT", str(tmp_path))
    assert tools.build_contact_sheet(_sample_doc(), "try-session-abc") is None


def test_storyboard_sheet_from_local_media_with_placeholder(tmp_path, monkeypatch):
    """Plan storyboard: real beats tile from ingested media; a generated beat gets a
    placeholder cell so cell order matches the digest; restyle refs are skipped."""
    import shutil
    import subprocess

    import pytest as _pytest

    from cassette import tools

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        _pytest.skip("ffmpeg not installed")
    monkeypatch.setenv("CASSETTE_ASSET_ROOT", str(tmp_path))
    source = tmp_path / "beach.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=teal:s=64x36:d=2:r=30", str(source)],
        capture_output=True,
        timeout=30,
        check=True,
    )
    monkeypatch.setattr(tools, "_sheet_media_lookup", lambda session_id: ({"media-1": str(source)}, {}))
    frames = [
        {
            "index": 0,
            "mediaFileId": "media-1",
            "startSec": 0.2,
            "endSec": 1.0,
            "role": "hook",
            "durationSec": 1,
            "look": "manual",
            "hero": True,
            "coverage": "source",
            "purpose": "open",
        },
        {
            "index": 1,
            "mediaFileId": None,
            "startSec": None,
            "endSec": None,
            "role": "b_roll",
            "durationSec": 2,
            "look": "manual",
            "hero": False,
            "coverage": "generated",
            "purpose": "gap",
        },
        {
            "index": 2,
            "mediaFileId": "media-1",
            "startSec": 0.0,
            "endSec": 1.5,
            "role": "restyle",
            "durationSec": 1.5,
            "look": "restyle",
            "hero": False,
            "coverage": "mixed",
            "purpose": "look",
        },
    ]
    sheet = tools.build_storyboard_sheet("try-session-sb", frames)
    assert sheet is not None
    from pathlib import Path

    out = Path(sheet)
    assert out.exists() and out.stat().st_size > 0
    assert out.parent == tmp_path / "previews" / "try-session-sb"
    assert out.name.startswith("storyboard-")
    # Immutable per frame-set digest: a rebuild returns the same cached file.
    assert tools.build_storyboard_sheet("try-session-sb", frames) == sheet


def test_storyboard_sheet_requires_beats(tmp_path, monkeypatch):
    from cassette import tools

    monkeypatch.setenv("CASSETTE_ASSET_ROOT", str(tmp_path))
    assert tools.build_storyboard_sheet("try-session-sb", []) is None
    only_restyle = [
        {
            "index": 0,
            "mediaFileId": "m",
            "startSec": 0,
            "endSec": 1,
            "role": "restyle",
            "durationSec": 1,
            "look": "restyle",
            "hero": False,
            "coverage": "mixed",
            "purpose": "x",
        }
    ]
    assert tools.build_storyboard_sheet("try-session-sb", only_restyle) is None


def test_contact_sheet_from_local_media(tmp_path, monkeypatch):
    """API-job path: no poster data URIs — frames come from the locally ingested media file."""
    import shutil
    import subprocess

    import pytest as _pytest

    from cassette import tools

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        _pytest.skip("ffmpeg not installed")
    monkeypatch.setenv("CASSETTE_ASSET_ROOT", str(tmp_path))
    source = tmp_path / "beach.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=teal:s=64x36:d=2:r=30", str(source)],
        capture_output=True,
        timeout=30,
        check=True,
    )
    doc = _sample_doc()  # video clips named intro/beach/drone — no thumbnails anywhere
    monkeypatch.setattr(tools, "_sheet_media_lookup", lambda session_id: ({}, {"beach.mp4": str(source)}))
    sheet = tools.build_contact_sheet(doc, "try-session-localmedia")
    assert sheet is not None
    from pathlib import Path

    assert Path(sheet).exists() and Path(sheet).stat().st_size > 0


def test_clip_source_midpoint_seek():
    from cassette.tools import _clip_source_midpoint_sec

    # 4s of timeline at speed 1.5 starting 2s into the source -> mid at 2 + 3 = 5s.
    clip = {"inSec": 2.0, "durationInFrames": 120, "speed": 1.5}
    assert abs(_clip_source_midpoint_sec(clip, 30.0) - 5.0) < 1e-6
    # Clamped inside the known source duration.
    clip["sourceDurationSeconds"] = 4.0
    assert _clip_source_midpoint_sec(clip, 30.0) <= 3.9 + 1e-6
    assert _clip_source_midpoint_sec({}, 30.0) == 0.0
