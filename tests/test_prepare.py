"""Local preparation with the real ffmpeg: every rendition meets its declaration, whatever the source."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
    pytest.skip("ffmpeg and ffprobe are not installed", allow_module_level=True)

from tests.bridge_support import bridge_host
from tests.reference_remote import PREPARE

from oh_my_cassette.bridge.errors import BridgeError
from oh_my_cassette.bridge.files import guess_mime
from oh_my_cassette.bridge.prepare import STDERR_TAIL, FFmpegPreparer, fit, jpeg_qscale, recording_clock
from oh_my_cassette.bridge.settings import BridgeSettings
from oh_my_cassette.contract import Convert, Transcode, preparation_for, prepare_spec

pytestmark = pytest.mark.anyio

SPEC = prepare_spec(PREPARE)
RENDITIONS = SPEC.renditions
ROLES = [r.role for r in RENDITIONS]
FRAME = 1 / 30
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
LAVFI_VIDEO = "testsrc2=size={size}:rate={rate}:duration={duration}"


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)


def _probe(path: Path, *, count_frames: bool = False) -> dict[str, Any]:
    args = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    if count_frames:
        args += ["-count_frames"]
    return json.loads(subprocess.run([*args, str(path)], capture_output=True, check=True, timeout=60).stdout)


def _has_encoder(name: str) -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
    return any(line.split()[1:2] == [name] for line in out.stdout.splitlines() if line.strip())


def _video(size: str = "320x180", rate: str = "30", duration: str = "1") -> list[str]:
    return ["-f", "lavfi", "-i", LAVFI_VIDEO.format(size=size, rate=rate, duration=duration)]


def _tone(duration: str = "1") -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]


def _make_hevc(path: Path) -> None:
    if not _has_encoder("libx265"):
        pytest.skip("this ffmpeg has no libx265 encoder")
    _ffmpeg(*_video(rate="25"), *_tone(), "-c:v", "libx265", "-x265-params", "log-level=none", "-c:a", "aac",
            str(path))  # fmt: skip


def _make_hdr(path: Path) -> None:
    tags = "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv"
    _ffmpeg(*_video(), "-vf", f"format=yuv420p10le,{tags}", "-c:v", "ffv1", str(path))


def _make_rotated(path: Path) -> None:
    plain = path.with_name("unrotated.mp4")
    _ffmpeg(*_video(), *_tone(), "-c:v", "libx264", "-c:a", "aac", "-shortest", str(plain))
    _ffmpeg("-display_rotation", "90", "-i", str(plain), "-c", "copy", str(path))  # 90° counter-clockwise


def _make_odd(path: Path) -> None:
    _ffmpeg(
        *_video(size="640x360"), "-vf", "scale=641:359,setsar=1,format=yuv444p", "-c:v", "ffv1", str(path)
    )


def _make_silent(path: Path) -> None:
    _ffmpeg(*_video(), "-c:v", "libx264", str(path))


def _make_vfr(path: Path) -> None:
    keep = r"select='lt(n\,20)+not(mod(n\,4))'"
    _ffmpeg(*_video(duration="2"), "-vf", keep, "-fps_mode", "vfr", "-c:v", "libx264", str(path))


def _make_large(path: Path) -> None:
    _ffmpeg(*_video(size="2400x1350", duration="0.5"), *_tone("0.5"), "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", str(path))  # fmt: skip


def _make_timecode(path: Path) -> None:
    _ffmpeg(
        *_video(size="160x90", rate="30000/1001"), "-timecode", "01:00:00;02", "-c:v", "libx264", str(path)
    )


SAMPLES = {
    "hevc.mkv": _make_hevc,
    "hdr-pq.mkv": _make_hdr,
    "rotated.mp4": _make_rotated,
    "odd-641x359.mkv": _make_odd,
    "silent.mp4": _make_silent,
    "vfr.mp4": _make_vfr,
    "large.mp4": _make_large,
    "timecode.mov": _make_timecode,
}


@pytest.fixture
def preparer(tmp_path: Path) -> FFmpegPreparer:
    return FFmpegPreparer(BridgeSettings(mcp_url="http://unused", workspace=tmp_path))


def _close(value: float, expected: float, tolerance: float) -> bool:
    return abs(value - expected) <= tolerance + 1e-6


def _rate(text: str) -> float:
    return float(Fraction(text))


def _stream(data: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((s for s in data["streams"] if s["codec_type"] == kind), None)


def _check_report_shape(report: dict[str, Any]) -> None:
    """The fields and types the backend's report schema accepts, and nothing else."""
    assert set(report) == {
        "recordingClock",
        "source",
        *ROLES,
        "canonicalWasTranscoded",
        "preparedAt",
        "processor",
    }
    profile_types = {
        "container": str,
        "codec": str,
        "codecParameter": (str, type(None)),
        "displayWidth": int,
        "displayHeight": int,
        "rotation": int,
        "durationSeconds": (int, float),
        "frameRate": (int, float, type(None)),
        "frameRateIsConstant": bool,
        "hdr": bool,
        "bitDepth": (int, type(None)),
        "pixelFormat": (str, type(None)),
        "hasAudio": bool,
        "audioCodec": (str, type(None)),
        "audioSampleRate": (int, type(None)),
        "audioChannels": (int, type(None)),
    }
    for key in ("source", *ROLES):
        profile = report[key]
        assert set(profile) == set(profile_types), key
        for field, kind in profile_types.items():
            assert isinstance(profile[field], kind) and not (
                kind is int and isinstance(profile[field], bool)
            ), field
        assert profile["displayWidth"] > 0 and profile["displayHeight"] > 0 and profile["durationSeconds"] > 0
        for field in ("frameRate", "bitDepth", "audioSampleRate", "audioChannels"):
            assert profile[field] is None or profile[field] > 0, field
        assert 1 <= len(profile["container"]) <= 64
    for role in ROLES:
        assert report[role]["container"] in {"mp4", "mov"}
    assert report["canonicalWasTranscoded"] is True
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", report["preparedAt"])
    processor = report["processor"]
    assert set(processor) == {"name", "version", "encoder"}
    assert processor["name"] == "ffmpeg" and processor["encoder"] == "libx264"
    assert 1 <= len(processor["version"]) <= 64
    clock = report["recordingClock"]
    if clock is not None:
        assert set(clock) <= {
            "origin", "ticks", "ticksPerSecond", "sourceOriginUs", "wrapsAt24Hours", "dropFrame", "nominalFps",
            "originalTimebase", "trackId",
        }  # fmt: skip
        assert clock["origin"] == "embedded-timecode" and isinstance(clock["ticks"], int)
        assert set(clock["ticksPerSecond"]) == {"num", "den"}


