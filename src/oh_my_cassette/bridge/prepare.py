"""Local media preparation with ffmpeg: what a `prepare` declaration asks for, made on this machine.

A video is always transcoded, in one ffmpeg run that splits the decoded frames into every declared
rendition so their frame counts match. Audio and images the backend does not take as they are get
converted. ffprobe describes the source and every output for the preparation report (contract §3).
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

import anyio

from oh_my_cassette.bridge.errors import BridgeError
from oh_my_cassette.bridge.settings import BridgeSettings
from oh_my_cassette.contract import Conversion, Rendition

ProgressFn = Callable[[float], None]
"""Receives the percent (0–100) of the step in progress."""

ENCODER = "libx264"
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
STDERR_TAIL = 1000
INSTALL_HINT = (
    "install ffmpeg (for example `brew install ffmpeg`, `sudo apt install ffmpeg` or `winget install ffmpeg`), "
    "or set CASSETTE_FFMPEG and CASSETTE_FFPROBE to the two programs"
)
# ffprobe prints these as `color_range`; zscale names them differently.
ZSCALE_RANGES = {"tv": "limited", "pc": "full"}
TIMECODE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})([:;])(\d{2,3})$")
H264_PROFILE_IDC = {
    "baseline": (0x42, 0x00),
    "constrained baseline": (0x42, 0x40),
    "main": (0x4D, 0x00),
    "extended": (0x58, 0x00),
    "high": (0x64, 0x00),
    "high 10": (0x6E, 0x00),
    "high 4:2:2": (0x7A, 0x00),
    "high 4:4:4 predictive": (0xF4, 0x00),
}


@dataclass(frozen=True)
class Artifact:
    role: str
    path: Path
    mime_type: str


class Preparer(Protocol):
    """Makes the files a `prepare` declaration asks for. The bridge's default is `FFmpegPreparer`."""

    async def transcode(
        self, source: Path, renditions: tuple[Rendition, ...], workdir: Path, progress: ProgressFn
    ) -> tuple[list[Artifact], dict[str, Any]]:
        """Every rendition of a video, plus its preparation report."""
        ...

    async def convert(
        self, source: Path, kind: str, conversion: Conversion, workdir: Path, progress: ProgressFn
    ) -> Path:
        """An audio or image file converted to `conversion`, named `<stem>.<extension>`."""
        ...


# ── ffprobe output ──


