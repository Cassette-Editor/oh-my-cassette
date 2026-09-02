#!/usr/bin/env python3
"""Live upload → Gemini analysis → edit → preview → export acceptance via stdio MCP."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSTRUCTION = "Keep only the section with the blue background, moving white circle, and KEEP THIS text."
SERVER_HOOK_CONTRACT = (
    "POST JSON to the configured hook. snapshot: "
    '{"action":"snapshot","sessionId":"...","mediaFileIds":["..."]} -> '
    '{"sessionId":"...","exportRenderRequestCount":0}. advance_and_sweep: '
    '{"action":"advance_and_sweep","sessionId":"...","expiresAt":"...","mediaFileIds":["..."]} -> '
    '{"sessionId":"...","sweepCompleted":true,"accessibleServerObjectCount":0,'
    '"accessibleGoogleFileCount":0,"queueReferenceCount":0,"idempotent":false}. '
    'The second identical call must return the same zero counts and "idempotent":true.'
)


class AcceptanceError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full live Cassette MCP media acceptance")
    parser.add_argument("--media", type=Path, help="optional existing media; omitted generates the canonical fixture")
    parser.add_argument("--instruction", default=DEFAULT_INSTRUCTION)
    parser.add_argument("--host", choices=("codex", "claude", "opencode", "hermes"), default="codex")
    parser.add_argument("--timeout-sec", type=int, default=1500)
    parser.add_argument("--model", default="GPT-5.6 Luna")
    parser.add_argument(
        "--acceptance-hook-url",
        default=os.getenv("CASSETTE_E2E_ACCEPTANCE_HOOK_URL", ""),
        help="test-only backend acceptance hook required for render and remote-retention proof",
    )
    parser.add_argument(
        "--acceptance-hook-token",
        default=os.getenv("CASSETTE_E2E_ACCEPTANCE_HOOK_TOKEN", ""),
        help="optional bearer token for the test-only backend acceptance hook",
    )
    parser.add_argument(
        "--allow-missing-server-hook",
        action="store_true",
        help="run only the plugin-local gate; output remains partial and is not full acceptance",
    )
    parser.add_argument(
        "--thinking-level",
        choices=("off", "minimal", "low", "medium", "high", "xhigh"),
        default="xhigh",
    )
    return parser.parse_args()


class AcceptanceHook:
    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token

    def call(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(
            self.url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:  # noqa: S310 - operator-supplied acceptance endpoint
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise AcceptanceError(
                f"backend acceptance hook failed: {exc}. Required contract: {SERVER_HOOK_CONTRACT}"
            ) from exc
        if not isinstance(result, dict):
            raise AcceptanceError(
                f"backend acceptance hook returned a non-object. Required contract: {SERVER_HOOK_CONTRACT}"
            )
        return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _temporary_environment(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_overlay_ppm(path: Path, *, circle: bool = False) -> None:
    if circle:
        width = height = 96
        pixels = bytearray(width * height * 3)
        for y in range(height):
            for x in range(width):
                if (x - width / 2) ** 2 + (y - height / 2) ** 2 <= (width / 2 - 2) ** 2:
                    offset = (y * width + x) * 3
                    pixels[offset : offset + 3] = b"\xff\xff\xff"
    else:
        width, height = 560, 96
        pixels = bytearray(width * height * 3)
        glyphs = {
            "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
            "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
            "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
            "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
            "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
            "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
            "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
            " ": ("00000",) * 7,
        }
        scale = 10
        text = "KEEP THIS"
        x_origin = (width - (len(text) * 6 - 1) * scale) // 2
        y_origin = (height - 7 * scale) // 2
        for character_index, character in enumerate(text):
            for y, row in enumerate(glyphs[character]):
                for x, enabled in enumerate(row):
                    if enabled != "1":
                        continue
                    for yy in range(y_origin + y * scale, y_origin + (y + 1) * scale):
                        for xx in range(
                            x_origin + (character_index * 6 + x) * scale,
                            x_origin + (character_index * 6 + x + 1) * scale,
                        ):
                            offset = (yy * width + xx) * 3
                            pixels[offset : offset + 3] = b"\xff\xff\xff"
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + pixels)


def generate_fixture(directory: Path) -> Path:
    """Create the 18s 720p30 H.264/AAC canonical acceptance fixture."""
    directory.mkdir(parents=True, exist_ok=True)
    text = directory / "keep-this.ppm"
    circle = directory / "circle.ppm"
    target = directory / "agentic-video-fixture.mp4"
    _write_overlay_ppm(text)
    _write_overlay_ppm(circle, circle=True)
    command = [
        os.getenv("CASSETTE_FFMPEG_BIN", "ffmpeg"),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=1280x720:r=30:d=6",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=1280x720:r=30:d=6",
        "-f",
        "lavfi",
        "-i",
        "color=c=green:s=1280x720:r=30:d=6",
        "-loop",
        "1",
        "-framerate",
        "30",
        "-t",
        "6",
        "-i",
        str(circle),
        "-loop",
        "1",
        "-framerate",
        "30",
        "-t",
        "6",
        "-i",
        str(text),
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=18",
        "-filter_complex",
        "[3:v]colorkey=black:0.1:0.0[circlekey];"
        "[1:v][circlekey]overlay=x='(W-w)*t/6':y='(H-h)/2'[circle];"
        "[4:v]colorkey=black:0.1:0.0[text];"
        "[circle][text]overlay=x='(W-w)/2':y=60[mid];"
        "[0:v][mid][2:v]concat=n=3:v=1:a=0[v]",
        "-map",
        "[v]",
        "-map",
        "5:a",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode != 0 or not target.is_file():
        raise AcceptanceError(f"failed to generate deterministic fixture: {(result.stderr or '')[-500:]}")
    return target


def _structured(result) -> dict:
    value = result.structuredContent
    if not isinstance(value, dict):
        raise AcceptanceError("MCP tool returned no structured result")
    if not value.get("ok"):
        error = value.get("error") or {}
        raise AcceptanceError(f"{error.get('code') or 'unknown'}: {error.get('message') or 'tool failed'}")
    return value


def _questions(envelope: dict) -> list[dict]:
    job = (envelope.get("data") or {}).get("job") or {}
    return [question for question in (job.get("questions") or []) if isinstance(question, dict)]


async def _approve_plan_reviews(session: ClientSession, envelope: dict, read_timeout: timedelta) -> dict:
    settled = envelope
    for _ in range(3):
        if settled.get("phase") != "needs_user":
            return settled
        if not any(question.get("reason") == "edit_plan_review" for question in _questions(settled)):
            raise AcceptanceError(f"live job requires non-plan user input: {settled.get('job_id')}")
        settled = _structured(
            await session.call_tool(
                "cassette_answer_question",
                {"job_id": settled["job_id"], "response": "approve"},
                read_timeout_seconds=read_timeout,
            )
        )
    raise AcceptanceError("plan review did not settle after three typed approvals")


def _parse_timestamp(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AcceptanceError(f"{field} is not an ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise AcceptanceError(f"{field} must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _assert_analysis_receipt(envelope: dict, *, expires_at: str) -> dict:
    job = (envelope.get("data") or {}).get("job") or {}
    receipts = (job.get("quality") or {}).get("analysis_receipts") or []
    if not receipts:
        raise AcceptanceError("backend exposed no Gemini analysis receipt")
    receipt = receipts[0]
    expected = {
        "provider": "google",
        "model": "gemini-3.8-flash",
        "api": "interactions",
        "processing": "agentic",
        "fileTransport": "files_api",
        "serviceTier": "standard",
        "store": False,
    }
    required = {
        *expected,
        "responseId",
        "agenticNavigationStepCount",
        "startedAt",
        "completedAt",
        "evidenceCount",
        "expiresAt",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise AcceptanceError(
            f"analysis receipt must contain only the canonical non-sensitive fields; got {sorted(receipt)}"
        )
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise AcceptanceError(f"analysis receipt mismatch for {key}: {receipt.get(key)!r}")
    # Stateless Interactions intentionally use store:false. Google currently returns a null
    # response id for those calls, so require the receipt field without inventing an id.
    if "responseId" not in receipt or int(receipt.get("agenticNavigationStepCount") or 0) < 1:
        raise AcceptanceError("analysis receipt lacks response-id field or agentic navigation evidence")
    if int(receipt.get("evidenceCount") or 0) < 1:
        raise AcceptanceError("analysis receipt proves no timestamped evidence")
    started = _parse_timestamp(receipt.get("startedAt"), "analysis receipt startedAt")
    completed = _parse_timestamp(receipt.get("completedAt"), "analysis receipt completedAt")
    if completed < started:
        raise AcceptanceError("analysis receipt completedAt precedes startedAt")
    if receipt.get("expiresAt") != expires_at:
        raise AcceptanceError(
            f"analysis receipt deadline {receipt.get('expiresAt')!r} does not match session deadline {expires_at!r}"
        )
    return receipt


def _assert_timeline(timeline: dict) -> None:
    data = timeline.get("data") or {}
    duration = float(data.get("duration_sec") or 0)
    if not 4.5 <= duration <= 7.5:
        raise AcceptanceError(f"edited timeline duration should be about 6 seconds, got {duration}")
    clips = [clip for clip in (data.get("clips") or []) if isinstance(clip, dict)]
    source_clips = [clip for clip in clips if clip.get("media_file_id")]
    if not source_clips:
        raise AcceptanceError("timeline returned no source-backed clips")
    starts = [float(clip.get("source_start_sec") or 0) for clip in source_clips]
    ends = [float(clip.get("source_end_sec") or 0) for clip in source_clips]
    if min(starts) < 4.5 or min(starts) > 7.0 or max(ends) < 11.0 or max(ends) > 13.5:
        raise AcceptanceError(f"timeline source windows do not select the 6–12s middle segment: {source_clips}")
    if not timeline.get("artifacts"):
        raise AcceptanceError("cassette_timeline(contact_sheet=true) returned no contact sheet artifact")


def _assert_contact_sheet_is_blue(path: Path) -> None:
    command = [
        os.getenv("CASSETTE_FFMPEG_BIN", "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        "scale=320:180",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, timeout=60, check=False)
    if result.returncode != 0 or len(result.stdout) != 320 * 180 * 3:
        raise AcceptanceError("could not inspect the returned contact sheet")
    pixels = list(zip(result.stdout[0::3], result.stdout[1::3], result.stdout[2::3]))
    blue_ratio = sum(1 for red, green, blue in pixels if blue > 80 and blue > red * 1.25 and blue > green * 1.1) / len(
        pixels
    )
    white_ratio = sum(1 for red, green, blue in pixels if min(red, green, blue) > 210) / len(pixels)
    if blue_ratio < 0.35 or white_ratio < 0.001:
        raise AcceptanceError(
            f"contact sheet lacks the selected blue scene or its white subjects: blue={blue_ratio:.3f}, white={white_ratio:.3f}"
        )


def _probe_media(path: Path) -> dict:
    command = [
        os.getenv("CASSETTE_FFPROBE_BIN", "ffprobe"),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise AcceptanceError(f"ffprobe could not inspect export: {(result.stderr or '')[-500:]}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise AcceptanceError("ffprobe returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise AcceptanceError("ffprobe returned a non-object")
    return payload


def _extract_rgb_frame(path: Path, at_seconds: float) -> bytes:
    command = [
        os.getenv("CASSETTE_FFMPEG_BIN", "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{at_seconds:.3f}",
        "-i",
        str(path),
        "-vf",
        "scale=320:180",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, timeout=60, check=False)
    if result.returncode != 0 or len(result.stdout) != 320 * 180 * 3:
        raise AcceptanceError(f"failed to decode sampled export frame at {at_seconds:.3f}s")
    return result.stdout


def _frame_metrics(frame: bytes) -> dict[str, float]:
    blue = 0
    white_top = 0
    lower_white_x: list[int] = []
    for index in range(0, len(frame), 3):
        red, green, blue_value = frame[index : index + 3]
        pixel = index // 3
        x = pixel % 320
        y = pixel // 320
        if blue_value > 80 and blue_value > red * 1.25 and blue_value > green * 1.1:
            blue += 1
        if min(red, green, blue_value) > 210:
            if y < 55:
                white_top += 1
            elif 60 <= y <= 125:
                lower_white_x.append(x)
    return {
        "blue_ratio": blue / (320 * 180),
        "text_white_ratio": white_top / (320 * 55),
        "circle_white_pixels": float(len(lower_white_x)),
        "circle_centroid_x": sum(lower_white_x) / len(lower_white_x) if lower_white_x else -1.0,
    }


def _audio_packet_span(path: Path) -> float:
    command = [
        os.getenv("CASSETTE_FFPROBE_BIN", "ffprobe"),
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "packet=pts_time,duration_time",
        "-of",
        "csv=p=0",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise AcceptanceError("ffprobe could not read export audio packets")
    starts: list[float] = []
    ends: list[float] = []
    for line in result.stdout.splitlines():
        fields = line.split(",")
        if not fields or not fields[0].strip():
            continue
        try:
            start = float(fields[0])
            packet_duration = float(fields[1]) if len(fields) > 1 and fields[1].strip() else 0.0
        except ValueError:
            continue
        starts.append(start)
        ends.append(start + packet_duration)
    if not starts:
        raise AcceptanceError("export contains no decodable audio packets")
    return max(ends) - min(starts)


def _assert_independent_export_qc(path: Path) -> dict:
    probe = _probe_media(path)
    streams = [stream for stream in probe.get("streams") or [] if isinstance(stream, dict)]
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise AcceptanceError("export must contain independently decodable video and audio streams")
    try:
        duration = float((probe.get("format") or {}).get("duration"))
        fps = float(Fraction(str(video.get("avg_frame_rate") or video.get("r_frame_rate"))))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise AcceptanceError("ffprobe omitted export duration or frame rate") from exc
    if not 4.5 <= duration <= 7.5:
        raise AcceptanceError(f"independent export duration should be about 6 seconds, got {duration}")
    if (int(video.get("width") or 0), int(video.get("height") or 0)) != (1280, 720):
        raise AcceptanceError(f"independent export resolution mismatch: {video}")
    if abs(fps - 30.0) > 0.05:
        raise AcceptanceError(f"independent export fps mismatch: {fps}")

    decode = subprocess.run(
        [
            os.getenv("CASSETTE_FFMPEG_BIN", "ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-vf",
            "blackdetect=d=0.1:pic_th=0.98",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if decode.returncode != 0:
        raise AcceptanceError(f"full export decode failed: {(decode.stderr or '')[-500:]}")
    black_durations: list[float] = []
    for line in decode.stderr.splitlines():
        marker = "black_duration:"
        if marker not in line:
            continue
        raw = line.split(marker, 1)[1].split()[0]
        try:
            black_durations.append(float(raw))
        except ValueError:
            continue
    if any(value > 0.25 for value in black_durations):
        raise AcceptanceError(f"full export decode found unexpected black video: {black_durations}")

    audio_span = _audio_packet_span(path)
    if audio_span < duration - 0.5:
        raise AcceptanceError(f"independent audio coverage is short: {audio_span:.3f}s for {duration:.3f}s video")
    metrics = [
        _frame_metrics(_extract_rgb_frame(path, at_seconds))
        for at_seconds in (0.5, duration / 2, max(0.0, duration - 0.5))
    ]
    if any(item["blue_ratio"] < 0.7 for item in metrics):
        raise AcceptanceError(f"sampled export frames are not the blue target scene: {metrics}")
    if any(item["text_white_ratio"] < 0.005 or item["circle_white_pixels"] < 100 for item in metrics):
        raise AcceptanceError(f"sampled export frames lack KEEP THIS text or the white circle: {metrics}")
    centroids = [item["circle_centroid_x"] for item in metrics]
    if max(centroids) - min(centroids) < 40:
        raise AcceptanceError(f"sampled export does not show a moving circle: {centroids}")
    return {
        "duration_sec": duration,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": fps,
        "audio_span_sec": audio_span,
        "black_segments_sec": black_durations,
        "frame_metrics": metrics,
    }


def _assert_export_qc(envelope: dict, path: Path) -> dict:
    job = (envelope.get("data") or {}).get("job") or {}
    qc = (job.get("quality") or {}).get("export_qc") or {}
    duration = float(qc.get("duration_sec") or 0)
    video = qc.get("video") or {}
    audio = qc.get("audio") or {}
    if not 4.5 <= duration <= 7.5:
        raise AcceptanceError(f"export duration should be about 6 seconds, got {duration}")
    if (int(video.get("width") or 0), int(video.get("height") or 0)) != (1280, 720):
        raise AcceptanceError(f"export resolution mismatch: {video}")
    if abs(float(video.get("fps") or 0) - 30.0) > 0.05:
        raise AcceptanceError(f"export fps mismatch: {video.get('fps')}")
    if not audio or float(audio.get("duration_sec") or duration) < duration - 0.5:
        raise AcceptanceError(f"export audio does not span the cut: {audio}")
    if any(float(segment.get("duration_sec") or 0) > 0.25 for segment in qc.get("black_segments") or []):
        raise AcceptanceError(f"export contains an unexpected black segment: {qc.get('black_segments')}")
    return {"reported": qc, "measured": _assert_independent_export_qc(path)}


def _copy_auth_configuration(target: Path) -> None:
    """Copy only protected auth/settings files into the disposable E2E config root."""
    import runtime_config

    source_root = runtime_config.config_root()
    target.mkdir(parents=True, mode=0o700)
    for name in ("credentials.json", "settings.json"):
        source = source_root / name
        if not source.is_file() or source.is_symlink():
            continue
        destination = target / name
        shutil.copy2(source, destination)
        destination.chmod(0o600)


def _read_isolated_manifest(asset_root: Path, session_id: str) -> tuple[Path, dict]:
    from core import security

    path = asset_root / "sessions" / security.safe_hash_id(session_id) / "manifest.json"
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise AcceptanceError(f"could not read isolated plugin manifest: {path}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError("isolated plugin manifest is not an object")
    return path, value


def _assert_hook_snapshot(value: dict, *, session_id: str, expected_zero_render: bool) -> int:
    if set(value) != {"sessionId", "exportRenderRequestCount"}:
        raise AcceptanceError(f"backend snapshot returned unexpected fields. Required contract: {SERVER_HOOK_CONTRACT}")
    if value.get("sessionId") != session_id:
        raise AcceptanceError(
            f"backend acceptance hook returned the wrong session. Required contract: {SERVER_HOOK_CONTRACT}"
        )
    try:
        render_count = int(value["exportRenderRequestCount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AcceptanceError(
            f"backend snapshot omitted exportRenderRequestCount. Required contract: {SERVER_HOOK_CONTRACT}"
        ) from exc
    if expected_zero_render and render_count != 0:
        raise AcceptanceError(f"backend observed {render_count} render requests before the explicit export turn")
    return render_count


def _assert_remote_retention(value: dict, *, session_id: str, idempotent: bool) -> None:
    expected = {
        "sessionId": session_id,
        "sweepCompleted": True,
        "accessibleServerObjectCount": 0,
        "accessibleGoogleFileCount": 0,
        "queueReferenceCount": 0,
        "idempotent": idempotent,
    }
    if set(value) != set(expected):
        raise AcceptanceError(
            f"backend retention proof returned unexpected fields. Required contract: {SERVER_HOOK_CONTRACT}"
        )
    for key, wanted in expected.items():
        if value.get(key) != wanted:
            raise AcceptanceError(
                f"backend retention proof mismatch for {key}: {value.get(key)!r}. Required contract: {SERVER_HOOK_CONTRACT}"
            )


def _assert_no_plugin_copies(data_root: Path, asset_root: Path) -> None:
    for class_name in ("sessions", "previews", "api_uploads", "screenshots", "exports", "jobs"):
        base = asset_root / class_name
        if base.exists() and any(path.is_file() or path.is_symlink() for path in base.rglob("*")):
            raise AcceptanceError(f"plugin-managed {class_name} copies remain after the deadline: {base}")
    state_root = data_root / "mcp-state" / "sessions"
    if state_root.exists() and any(path.is_file() or path.is_symlink() for path in state_root.rglob("*")):
        raise AcceptanceError(f"plugin MCP state remains after the deadline: {state_root}")


async def _assert_resource_expired(session: ClientSession, uri: str) -> None:
    try:
        await session.read_resource(uri)
    except Exception:  # noqa: BLE001 - MCP SDK maps the resource error by protocol version
        return
    raise AcceptanceError("artifact resource remained readable at its retention deadline")


async def run(args: argparse.Namespace) -> dict:
    hook_url = str(args.acceptance_hook_url or "").strip()
    if not hook_url and not args.allow_missing_server_hook:
        raise AcceptanceError(
            f"full acceptance requires --acceptance-hook-url. Required contract: {SERVER_HOOK_CONTRACT}"
        )
    hook = AcceptanceHook(hook_url, str(args.acceptance_hook_token or "")) if hook_url else None
    with tempfile.TemporaryDirectory(prefix="cassette-agentic-e2e-") as temporary:
        sandbox = Path(temporary)
        media = (
            generate_fixture(sandbox / "fixture")
            if args.media is None
            else args.media.expanduser().resolve(strict=True)
        )
        if not media.is_file():
            raise AcceptanceError(f"media is not a file: {media}")
        source_digest = _sha256(media)
        config_root = sandbox / "config"
        data_root = sandbox / "data"
        asset_root = sandbox / "assets"
        cache_root = sandbox / "cache"
        temp_root = sandbox / "tmp"
        for directory in (data_root, asset_root, cache_root, temp_root):
            directory.mkdir(parents=True, mode=0o700)
        _copy_auth_configuration(config_root)
        clock_file = sandbox / "retention-clock.txt"
        clock_file.write_text(str(datetime.now(timezone.utc).timestamp()), encoding="utf-8")
        environment = os.environ.copy()
        environment.update(
            {
                "CASSETTE_RUNTIME_ADAPTER": "mcp",
                "CASSETTE_MCP_HOST": args.host,
                "CASSETTE_PROJECT_ROOT": str(media.parent),
                "CASSETTE_MCP_SKIP_BOOTSTRAP": "1",
                "CASSETTE_MCP_PYTHON": sys.executable,
                "CASSETTE_MIN_JOB_TIMEOUT_SEC": "0",
                "CASSETTE_CONFIG_HOME": str(config_root),
                "CASSETTE_DATA_HOME": str(data_root),
                "CASSETTE_ASSET_ROOT": str(asset_root),
                "CASSETTE_ALLOWED_SOURCE_ROOTS": str(media.parent),
                "CASSETTE_RETENTION_TEST_MODE": "1",
                "CASSETTE_RETENTION_TEST_CLOCK_FILE": str(clock_file),
                "XDG_CACHE_HOME": str(cache_root),
                "TMPDIR": str(temp_root),
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "scripts" / "run_local_mcp.py")],
            cwd=str(media.parent),
            env=environment,
        )
        read_timeout = timedelta(seconds=max(60, args.timeout_sec + 300))
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=read_timeout) as session:
                await session.initialize()
                ingest = _structured(await session.call_tool("cassette_ingest_media", {"source_path": str(media)}))
                session_id = ingest["session_id"]
                listed = _structured(await session.call_tool("cassette_list_assets", {"session_id": session_id}))
                if len((listed.get("data") or {}).get("manifest", {}).get("assets") or []) != 1:
                    raise AcceptanceError("cassette_list_assets did not return the ingested fixture")
                _structured(
                    await session.call_tool(
                        "cassette_config",
                        {"session_id": session_id, "model": args.model, "thinking_level": args.thinking_level},
                    )
                )

                edited = _structured(
                    await session.call_tool(
                        "cassette_run_job",
                        {"session_id": session_id, "message": args.instruction, "timeout_sec": args.timeout_sec},
                        read_timeout_seconds=read_timeout,
                    )
                )
                edited = await _approve_plan_reviews(session, edited, read_timeout)
                if edited.get("phase") != "succeeded" or edited.get("artifacts"):
                    raise AcceptanceError(f"edit turn did not settle without rendering: {edited.get('phase')}")
                manifest_path, session_manifest = _read_isolated_manifest(asset_root, session_id)
                expires_at = str(session_manifest.get("expires_at") or "")
                ingest_expires_at = str(ingest.get("expires_at") or "")
                if (
                    not expires_at
                    or not ingest_expires_at
                    or _parse_timestamp(expires_at, "manifest expires_at")
                    > _parse_timestamp(ingest_expires_at, "ingest expires_at")
                ):
                    raise AcceptanceError("session deadline was missing or extended after ingest")
                assets = [asset for asset in session_manifest.get("assets") or [] if isinstance(asset, dict)]
                media_file_ids = [str(asset["media_file_id"]) for asset in assets if asset.get("media_file_id")]
                if not media_file_ids:
                    raise AcceptanceError("isolated manifest contains no uploaded mediaFileId")
                if any(Path(str(asset.get("saved_path") or "")).exists() for asset in assets):
                    raise AcceptanceError("redundant plugin-managed upload media was not removed after backend upload")
                receipt = _assert_analysis_receipt(edited, expires_at=expires_at)

                timeline = _structured(
                    await session.call_tool("cassette_timeline", {"session_id": session_id, "contact_sheet": True})
                )
                _assert_timeline(timeline)
                contact_sheet = timeline["artifacts"][0]
                if contact_sheet.get("expires_at") != expires_at:
                    raise AcceptanceError("contact sheet deadline does not match the session deadline")
                _assert_contact_sheet_is_blue(Path(contact_sheet["path"]))

                render_before = None
                if hook is not None:
                    render_before = _assert_hook_snapshot(
                        hook.call({"action": "snapshot", "sessionId": session_id, "mediaFileIds": media_file_ids}),
                        session_id=session_id,
                        expected_zero_render=True,
                    )

                exported = _structured(
                    await session.call_tool(
                        "cassette_run_job",
                        {"session_id": session_id, "message": "export it", "export": True},
                        read_timeout_seconds=read_timeout,
                    )
                )
                exported = await _approve_plan_reviews(session, exported, read_timeout)
                if exported.get("phase") == "review_required":
                    exported = _structured(
                        await session.call_tool(
                            "cassette_review_completion",
                            {
                                "job_id": exported["job_id"],
                                "decision": "export",
                                "reason": "Live acceptance verified the 6–12s source window and contact sheet.",
                            },
                            read_timeout_seconds=read_timeout,
                        )
                    )
                if exported.get("phase") != "exported" or not exported.get("artifacts"):
                    raise AcceptanceError(f"explicit export turn did not produce an artifact: {exported.get('phase')}")
                artifact = exported["artifacts"][0]
                export_path = Path(artifact["path"])
                if not export_path.is_file() or export_path.stat().st_size != artifact["size"]:
                    raise AcceptanceError("validated artifact metadata does not match the exported file")
                if artifact.get("expires_at") != expires_at:
                    raise AcceptanceError("export deadline does not match the session deadline")
                await session.read_resource(artifact["uri"])
                qc = _assert_export_qc(exported, export_path)

                remote_retention = None
                if hook is not None:
                    render_after = _assert_hook_snapshot(
                        hook.call({"action": "snapshot", "sessionId": session_id, "mediaFileIds": media_file_ids}),
                        session_id=session_id,
                        expected_zero_render=False,
                    )
                    if render_before is None or render_after <= render_before:
                        raise AcceptanceError("backend observed no new render request during the explicit export turn")

                deadline = _parse_timestamp(expires_at, "session expires_at").timestamp()
                clock_file.write_text(str(deadline), encoding="utf-8")
                await _assert_resource_expired(session, artifact["uri"])
                local_environment = {
                    "CASSETTE_ASSET_ROOT": str(asset_root),
                    "CASSETTE_DATA_HOME": str(data_root),
                    "CASSETTE_RETENTION_TEST_MODE": "1",
                    "CASSETTE_RETENTION_TEST_CLOCK_FILE": str(clock_file),
                }
                with _temporary_environment(local_environment):
                    from core import manifest as cassette_manifest

                    first_sweep = cassette_manifest.sweep_stale_artifacts()
                    second_sweep = cassette_manifest.sweep_stale_artifacts()
                if second_sweep:
                    raise AcceptanceError(f"plugin production sweeper is not idempotent: {second_sweep}")
                _assert_no_plugin_copies(data_root, asset_root)
                if manifest_path.exists():
                    raise AcceptanceError("session manifest remained after the retention sweep")

                expired_list = (
                    await session.call_tool("cassette_list_assets", {"session_id": session_id})
                ).structuredContent
                if expired_list.get("ok") or (expired_list.get("error") or {}).get("code") != "session_expired":
                    raise AcceptanceError(f"expired session did not fail closed: {expired_list}")
                resurrect = (
                    await session.call_tool(
                        "cassette_ingest_media", {"source_path": str(media), "session_id": session_id}
                    )
                ).structuredContent
                if resurrect.get("ok") or (resurrect.get("error") or {}).get("code") != "session_expired":
                    raise AcceptanceError(f"expired session was resurrected by a late ingest: {resurrect}")

                if hook is not None:
                    remote_payload = {
                        "action": "advance_and_sweep",
                        "sessionId": session_id,
                        "expiresAt": expires_at,
                        "mediaFileIds": media_file_ids,
                    }
                    first_remote = hook.call(remote_payload)
                    second_remote = hook.call(remote_payload)
                    _assert_remote_retention(first_remote, session_id=session_id, idempotent=False)
                    _assert_remote_retention(second_remote, session_id=session_id, idempotent=True)
                    remote_retention = {"first": first_remote, "second": second_remote}
                if not media.is_file() or _sha256(media) != source_digest:
                    raise AcceptanceError("acceptance changed or removed the user-owned source media")
                return {
                    "ok": hook is not None,
                    "partial": hook is None,
                    "host": args.host,
                    "transport": "api",
                    "session_id": session_id,
                    "job_id": exported["job_id"],
                    "phase": exported["phase"],
                    "timeline": {
                        "version": timeline["data"]["version"],
                        "duration_sec": timeline["data"]["duration_sec"],
                        "clips": timeline["data"]["clips"],
                    },
                    "analysis_receipt": receipt,
                    "export_qc": qc,
                    "retention": {
                        "expires_at": expires_at,
                        "plugin_first_sweep": first_sweep,
                        "plugin_second_sweep": second_sweep,
                        "server": remote_retention,
                    },
                    "artifact": {
                        "name": artifact["name"],
                        "mime_type": artifact["mime_type"],
                        "size": artifact["size"],
                        "uri": artifact["uri"],
                        "expires_at": artifact.get("expires_at"),
                    },
                }


def main() -> None:
    args = parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