def _check_rendition(
    path: Path, role: str, source: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any]:
    """One output against its declaration and its source; returns its video stream."""
    declared = next(r for r in RENDITIONS if r.role == role).video
    data = _probe(path, count_frames=True)
    video = _stream(data, "video")
    assert video is not None
    assert data["format"]["format_name"].startswith("mov,mp4")
    assert video["codec_name"] == "h264" and video["profile"] == "High"
    assert video["pix_fmt"] == "yuv420p" and video.get("bits_per_raw_sample", "8") == "8"
    assert _close(_rate(video["r_frame_rate"]), 30, 0.02) and _close(_rate(video["avg_frame_rate"]), 30, 0.02)
    width, height = video["width"], video["height"]
    assert width % 2 == 0 and height % 2 == 0
    assert max(width, height) <= declared.max_long_edge and min(width, height) <= declared.max_short_edge
    assert video.get("color_transfer") not in HDR_TRANSFERS and video.get("color_primaries") != "bt2020"
    assert video.get("color_transfer") == "bt709" and video.get("color_space") == "bt709"
    assert not any("rotation" in side for side in video.get("side_data_list", []))
    assert "rotate" not in video.get("tags", {})
    source_aspect = source["displayWidth"] / source["displayHeight"]
    assert abs(width / height - source_aspect) / source_aspect <= 0.01
    audio = _stream(data, "audio")
    assert (audio is not None) == source["hasAudio"]
    if audio is not None:
        assert audio["codec_name"] == "aac" and int(audio["sample_rate"]) == 48000 and audio["channels"] == 2
    if source["frameRateIsConstant"]:
        assert _close(float(video["duration"]), source["durationSeconds"], FRAME)
    # the report describes this very file
    assert (profile["displayWidth"], profile["displayHeight"]) == (width, height)
    assert profile["codec"] == "h264" and profile["rotation"] == 0 and profile["hdr"] is False
    assert profile["bitDepth"] == 8 and profile["hasAudio"] == source["hasAudio"]
    assert profile["codecParameter"].startswith("avc1.64")
    return video


@pytest.mark.parametrize("name", list(SAMPLES))
async def test_every_rendition_meets_its_declaration(name, preparer, tmp_path):
    source = tmp_path / name
    SAMPLES[name](source)
    seen: list[float] = []
    artifacts, report = await preparer.transcode(source, RENDITIONS, tmp_path / "out", seen.append)

    _check_report_shape(report)
    assert [a.role for a in artifacts] == ROLES and all(a.mime_type == "video/mp4" for a in artifacts)
    streams = [_check_rendition(a.path, a.role, report["source"], report[a.role]) for a in artifacts]
    # one decode split into every rendition: the same frames, the same length
    assert len({int(s["nb_read_frames"]) for s in streams}) == 1
    durations = [float(s["duration"]) for s in streams]
    assert max(durations) - min(durations) <= FRAME
    assert seen and seen[-1] == 100.0 and all(0 <= value <= 100 for value in seen)