def _rate(value: Any) -> Fraction | None:
    if not isinstance(value, str):
        return None
    num, _, den = value.replace(":", "/").partition("/")
    try:
        rate = Fraction(int(num), int(den or "1"))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _whole(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def video_stream(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and not (stream.get("disposition") or {}).get("attached_pic"):
            return stream
    return None


def audio_stream(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), None)


def rotation(stream: Mapping[str, Any]) -> int:
    """Clockwise degrees a player turns the coded frames: 0, 90, 180 or 270."""
    sides = [s for s in stream.get("side_data_list") or [] if isinstance(s, Mapping) and "rotation" in s]
    try:
        # ffprobe prints the display matrix angle counter-clockwise; the legacy `rotate` tag is clockwise.
        degrees = (
            -float(sides[0]["rotation"]) if sides else float((stream.get("tags") or {}).get("rotate") or 0)
        )
    except (TypeError, ValueError):
        degrees = 0.0
    return int(round(degrees / 90) * 90) % 360


def display_size(stream: Mapping[str, Any]) -> tuple[int, int]:
    """Width and height as shown: non-square pixels stretched, rotation applied."""
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    sar = _rate(stream.get("sample_aspect_ratio"))
    if sar is not None and sar != 1:
        width = round(width * sar)
    return (height, width) if rotation(stream) in (90, 270) else (width, height)


def _bit_depth(stream: Mapping[str, Any]) -> int | None:
    explicit = _whole(stream.get("bits_per_raw_sample"))
    if explicit:
        return explicit
    pixel_format = str(stream.get("pix_fmt") or "")
    match = re.search(r"(\d+)(le|be)$", pixel_format)
    if match:
        return int(match.group(1))
    return 8 if pixel_format else None


def _codec_parameter(stream: Mapping[str, Any]) -> str | None:
    """The RFC 6381 codec string for H.264 (`avc1.PPCCLL`); None for other codecs."""
    if stream.get("codec_name") != "h264":
        return None
    known = H264_PROFILE_IDC.get(str(stream.get("profile") or "").lower())
    level = stream.get("level")
    if known is None or not isinstance(level, int) or level <= 0:
        return None
    return f"avc1.{known[0]:02x}{known[1]:02x}{level:02x}"


def container_name(data: Mapping[str, Any]) -> str:
    """ffprobe's format name, first alias only (`matroska,webm` → `matroska`)."""
    name = str((data.get("format") or {}).get("format_name") or "unknown")
    return name.split(",", 1)[0]


def media_profile(data: Mapping[str, Any], container: str) -> dict[str, Any]:
    """The report's profile of one file. Raises ValueError when it has no usable video."""
    video = video_stream(data)
    if video is None:
        raise ValueError("no video stream")
    width, height = display_size(video)
    duration = _number((data.get("format") or {}).get("duration")) or _number(video.get("duration"))
    if width <= 0 or height <= 0 or duration is None:
        raise ValueError("no video dimensions or duration")
    audio = audio_stream(data)
    real, average = _rate(video.get("r_frame_rate")), _rate(video.get("avg_frame_rate"))
    frame_rate = average or real
    return {
        "container": container,
        "codec": str(video.get("codec_name") or "unknown"),
        "codecParameter": _codec_parameter(video),
        "displayWidth": width,
        "displayHeight": height,
        "rotation": rotation(video),
        "durationSeconds": duration,
        "frameRate": round(float(frame_rate), 6) if frame_rate else None,
        "frameRateIsConstant": bool(real and average and abs(float(real) - float(average)) <= 0.02),
        "hdr": str(video.get("color_transfer") or "").lower() in HDR_TRANSFERS,
        "bitDepth": _bit_depth(video),
        "pixelFormat": video.get("pix_fmt") or None,
        "hasAudio": audio is not None,
        "audioCodec": audio.get("codec_name") if audio else None,
        "audioSampleRate": _whole(audio.get("sample_rate")) if audio else None,
        "audioChannels": _whole(audio.get("channels")) if audio else None,
    }


def recording_clock(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """The embedded timecode as a frame count at the nominal rate, or None without one."""
    video = video_stream(data)
    rate = _rate(video.get("r_frame_rate")) if video else None
    tags = [
        (data.get("format") or {}).get("tags") or {},
        *((s.get("tags") or {}) for s in data.get("streams") or []),
    ]
    timecode = next((t["timecode"] for t in tags if isinstance(t.get("timecode"), str)), None)
    match = TIMECODE.match(timecode.strip()) if timecode else None
    if rate is None or match is None:
        return None
    hours, minutes, seconds, separator, frames = match.groups()
    hh, mm, ss, ff = int(hours), int(minutes), int(seconds), int(frames)
    nominal = round(rate)
    if nominal <= 0 or hh >= 24 or mm >= 60 or ss >= 60 or ff >= nominal:
        return None
    drop_frame = separator == ";" and nominal % 30 == 0
    ticks = (hh * 3600 + mm * 60 + ss) * nominal + ff
    if drop_frame:  # labels 00 and 01 (00–03 at 60) are skipped each minute except every tenth
        total_minutes = hh * 60 + mm
        ticks -= (nominal // 15) * (total_minutes - total_minutes // 10)
    return {
        "origin": "embedded-timecode",
        "ticks": ticks,
        "ticksPerSecond": {"num": rate.numerator, "den": rate.denominator},
        "sourceOriginUs": 0,
        "wrapsAt24Hours": True,
        "dropFrame": drop_frame,
        "nominalFps": nominal,
    }


def fit(width: int, height: int, max_long: int, max_short: int) -> tuple[int, int]:
    """The largest even size inside the envelope with the source's aspect, never above the source."""
    landscape = width >= height
    scale = min(1.0, max_long / max(width, height), max_short / min(width, height))

    def even(value: float) -> int:
        return max(2, math.floor(value + 1e-6) // 2 * 2)

    w_limit, h_limit = (max_long, max_short) if landscape else (max_short, max_long)
    return min(even(width * scale), w_limit // 2 * 2), min(even(height * scale), h_limit // 2 * 2)


def rate_text(value: float) -> str:
    return str(Fraction(value).limit_denominator(1001))


def video_filtergraph(
    source: Mapping[str, Any], renditions: tuple[Rendition, ...], sizes: list[tuple[int, int]], tonemap: bool
) -> str:
    """Decode once, split, then per rendition: constant frame rate, size, 8-bit BT.709.

    Frames arrive already rotated (ffmpeg's autorotate). An HDR source is tone-mapped to SDR with
    zscale when `tonemap` is set; otherwise the scale step still converts its matrix to BT.709.
    """
    head = "[0:v:0]"
    if tonemap:
        width, height = max(sizes, key=lambda size: size[0] * size[1])
        transfer = str(source.get("color_transfer") or "smpte2084")
        primaries = str(source.get("color_primaries") or "bt2020")
        matrix = str(source.get("color_space") or "bt2020nc")
        value_range = ZSCALE_RANGES.get(str(source.get("color_range") or "tv"), "limited")
        head += (
            f"zscale=w={width}:h={height}:tin={transfer}:pin={primaries}:min={matrix}:rin={value_range}"
            ":t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
            "zscale=t=bt709:m=bt709:r=limited,format=yuv420p,"
        )
    head += f"split={len(renditions)}" + "".join(f"[s{i}]" for i in range(len(renditions)))
    chains = [head]
    for index, (rendition, (width, height)) in enumerate(zip(renditions, sizes, strict=True)):
        chains.append(
            f"[s{index}]fps={rate_text(rendition.video.frame_rate)},"
            f"scale={width}:{height}:out_color_matrix=bt709:out_range=tv,"
            f"format={rendition.video.pixel_format},setsar=1,"
            f"setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709[v{index}]"
        )
    return ";".join(chains)


def rendition_args(index: int, rendition: Rendition, has_audio: bool, output: Path) -> list[str]:
    video, audio = rendition.video, rendition.audio
    rate = rate_text(video.frame_rate)
    gop = str(max(1, round(video.frame_rate * video.keyframe_interval_seconds)))
    args = [
        "-map", f"[v{index}]",
        "-c:v", ENCODER, "-profile:v", video.profile, "-preset", video.preset, "-crf", str(video.crf),
        "-pix_fmt", video.pixel_format, "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
        "-fps_mode", "cfr", "-r", rate,
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
    ]  # fmt: skip
    if has_audio:
        args += [
            "-map", "0:a:0", "-c:a", "aac", "-b:a", str(audio.bitrate),
            "-ar", str(audio.sample_rate), "-ac", str(audio.channels),
        ]  # fmt: skip
    else:
        args += ["-an"]
    return [
        *args,
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output),
    ]


def jpeg_qscale(quality: int | None) -> int:
    """JPEG quality 0–100 as ffmpeg's `-q:v` (31 worst … 2 best)."""
    return round(31 - (90 if quality is None else quality) / 100 * 29)


# ── running ffmpeg ──


def _failed(what: str, detail: str) -> BridgeError:
    tail = detail.strip()[-STDERR_TAIL:] or "no output"
    return BridgeError("bridge.prepare_failed", f"{what}: {tail}")


class FFmpegPreparer:
    def __init__(self, settings: BridgeSettings) -> None:
        self._settings = settings
        self._programs: tuple[str, str] | None = None
        self._capabilities: tuple[str, bool] | None = None

    def programs(self) -> tuple[str, str]:
        """(ffmpeg, ffprobe), from CASSETTE_FFMPEG / CASSETTE_FFPROBE or PATH. Looked up again until found."""
        if self._programs is None:
            ffmpeg = shutil.which(self._settings.ffmpeg or "ffmpeg")
            ffprobe = shutil.which(self._settings.ffprobe or "ffprobe")
            if ffmpeg is None or ffprobe is None:
                missing = " and ".join(
                    name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not path
                )
                raise BridgeError("bridge.ffmpeg_unavailable", f"{missing} not found; {INSTALL_HINT}")
            self._programs = (ffmpeg, ffprobe)
        return self._programs

    async def capabilities(self) -> tuple[str, bool]:
        """(ffmpeg version, whether this build can tone-map HDR with zscale)."""
        if self._capabilities is None:
            ffmpeg, _ = self.programs()
            version = await anyio.run_process([ffmpeg, "-hide_banner", "-version"], check=False)
            match = re.match(r"ffmpeg version (\S+)", version.stdout.decode(errors="replace"))
            filters = await anyio.run_process([ffmpeg, "-hide_banner", "-filters"], check=False)
            names = {
                parts[1]
                for line in filters.stdout.decode(errors="replace").splitlines()
                if len(parts := line.split()) > 2
            }
            self._capabilities = (match.group(1)[:64] if match else "unknown", {"zscale", "tonemap"} <= names)
        return self._capabilities

    async def probe(self, path: Path) -> dict[str, Any]:
        _, ffprobe = self.programs()
        result = await anyio.run_process(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            check=False,
        )
        if result.returncode != 0:
            raise _failed(f"ffprobe could not read {path.name}", result.stderr.decode(errors="replace"))
        return json.loads(result.stdout or b"{}")

    async def _run(self, what: str, args: list[str], duration: float | None, progress: ProgressFn) -> None:
        tail = bytearray()
        process = await anyio.open_process(args, stdin=subprocess.DEVNULL)
        async with process:

            async def drain() -> None:
                assert process.stderr is not None
                async for chunk in process.stderr:
                    tail.extend(chunk)
                    del tail[: -4 * STDERR_TAIL]

            async def follow() -> None:
                assert process.stdout is not None
                pending = b""
                async for chunk in process.stdout:
                    *lines, pending = (pending + chunk).split(b"\n")
                    for line in lines:
                        key, _, value = line.decode(errors="replace").strip().partition("=")
                        if duration and key in ("out_time_us", "out_time_ms") and value.isdigit():
                            progress(min(100.0, int(value) / 1e6 / duration * 100))

            async with anyio.create_task_group() as tg:
                tg.start_soon(drain)
                tg.start_soon(follow)
            code = await process.wait()
        if code != 0:
            raise _failed(f"{what} failed (ffmpeg exit {code})", tail.decode(errors="replace"))

    def _command(self, source: Path) -> list[str]:
        ffmpeg, _ = self.programs()
        quiet = ["-hide_banner", "-nostdin", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y"]
        return [ffmpeg, *quiet, "-i", str(source)]

    async def transcode(
        self, source: Path, renditions: tuple[Rendition, ...], workdir: Path, progress: ProgressFn
    ) -> tuple[list[Artifact], dict[str, Any]]:
        version, can_tonemap = await self.capabilities()
        data = await self.probe(source)
        try:
            source_profile = media_profile(data, container_name(data))
        except ValueError as exc:
            raise BridgeError("bridge.prepare_failed", f"{source.name}: {exc}") from exc
        stream = video_stream(data) or {}
        width, height = source_profile["displayWidth"], source_profile["displayHeight"]
        sizes = [fit(width, height, r.video.max_long_edge, r.video.max_short_edge) for r in renditions]
        workdir.mkdir(parents=True, exist_ok=True)
        outputs = [
            workdir / f"{i}-{re.sub(r'[^A-Za-z0-9_.-]', '_', r.role)}.mp4" for i, r in enumerate(renditions)
        ]
        graph = video_filtergraph(stream, renditions, sizes, tonemap=can_tonemap and source_profile["hdr"])
        args = [*self._command(source), "-filter_complex", graph]
        for index, (rendition, output) in enumerate(zip(renditions, outputs, strict=True)):
            args += rendition_args(index, rendition, source_profile["hasAudio"], output)
        await self._run(f"transcoding {source.name}", args, source_profile["durationSeconds"], progress)

        report: dict[str, Any] = {"recordingClock": recording_clock(data), "source": source_profile}
        for rendition, output in zip(renditions, outputs, strict=True):
            try:
                report[rendition.role] = media_profile(await self.probe(output), rendition.container)
            except ValueError as exc:
                raise BridgeError("bridge.prepare_failed", f"{source.name} {rendition.role}: {exc}") from exc
        report |= {
            "canonicalWasTranscoded": True,
            "preparedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "processor": {"name": "ffmpeg", "version": version, "encoder": ENCODER},
        }
        progress(100.0)
        artifacts = [
            Artifact(r.role, output, r.mime_type) for r, output in zip(renditions, outputs, strict=True)
        ]
        return artifacts, report

    async def convert(
        self, source: Path, kind: str, conversion: Conversion, workdir: Path, progress: ProgressFn
    ) -> Path:
        workdir.mkdir(parents=True, exist_ok=True)
        output = workdir / f"{source.stem}.{conversion.extension}"
        if kind == "image":
            # No -map: for tiled HEIF the default selection is the assembled picture, not one tile.
            codec = ["-frames:v", "1", "-an", "-c:v", "mjpeg", "-q:v", str(jpeg_qscale(conversion.quality))]
            args = [*self._command(source), *codec, "-pix_fmt", "yuvj420p", "-update", "1", "-f", "image2"]
            duration = None
        else:
            data = await self.probe(source)
            duration = _number((data.get("format") or {}).get("duration"))
            args = [*self._command(source), "-map", "0:a:0", "-vn", "-c:a", "flac", "-f", "flac"]
        await self._run(f"converting {source.name}", [*args, str(output)], duration, progress)
        progress(100.0)
        return output
