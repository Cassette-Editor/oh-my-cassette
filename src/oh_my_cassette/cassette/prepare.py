"""Local media inspection and video preparation with ffprobe/ffmpeg.

Cassette stores three artifacts per video: the untouched ``original``, a ``canonical`` H.264 mezzanine
(yuv420p, 8-bit, SDR, 30 fps CFR, inside a 1920x1080 envelope, even dimensions) and a ``preview`` proxy
(same constraints, long edge <= 1280, short edge <= 720). The server re-probes both and rejects
anything outside those rules (server/services/preview-proxy-validation.ts), so this module produces
exactly that and self-checks with ffprobe before uploading a byte.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import mimetypes
import shutil
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from oh_my_cassette.cassette.errors import CassetteError, ToolNeedsFfmpeg

VIDEO_EXT = {"mp4", "mov"}
AUDIO_EXT = {"mp3", "wav", "m4a", "aac", "ogg", "oga", "opus", "flac"}
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "avif"}

MIME_OVERRIDES = {
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "oga": "audio/ogg",
    "opus": "audio/opus",
    "flac": "audio/flac",
    "mov": "video/quicktime",
    "avif": "image/avif",
    "webp": "image/webp",
}

TARGET_FPS = 30
CANONICAL_MAX_LONG, CANONICAL_MAX_SHORT = 1920, 1080
PREVIEW_MAX_LONG, PREVIEW_MAX_SHORT = 1280, 720


def media_kind(path: Path) -> str | None:
    ext = path.suffix.lower().lstrip(".")
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    return None


def mime_type(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    kind = media_kind(path)
    return f"{kind}/{ext}" if kind else "application/octet-stream"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


async def sha256_file_async(path: Path) -> str:
    return await asyncio.to_thread(sha256_file, path)


@dataclass
class Probe:
    container: str
    duration_sec: float | None
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    rotation: int
    codec: str | None
    codec_parameter: str | None
    pixel_format: str | None
    bit_depth: int | None
    hdr: bool
    frame_rate: float | None
    frame_rate_is_constant: bool
    audio_codec: str | None
    audio_sample_rate: int | None
    audio_channels: int | None
    raw: dict[str, Any]

    @property
    def display_size(self) -> tuple[int, int] | None:
        if not self.width or not self.height:
            return None
        if self.rotation % 180 == 90:
            return self.height, self.width
        return self.width, self.height

    def profile(self, container: str | None = None) -> dict[str, Any]:
        """Shape of packages/shared/src/types/media-upload.ts `preparedVideoProfileSchema`."""
        size = self.display_size or (0, 0)
        return {
            "container": container or self.container,
            "codec": self.codec or "unknown",
            "codecParameter": self.codec_parameter,
            "displayWidth": size[0],
            "displayHeight": size[1],
            "rotation": self.rotation,
            "durationSeconds": self.duration_sec or 0,
            "frameRate": self.frame_rate,
            "frameRateIsConstant": self.frame_rate_is_constant,
            "hdr": self.hdr,
            "bitDepth": self.bit_depth,
            "pixelFormat": self.pixel_format,
            "hasAudio": self.has_audio,
            "audioCodec": self.audio_codec,
            "audioSampleRate": self.audio_sample_rate,
            "audioChannels": self.audio_channels,
        }


class MediaTools:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    def require(self, binary: str) -> str:
        resolved = shutil.which(binary)
        if not resolved:
            raise ToolNeedsFfmpeg(binary)
        return resolved

    async def version(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.require(self.ffmpeg),
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        first = out.decode("utf-8", "replace").splitlines()[:1]
        if first and first[0].startswith("ffmpeg version "):
            return first[0].split()[2]
        return "unknown"

    async def probe(self, path: Path) -> Probe:
        proc = await asyncio.create_subprocess_exec(
            self.require(self.ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise CassetteError(
                "media_invalid", f"ffprobe failed for {path.name}: {err.decode('utf-8', 'replace')[:300]}"
            )
        raw = json.loads(out or b"{}")
        streams = raw.get("streams", [])
        video = next(
            (
                s
                for s in streams
                if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) != 1
            ),
            None,
        )
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = raw.get("format", {})
        duration = _float(fmt.get("duration")) or (_float(video.get("duration")) if video else None)
        rotation = 0
        if video:
            for side in video.get("side_data_list", []) or []:
                if "rotation" in side:
                    rotation = int(round(float(side["rotation"]))) % 360
            tag_rot = (video.get("tags") or {}).get("rotate")
            if tag_rot:
                rotation = int(tag_rot) % 360
        r_rate = _rate(video.get("r_frame_rate")) if video else None
        avg_rate = _rate(video.get("avg_frame_rate")) if video else None
        bit_depth = None
        if video:
            bits = video.get("bits_per_raw_sample")
            bit_depth = (
                int(bits) if bits and str(bits).isdigit() else _bit_depth_from_pix_fmt(video.get("pix_fmt"))
            )
        hdr = False
        if video:
            transfer = video.get("color_transfer") or ""
            primaries = video.get("color_primaries") or ""
            space = video.get("color_space") or ""
            hdr = (
                transfer in {"smpte2084", "arib-std-b67", "pq", "hlg"}
                or primaries == "bt2020"
                or "bt2020" in space
            )
        ext = path.suffix.lower().lstrip(".")
        container = "mov" if ext == "mov" else "mp4"
        return Probe(
            container=container,
            duration_sec=duration,
            has_video=video is not None,
            has_audio=audio is not None,
            width=int(video["width"]) if video and video.get("width") else None,
            height=int(video["height"]) if video and video.get("height") else None,
            rotation=rotation,
            codec=video.get("codec_name") if video else None,
            codec_parameter=(video.get("profile") if video else None),
            pixel_format=video.get("pix_fmt") if video else None,
            bit_depth=bit_depth,
            hdr=hdr,
            frame_rate=avg_rate or r_rate,
            frame_rate_is_constant=(
                r_rate is not None and avg_rate is not None and abs(r_rate - avg_rate) <= 0.02
            ),
            audio_codec=audio.get("codec_name") if audio else None,
            audio_sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
            audio_channels=int(audio["channels"]) if audio and audio.get("channels") else None,
            raw=raw,
        )

    async def prepare_video(self, source: Path, workdir: Path) -> dict[str, Any]:
        """Produce canonical + preview MP4s next to `workdir` and return the preparation report."""
        workdir.mkdir(parents=True, exist_ok=True)
        src = await self.probe(source)
        if not src.has_video:
            raise CassetteError("media_invalid", f"{source.name} has no video stream")
        display = src.display_size or (0, 0)
        canonical_path = workdir / f"{source.stem}.canonical.mp4"
        preview_path = workdir / f"{source.stem}.preview.mp4"
        await self._encode(
            source, canonical_path, display, CANONICAL_MAX_LONG, CANONICAL_MAX_SHORT, src.has_audio, crf=18
        )
        await self._encode(
            source, preview_path, display, PREVIEW_MAX_LONG, PREVIEW_MAX_SHORT, src.has_audio, crf=23
        )
        canonical = await self.probe(canonical_path)
        preview = await self.probe(preview_path)
        problems = self.validate_pair(src, canonical, preview)
        if problems:
            raise CassetteError(
                "canonical_profile_invalid", f"prepared artifacts failed self-check: {', '.join(problems)}"
            )
        return {
            "paths": {"canonical": canonical_path, "preview": preview_path},
            "report": {
                "source": src.profile(),
                "canonical": canonical.profile("mp4"),
                "preview": preview.profile("mp4"),
                "canonicalWasTranscoded": True,
                "preparedAt": dt.datetime.now(dt.UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "processor": {
                    "name": "ffmpeg",
                    "version": await self.version(),
                    "hardwareAcceleration": "none",
                    "storage": "local",
                },
            },
        }

    async def _encode(
        self,
        source: Path,
        dest: Path,
        display: tuple[int, int],
        max_long: int,
        max_short: int,
        has_audio: bool,
        *,
        crf: int,
    ) -> None:
        width, height = fit_even(display, max_long, max_short)
        vf = f"scale={width}:{height}:flags=lanczos,format=yuv420p,fps={TARGET_FPS}"
        args = [
            self.require(self.ffmpeg),
            "-y",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            str(crf),
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(TARGET_FPS),
            "-fps_mode",
            "cfr",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
        ]
        if has_audio:
            args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        else:
            args += ["-an"]
        args += ["-movflags", "+faststart", str(dest)]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise CassetteError(
                "video_encode_unsupported",
                f"ffmpeg failed on {source.name}: {err.decode('utf-8', 'replace')[-400:]}",
            )

    @staticmethod
    def validate_pair(source: Probe, canonical: Probe, preview: Probe) -> list[str]:
        reasons: list[str] = []
        for name, probe, max_long, max_short in (
            ("canonical", canonical, CANONICAL_MAX_LONG, CANONICAL_MAX_SHORT),
            ("preview", preview, PREVIEW_MAX_LONG, PREVIEW_MAX_SHORT),
        ):
            if not probe.has_video:
                reasons.append(f"{name}-missing-video")
                continue
            if probe.codec != "h264":
                reasons.append(f"{name}-codec-not-h264")
            if probe.pixel_format != "yuv420p":
                reasons.append(f"{name}-pixel-format-not-yuv420p")
            if probe.bit_depth not in (None, 8):
                reasons.append(f"{name}-bit-depth-not-8")
            if probe.hdr:
                reasons.append(f"{name}-hdr-not-supported")
            if probe.frame_rate is None or abs(probe.frame_rate - TARGET_FPS) > 0.02:
                reasons.append(f"{name}-frame-rate-not-30")
            w, h = probe.width or 0, probe.height or 0
            if max(w, h) > max_long or min(w, h) > max_short:
                reasons.append(f"{name}-dimensions-over-limit")
            if w % 2 or h % 2:
                reasons.append(f"{name}-dimensions-not-even")
            if probe.has_audio != source.has_audio:
                reasons.append(f"{name}-audio-presence-mismatch")
        if (
            canonical.duration_sec
            and preview.duration_sec
            and abs(canonical.duration_sec - preview.duration_sec) > 1 / TARGET_FPS + 0.05
        ):
            reasons.append("duration-drift")
        return reasons


def fit_even(display: tuple[int, int], max_long: int, max_short: int) -> tuple[int, int]:
    width, height = display
    if width <= 0 or height <= 0:
        return (max_long, max_short) if max_long >= max_short else (max_short, max_long)
    long_edge, short_edge = max(width, height), min(width, height)
    scale = min(1.0, max_long / long_edge, max_short / short_edge)
    out_w = max(2, int(round(width * scale / 2)) * 2)
    out_h = max(2, int(round(height * scale / 2)) * 2)
    return out_w, out_h


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        return None


def _rate(value: Any) -> float | None:
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        frac = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return float(frac) if frac > 0 else None


def _bit_depth_from_pix_fmt(pix_fmt: str | None) -> int | None:
    if not pix_fmt:
        return None
    for depth in (16, 14, 12, 10):
        if f"p{depth}" in pix_fmt:
            return depth
    return 8