async def test_what_ffprobe_says_about_each_source(preparer, tmp_path):
    expected = {
        "hdr-pq.mkv": {
            "container": "matroska",
            "codec": "ffv1",
            "hdr": True,
            "bitDepth": 10,
            "hasAudio": False,
        },
        "rotated.mp4": {"rotation": 270, "displayWidth": 180, "displayHeight": 320, "hasAudio": True},
        "odd-641x359.mkv": {"displayWidth": 641, "displayHeight": 359, "pixelFormat": "yuv444p"},
        "silent.mp4": {"hasAudio": False, "audioCodec": None, "frameRateIsConstant": True},
        "vfr.mp4": {"frameRateIsConstant": False},
        "large.mp4": {"displayWidth": 2400, "displayHeight": 1350},
    }
    sizes = {
        "rotated.mp4": [(180, 320), (180, 320)],
        "odd-641x359.mkv": [(640, 358), (640, 358)],
        "large.mp4": [(1920, 1080), (1280, 720)],
    }
    for name, facts in expected.items():
        source = tmp_path / name
        SAMPLES[name](source)
        _, report = await preparer.transcode(source, RENDITIONS, tmp_path / f"out-{name}", lambda _: None)
        assert {key: report["source"][key] for key in facts} == facts, name
        assert report["recordingClock"] is None
        if name in sizes:
            assert [(report[r]["displayWidth"], report[r]["displayHeight"]) for r in ROLES] == sizes[name], (
                name
            )


async def test_an_embedded_timecode_becomes_the_recording_clock(preparer, tmp_path):
    source = tmp_path / "timecode.mov"
    _make_timecode(source)
    _, report = await preparer.transcode(source, RENDITIONS, tmp_path / "out", lambda _: None)
    assert report["recordingClock"] == {
        "origin": "embedded-timecode",
        "ticks": 107894,  # 01:00:00;02 in drop-frame labels at 30000/1001
        "ticksPerSecond": {"num": 30000, "den": 1001},
        "sourceOriginUs": 0,
        "wrapsAt24Hours": True,
        "dropFrame": True,
        "nominalFps": 30,
    }


def test_timecode_labels_count_frames_at_the_nominal_rate():
    def clock(timecode: str, rate: str) -> dict[str, Any] | None:
        stream = {"codec_type": "video", "r_frame_rate": rate, "tags": {"timecode": timecode}}
        return recording_clock({"streams": [stream], "format": {}})

    assert clock("00:01:00;02", "30000/1001")["ticks"] == 1800  # the first label of minute one
    assert clock("00:10:00;00", "30000/1001")["ticks"] == 17982  # tenth minutes drop nothing
    assert clock("00:01:00;04", "60000/1001")["ticks"] == 3600
    non_drop = clock("10:00:00:00", "25/1")
    assert non_drop["ticks"] == 900000 and non_drop["dropFrame"] is False
    assert clock("01:00:00;00", "25/1")["dropFrame"] is False  # drop-frame exists only at multiples of 30
    assert clock("not a timecode", "30/1") is None
    assert clock("00:00:00:30", "30/1") is None
    assert (
        recording_clock({"streams": [{"codec_type": "video", "r_frame_rate": "30/1"}], "format": {}}) is None
    )


def test_sizes_fit_the_envelope_evenly_and_never_grow():
    assert fit(3840, 2160, 1920, 1080) == (1920, 1080)
    assert fit(2160, 3840, 1920, 1080) == (1080, 1920)
    assert fit(641, 359, 1920, 1080) == (640, 358)
    assert fit(320, 180, 1280, 720) == (320, 180)
    assert fit(4000, 1000, 1920, 1080) == (1920, 480)
    assert fit(1, 1, 1920, 1080) == (2, 2)
    assert (jpeg_qscale(100), jpeg_qscale(0), jpeg_qscale(92)) == (2, 31, 4)


def _make_png(path: Path) -> None:
    _ffmpeg(*_video(size="64x48"), "-frames:v", "1", str(path))


