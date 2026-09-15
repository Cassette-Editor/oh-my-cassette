from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from oh_my_cassette.cassette.prepare import MediaTools, fit_even, media_kind, mime_type, sha256_file

FIXTURES = Path(__file__).resolve().parent / "fixtures"
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)


def test_media_kind_and_mime():
    assert media_kind(Path("a.MOV")) == "video"
    assert media_kind(Path("a.m4a")) == "audio" and mime_type(Path("a.m4a")) == "audio/mp4"
    assert media_kind(Path("a.avif")) == "image" and mime_type(Path("a.avif")) == "image/avif"
    assert media_kind(Path("a.txt")) is None
    assert mime_type(Path("a.png")) == "image/png"


def test_fit_even_keeps_aspect_and_parity():
    assert fit_even((3840, 2160), 1920, 1080) == (1920, 1080)
    assert fit_even((1080, 1920), 1920, 1080) == (1080, 1920)
    assert fit_even((1280, 720), 1280, 720) == (1280, 720)
    assert fit_even((1281, 721), 1920, 1080) == (1280, 720)
    w, h = fit_even((4096, 1716), 1920, 1080)
    assert w == 1920 and h % 2 == 0 and h <= 1080


def test_sha256_matches_hashlib():
    import hashlib

    path = FIXTURES / "slide.png"
    assert sha256_file(path) == hashlib.sha256(path.read_bytes()).hexdigest()


@needs_ffmpeg
async def test_probe_reads_streams(anyio_backend):
    tools = MediaTools()
    probe = await tools.probe(FIXTURES / "clip.mp4")
    assert probe.has_video and probe.has_audio
    assert (probe.width, probe.height) == (160, 90)
    assert probe.codec == "h264" and probe.pixel_format == "yuv420p"
    assert probe.frame_rate == pytest.approx(25, abs=0.1)
    audio = await tools.probe(FIXTURES / "tone.wav")
    assert audio.has_audio and not audio.has_video and audio.duration_sec == pytest.approx(0.5, abs=0.05)


@needs_ffmpeg
async def test_prepare_video_meets_server_rules(anyio_backend, tmp_path):
    tools = MediaTools()
    prepared = await tools.prepare_video(FIXTURES / "clip.mp4", tmp_path)
    report = prepared["report"]
    assert report["processor"]["name"] == "ffmpeg"
    assert report["canonical"]["frameRate"] == pytest.approx(30, abs=0.05)
    assert report["preview"]["frameRate"] == pytest.approx(30, abs=0.05)
    assert report["canonical"]["codec"] == "h264" and report["preview"]["pixelFormat"] == "yuv420p"
    assert report["source"]["hasAudio"] is True and report["canonical"]["hasAudio"] is True
    for role in ("canonical", "preview"):
        assert prepared["paths"][role].exists() and prepared["paths"][role].stat().st_size > 0