@pytest.mark.parametrize("name", ["still.tiff", "still.heic"])
async def test_images_the_backend_does_not_take_become_jpeg(name, preparer, tmp_path):
    png = tmp_path / "still.png"
    _make_png(png)
    source = tmp_path / name
    if name.endswith(".heic"):
        if shutil.which("sips") is None:
            pytest.skip("no HEIC encoder here (sips is macOS only)")
        subprocess.run(["sips", "-s", "format", "heic", str(png), "--out", str(source)], check=True,
                       capture_output=True, timeout=60)  # fmt: skip
    else:
        _ffmpeg("-i", str(png), str(source))
    plan = preparation_for(SPEC, guess_mime(source), source.name)
    assert isinstance(plan, Convert) and plan.kind == "image"
    seen: list[float] = []
    output = await preparer.convert(source, plan.kind, plan.conversion, tmp_path / "out", seen.append)
    assert output.name == "still.jpg" and seen[-1] == 100.0
    data = _probe(output)
    image = _stream(data, "video")
    assert data["format"]["format_name"] in {"jpeg_pipe", "image2"}
    assert image["codec_name"] == "mjpeg" and (image["width"], image["height"]) == (64, 48)


async def test_accepted_images_and_audio_pass_through(tmp_path):
    png, wav = tmp_path / "still.png", tmp_path / "tone.wav"
    _make_png(png)
    _ffmpeg(*_tone("0.5"), str(wav))
    assert preparation_for(SPEC, guess_mime(png), png.name) is None
    assert preparation_for(SPEC, guess_mime(wav), wav.name) is None
    assert isinstance(preparation_for(SPEC, "video/mp4", "clip.mp4"), Transcode)


async def test_audio_the_backend_does_not_take_becomes_flac(preparer, tmp_path):
    source = tmp_path / "tone.aiff"
    _ffmpeg(*_tone("0.5"), "-c:a", "pcm_s16be", str(source))
    plan = preparation_for(SPEC, guess_mime(source), source.name)
    assert isinstance(plan, Convert) and plan.kind == "audio"
    output = await preparer.convert(source, plan.kind, plan.conversion, tmp_path / "out", lambda _: None)
    assert output.name == "tone.flac"
    data = _probe(output)
    assert data["format"]["format_name"] == "flac" and _stream(data, "audio")["codec_name"] == "flac"
    assert math.isclose(float(data["format"]["duration"]), 0.5, abs_tol=0.05)


async def test_a_missing_ffmpeg_says_how_to_install_it(tmp_path):
    missing = FFmpegPreparer(
        BridgeSettings(mcp_url="http://unused", workspace=tmp_path, ffmpeg="/nowhere/ffmpeg")
    )
    with pytest.raises(BridgeError) as caught:
        await missing.transcode(tmp_path / "a.mp4", RENDITIONS, tmp_path / "out", lambda _: None)
    assert caught.value.code == "bridge.ffmpeg_unavailable"
    assert "CASSETTE_FFMPEG" in caught.value.message and "install ffmpeg" in caught.value.message


async def test_an_unreadable_file_fails_with_the_stderr_tail(preparer, tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video " * 1000)
    with pytest.raises(BridgeError) as caught:
        await preparer.transcode(broken, RENDITIONS, tmp_path / "out", lambda _: None)
    assert caught.value.code == "bridge.prepare_failed" and caught.value.retryable is False
    assert "broken.mp4" in caught.value.message
    assert len(caught.value.message.partition(": ")[2]) <= STDERR_TAIL


async def test_the_bridge_prepares_real_files_before_the_handshake(remote, bridge_settings):
    settings = bridge_settings(remote.url)
    media = settings.workspace / "media"
    media.mkdir()
    _make_odd(media / "clip.mkv")
    _make_png(media / "still.png")
    _ffmpeg("-i", str(media / "still.png"), str(media / "scan.tiff"))
    _ffmpeg(*_tone("0.5"), str(media / "tone.wav"))
    paths = ["media/clip.mkv", "media/scan.tiff", "media/tone.wav", "media/still.png"]
    async with bridge_host(settings) as (_, host):
        result = await host.call_tool("import_media", {"paths": paths})
    assert not result.is_error, result
    clip, scan, tone, still = remote.begun
    assert clip["mimeType"] == "video/x-matroska" and clip["size"] == (media / "clip.mkv").stat().st_size
    assert [(a["role"], a["mimeType"]) for a in clip["artifacts"]] == [(r, "video/mp4") for r in ROLES]
    _check_report_shape(clip["preparation"])
    assert (scan["name"], scan["relativePath"], scan["mimeType"]) == (
        "scan.jpg",
        "media/scan.tiff",
        "image/jpeg",
    )
    assert [a["role"] for a in scan["artifacts"]] == ["original"] and scan["preparation"] is None
    assert tone["name"] == "tone.wav" and tone["artifacts"][0]["role"] == "original"
    assert still["artifacts"][0]["sha256"] == still["sha256"]  # passed through unchanged
    assert list(settings.temp_root.iterdir()) == []
