"""The Cassette transport: direct calls to the Cassette server APIs.

  auth    POST {API}/api/agent-auth/verify            -> Supabase JWT (+ registers agent session row)
  upload  POST {API}/api/media/upload/init            -> presigned PUT url
          PUT  <presigned url>                          (raw bytes)
          POST {API}/api/media/upload/complete        -> mediaFileId
          GET  {API}/api/media/upload/status?key=     -> poll until 'completed' (video)
  agent   POST {API}/api/langgraph/threads            -> thread_id
          POST {API}/api/langgraph/threads/{id}/runs  -> run_id (server-side edits commit to the project)
          GET  .../runs/{run_id}                        -> poll status
          GET  .../state                                -> interrupts (editor_navigate is answered, not driven)
          POST .../runs (command.resume)                -> satisfy interrupts headlessly
  export  POST {API}/api/export/projects/{sid}/jobs    -> render the stored project
          GET  {API}/api/export/jobs/{id}              -> poll until done
          GET  {API}/api/export/jobs/{id}/file         -> download mp4 to disk

The result dict shape (status / outputs / questions / errors / quality / final_screenshot)
is what jobs/notifier/_scrub_job/_job_report consume.

Every call above is an agent operation, export included. The plugin has one class of user --
an agent account -- and never inspects or reports a Cassette access level: a 403 anywhere here
is a server-side refusal to relay, not a tier this client is expected to explain or work around.

Wire format is coded against the Cassette server source (remotion-canvas-hotfix): the run's
config.configurable carries the full sessionContext + projectContext + runContext.connectionState
the editor sends; uploaded media is linked to the run by session id (sessionContext.mediaSessionId
== the upload x-session-id), NOT by ids in the run input; tool interrupts (editor_navigate) resume
KEYED by toolCall.id while typed interrupts resume bare; export renders the stored project by that
same session id. A run is executed by the upstream LangGraph queue worker: if it never leaves
'pending' (queue not draining) the transport fails fast with 'agent_run_not_started' rather than
hanging until the job timeout.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import runtime_config

from . import manifest
from .manifest import get_asset_root, load_manifest

# Terminal Cassette job statuses (mirror jobs.update_job terminal set).
_SUCCEEDED = "succeeded"
_FAILED = "failed"
_NEEDS_USER = "needs_user"
_CANCELLED = "cancelled"
_TIMED_OUT = "timed_out"

# LangGraph run statuses that mean "stop polling".
_LG_TERMINAL = {"success", "error", "timeout", "interrupted"}

# editor_navigate is the ONLY executionTarget:'browser' tool (catalog.ts). A headless run
# satisfies its interrupt with a no-op result that conforms to EditorNavigateOutputSchema.
_NAVIGATE_NOOP_RESULT = {
    "ok": True,
    "newVersion": 0,
    "undoCount": 0,
    "summary": "headless-noop",
    "noOp": True,
}

# The Cassette agent requires an explicit modelId (sessionContext.modelId); it errors otherwise.
# Mirror the PRODUCT model list the editor offers (cassette-config MODEL_OPTIONS) — NOT the broader
# backend agent-models.ts list — and default to the same model the UI defaults to
# (useAgentModelPrefsStore DEFAULT_MODEL), so the plugin matches the web editor. The
# plugin's model_selection holds UI labels (or is empty), so it is only forwarded when it already
# names a product model id; otherwise the configured/default model is used.
DEFAULT_AGENT_MODEL_ID = "openai/gpt-5.6-luna"
# Single source for the product model list (id + display label): cassette_config, the gateway
# /cassette_model picker, and the run-time resolver all derive from this tuple.
AGENT_MODEL_OPTIONS = (
    {"id": "openai/gpt-5.6-luna", "label": "GPT-5.6 Luna"},
    {"id": "openai/gpt-5.4-mini", "label": "GPT-5.4 Mini"},
)
_SUPPORTED_AGENT_MODEL_IDS = frozenset(option["id"] for option in AGENT_MODEL_OPTIONS)
# The plugin's model_selection stores a UI *label*, not a model id, so map the normalized
# label -> agent model id to honor the user's model choice.
# Labels are locale-independent brand names (cassette-config MODEL_OPTIONS i18n; same in zh/en).
_MODEL_LABEL_TO_ID = {
    "".join(ch for ch in option["label"].lower() if ch.isalnum()): option["id"] for option in AGENT_MODEL_OPTIONS
}
AGENT_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")
_DEFAULT_THINKING = "xhigh"  # quality-first default shared by the plugin, MCP, editor, and web demo
_API_USER_AGENT = "oh-my-cassette/1.0"
_BROWSER_VIDEO_PREPARATION_PROFILE_VERSION = "browser-preview-v1"


def _require_model_selection() -> bool:
    # Default true: a chosen-but-unresolvable model fails the job rather than silently
    # running the default.
    return _env("CASSETTE_REQUIRE_MODEL_SELECTION").lower() not in {"0", "false", "no", "off"}


def _export_on_complete(job: dict) -> bool:
    # Whether a finished turn should route to the export/review ceremony. API default is FALSE
    # (a conversational turn ends committed-but-unrendered; run_job export=true opts in per turn).
    raw = _env("CASSETTE_EXPORT_ON_COMPLETE") or str(job.get("export_on_complete", "false"))
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _auto_export() -> bool:
    # Opt-in: export directly on api-success instead of routing through the Hermes completion
    # review. Off by default, so completion normally goes through the supervisor.
    return _env("CASSETTE_API_AUTO_EXPORT").lower() in {"1", "true", "yes", "on"}


def _model_id_from_label(label: str) -> str | None:
    """Map an exact normalized product label (e.g. 'GPT-5.6 Luna') to its model id."""
    norm = "".join(ch for ch in str(label).lower() if ch.isalnum())
    if not norm:
        return None
    return _MODEL_LABEL_TO_ID.get(norm)


# Quality subkeys that _result computes from the current outcome — never carry these forward from a
# prior job's quality (they would clobber the fresh values with stale ones).
_RESULT_COMPUTED_QUALITY_KEYS = frozenset(
    {
        "transport",
        "completion_observed",
        "export_completed",
        "export_pending",
        "output_link_count",
        "local_output_count",
        "risk",
    }
)


class ApiTransportError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class _JobCancelled(Exception):
    """Raised inside a poll loop when jobs.is_cancel_requested(job_id) flips to True.

    Cancellation is cooperative in this plugin (jobs.request_cancel just sets status=cancel_requested);
    the runner must notice and stop. run_job catches this and returns a terminal 'cancelled' result so
    the downstream terminal save does not overwrite the cancel with the run's own status.
    """


def _env(name: str) -> str:
    # MCP reads only the host-neutral protected config (after process env); the web demo reads only
    # process env. Hermes retains its historical ~/.hermes/.env fallback.
    try:
        import runtime_config

        adapter = runtime_config.runtime_adapter()
        if adapter == runtime_config.MCP_ADAPTER:
            return runtime_config.mcp_env_value(name)
        if adapter == runtime_config.WEB_ADAPTER:
            return str(os.getenv(name, "") or "").strip()
    except Exception:  # noqa: BLE001 — preserve the legacy adapter below
        pass
    try:
        from . import notifier

        getter = getattr(notifier, "_runtime_env", None)
        if callable(getter):
            return str(getter(name) or "").strip()
    except Exception:  # noqa: BLE001 — fall back to the process env
        pass
    return str(os.getenv(name, "") or "").strip()


def _env_num(name: str, default, floor, *, cast=float, getter=None):
    # Shared env-number parse: read name (via _env by default, or os.getenv), coerce with cast,
    # clamp to floor, and fall back to default on missing/garbage input.
    getter = getter or _env
    try:
        return max(floor, cast(getter(name) or default))
    except (TypeError, ValueError):
        return default


# Stable Cloudflare edge for the deployed Cassette API. It routes /api requests to the current
# backend deployment; CASSETTE_URL remains the human-facing editor URL. Override with
# CASSETTE_API_URL for self-hosted / non-default deployments.
DEFAULT_CASSETTE_API_URL = runtime_config.DEFAULT_CASSETTE_API_URL

# Host-progress plumbing. A blocking run_job is the no-poll path: the host makes one tool
# call and is answered when the job is terminal. That only works if the transport can reach
# the host's progress channel from inside its own wait, so the sink is set for the duration
# of the call rather than threaded through every generic dispatch layer between them.
_HOST_PROGRESS_INTERVAL_SEC = 5.0
_PROGRESS_SINK: Callable[[float, str], None] | None = None


@contextmanager
def host_progress_sink(sink: Callable[[float, str], None] | None) -> Iterator[None]:
    """Route transport progress to `sink` for the duration of one blocking call."""
    global _PROGRESS_SINK
    previous = _PROGRESS_SINK
    _PROGRESS_SINK = sink
    try:
        yield
    finally:
        _PROGRESS_SINK = previous


def _api_base() -> str:
    """Render-server API origin serving /api/agent-auth, /api/media, /api/langgraph,
    /api/projects and /api/export. Defaults to the deployed Cassette API; override per env."""
    base = _env("CASSETTE_API_URL") or _env("CASSETTE_API_BASE_URL") or DEFAULT_CASSETTE_API_URL
    return base.rstrip("/")


def check_cassette_connectivity(url: str | None = None, timeout_sec: float | None = None) -> dict[str, Any]:
    """Cheap reachability probe the Hermes gateway runs before dispatching an instruction.

    Hits the server's unauthenticated ``/healthz``, so an expired or unprivileged
    credential still reports "reachable" — this answers "is Cassette up?", never
    "may this account use it?", which the job itself reports with far better detail.
    """
    base = (url or _api_base()).rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme in {"", "file"}:
        return {"ok": True, "status": "skipped", "reason": "local_url"}
    timeout = timeout_sec
    if timeout is None:
        try:
            timeout = max(1.0, float(_env("CASSETTE_PING_TIMEOUT_SEC") or "10"))
        except ValueError:
            timeout = 10.0
    target = f"{base}/healthz"
    try:
        request = Request(target, method="GET", headers={"User-Agent": _API_USER_AGENT})
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
        if 200 <= status < 400:
            return {"ok": True, "status": "reachable", "http_status": status}
        return {"ok": False, "code": "cassette_http_unhealthy", "http_status": status}
    except HTTPError as exc:
        # A reply of any kind proves the origin is serving; only 5xx means it is unwell.
        status = int(getattr(exc, "code", 0) or 0)
        if status and status < 500:
            return {"ok": True, "status": "reachable", "http_status": status}
        return {"ok": False, "code": "cassette_http_unhealthy", "http_status": status}
    except (TimeoutError, URLError, OSError) as exc:
        return {"ok": False, "code": "cassette_unreachable", "details": {"type": type(exc).__name__}}


# ── credential setup (unauthenticated /api/agent-auth) ────────────────────────
# scripts/setup_local_mcp.py duplicates these two calls with its own urllib helper on
# purpose: it runs before this package's virtualenv exists, so it may import only
# runtime_config. Keep both in step rather than trying to share code across that boundary.
_AGENT_AUTH_TIMEOUT_SEC = 60.0


def _post_agent_auth(path: str, payload: dict[str, Any], *, timeout_sec: float | None = None) -> tuple[int, dict, str]:
    """POST an unauthenticated /api/agent-auth call; return (status, body, retry_after).

    HTTP error statuses are returned rather than raised so callers can tell 401 from 403
    from 429. Only transport failures raise.
    """
    body = json.dumps(payload).encode("utf-8")
    target = _api_base() + path
    request = Request(
        target,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _API_USER_AGENT,
        },
    )
    timeout = timeout_sec or _AGENT_AUTH_TIMEOUT_SEC
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read().decode("utf-8", "replace")
            retry_after = response.headers.get("retry-after") or ""
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8", "replace")
        retry_after = (exc.headers.get("retry-after") if exc.headers else "") or ""
    except (TimeoutError, URLError, OSError) as exc:
        raise ApiTransportError(
            "cassette_unreachable", f"Could not reach the Cassette API ({type(exc).__name__})."
        ) from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}, str(retry_after)


def _is_edge_access_denied(body: dict[str, Any]) -> bool:
    """Identify Cloudflare's browser-signature denial instead of blaming Cassette access."""
    return str(body.get("error_code") or "") == "1010" or body.get("error_name") == "browser_signature_banned"


def _retry_after_seconds(value: str) -> int | None:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def verify_agent_credentials(email: str, password: str, *, timeout_sec: float | None = None) -> dict[str, Any]:
    """Prove an email/password pair signs in. Stores nothing and returns no token.

    The reply carries an access token and a refresh token; both are deliberately dropped.
    Nothing in this plugin persists tokens, and handing one back here would invite a caller
    to write it next to the credentials.

    The route checks the password before the allowlist, so the two failures arrive
    distinguishable: 403 means the password was right and the address has no Cassette access
    (never granted, or revoked), 401 means the password itself did not check out and says
    nothing about whether the account exists.
    """
    status, body, retry_after = _post_agent_auth(
        "/api/agent-auth/verify", {"email": email, "password": password}, timeout_sec=timeout_sec
    )
    if status == 200 and isinstance(body.get("session"), dict) and body["session"].get("access_token"):
        return {"ok": True}
    if status == 429:
        return {
            "ok": False,
            "code": "auth_rate_limited",
            "http_status": status,
            "retry_after_sec": _retry_after_seconds(retry_after),
        }
    if status == 403 and _is_edge_access_denied(body):
        return {"ok": False, "code": "auth_edge_access_denied", "http_status": status}
    if status == 403:  # password verified, address not on the allowlist
        return {"ok": False, "code": "auth_not_authorized", "http_status": status}
    if status in {400, 401}:
        return {"ok": False, "code": "auth_invalid_password", "http_status": status}
    return {"ok": False, "code": "auth_verify_failed", "http_status": status}


def request_new_agent_password(email: str, *, timeout_sec: float | None = None) -> dict[str, Any]:
    """Ask the server to replace the account password and mail the replacement.

    DESTRUCTIVE on success: the previous password stops working everywhere. The server now
    mails the replacement before it stores it, so a failure reported here almost always means
    nothing was replaced and the previous password still works -- "almost" because a store
    that succeeds and then loses its response is indistinguishable from one that never
    happened. Trying the old password is the cheap way to tell, which is what the failure
    message says to do. Upstream rate limit is 3/hour per (ip, email) and is spent before the
    allowlist is consulted, so an unauthorized address still costs an attempt.
    """
    status, body, retry_after = _post_agent_auth(
        "/api/agent-auth/request-code", {"email": email}, timeout_sec=timeout_sec
    )
    if status == 429:
        return {
            "ok": False,
            "code": "auth_rate_limited",
            "http_status": status,
            "retry_after_sec": _retry_after_seconds(retry_after),
        }
    if status == 403 and _is_edge_access_denied(body):
        return {"ok": False, "code": "auth_edge_access_denied", "http_status": status}
    if status == 200:
        # 200 with sent=false is the server refusing to confirm who has an account; no mail
        # is coming and no password was touched, so this is the one safe negative here.
        if body.get("sent") is True:
            return {"ok": True}
        return {"ok": False, "code": "auth_not_authorized", "http_status": status}
    return {"ok": False, "code": "auth_password_request_failed", "http_status": status}


def _parse_volume_levels(ffmpeg_stderr: str) -> dict | None:
    """Mean and peak dBFS from an ffmpeg volumedetect pass.

    Kept separate from the subprocess call so the parse is testable without ffmpeg.
    volumedetect prints one summary per input; the last match wins so a filtergraph
    that ends up reporting twice still yields the figures for the audio actually read.
    """
    mean = peak = None
    for match in re.finditer(r"mean_volume:\s*(-?[0-9.]+) dB", ffmpeg_stderr or ""):
        mean = match.group(1)
    for match in re.finditer(r"max_volume:\s*(-?[0-9.]+) dB", ffmpeg_stderr or ""):
        peak = match.group(1)
    if mean is None and peak is None:
        return None
    try:
        return {
            "mean_dbfs": round(float(mean), 1) if mean is not None else None,
            "peak_dbfs": round(float(peak), 1) if peak is not None else None,
        }
    except (TypeError, ValueError):
        return None


def _parse_black_segments(ffmpeg_stderr: str) -> list[dict]:
    """Black stretches from an ffmpeg blackdetect pass, in file order.

    Kept separate from the subprocess call so the parse is testable without ffmpeg.
    """
    segments: list[dict] = []
    for match in re.finditer(
        r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)",
        ffmpeg_stderr or "",
    ):
        try:
            start, end, duration = (round(float(match.group(i)), 3) for i in (1, 2, 3))
        except (TypeError, ValueError):
            continue
        segments.append({"start_sec": start, "end_sec": end, "duration_sec": duration})
    return segments


def _probe_duration_sec(file_path: Path) -> float | None:
    """Container duration in seconds via ffprobe, or None when it cannot be determined.

    Only video uploads get a server-side probe; the readiness pipeline (remux, VFR, HEVC,
    thumbnail) is video-shaped and audio never enters it. With no probe and no client
    metadata the agent's media import substitutes a hardcoded 3 seconds, so a 159-second
    music track is planned against as if it were 3 — the edit is then built to the wrong
    length and the agent burns its run reconciling the mismatch.

    Reading a container header is not media analysis: creative decisions stay with
    Cassette, this only reports how long the file the caller handed us actually is.
    """
    binary = str(_env("CASSETTE_FFPROBE_BIN") or os.getenv("CASSETTE_FFPROBE_BIN", "") or "ffprobe")
    try:
        proc = subprocess.run(
            [binary, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(file_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        raw = (json.loads(proc.stdout or "{}").get("format") or {}).get("duration")
        duration = float(raw)
    except (AttributeError, TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _parse_frame_rate(raw: object) -> float | None:
    try:
        rate = float(Fraction(str(raw)))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _probe_browser_video_source(file_path: Path) -> dict[str, Any]:
    """Validate the source against the browser-preparation contract using ffprobe."""
    binary = str(_env("CASSETTE_FFPROBE_BIN") or os.getenv("CASSETTE_FFPROBE_BIN", "") or "ffprobe")
    entries = (
        "format=duration:stream=codec_type,codec_name,codec_tag_string,width,height,"
        "avg_frame_rate,r_frame_rate,channels,pix_fmt,color_transfer,bits_per_raw_sample"
    )
    try:
        proc = subprocess.run(
            [binary, "-v", "error", "-show_entries", entries, "-of", "json", str(file_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApiTransportError(
            "browser_video_preparation_failed",
            "ffprobe is required to prepare video uploads for Cassette.",
        ) from exc
    if proc.returncode != 0:
        raise ApiTransportError(
            "browser_video_preparation_failed",
            "Cassette could not inspect this MP4 for browser-compatible upload.",
        )
    try:
        payload = json.loads(proc.stdout or "{}")
        streams = payload.get("streams") or []
        video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if len(video_streams) != 1 or len(audio_streams) > 1:
            raise ValueError("unsupported track layout")
        video = video_streams[0]
        audio = audio_streams[0] if audio_streams else None
        duration = float((payload.get("format") or {}).get("duration"))
        width = int(video.get("width"))
        height = int(video.get("height"))
        avg_rate = _parse_frame_rate(video.get("avg_frame_rate"))
        raw_rate = _parse_frame_rate(video.get("r_frame_rate"))
        channels = int(audio.get("channels")) if audio else 0
    except (AttributeError, TypeError, ValueError) as exc:
        raise ApiTransportError(
            "browser_video_preparation_failed",
            "This video does not expose the metadata required by Cassette's browser upload profile.",
        ) from exc

    landscape = width >= height
    if (
        video.get("codec_name") != "h264"
        or duration <= 0
        or duration > 60 * 60
        or width <= 0
        or height <= 0
        or (landscape and (width > 1920 or height > 1080))
        or (not landscape and (width > 1080 or height > 1920))
        or avg_rate is None
        or raw_rate is None
        or abs(avg_rate - raw_rate) > 0.05
        or min(abs(avg_rate - 30), abs(avg_rate - (30_000 / 1_001))) > 0.05
        or (audio is not None and (audio.get("codec_name") != "aac" or channels not in {1, 2}))
        or str(video.get("color_transfer") or "").lower() in {"smpte2084", "arib-std-b67"}
        or str(video.get("bits_per_raw_sample") or "8") not in {"", "8"}
        or "10" in str(video.get("pix_fmt") or "")
    ):
        raise ApiTransportError(
            "browser_video_unsupported",
            "This video does not meet Cassette's MP4/AVC, SDR, CFR 30 fps browser upload profile.",
        )

    return {
        "container": "mp4",
        "videoCodec": "avc",
        "videoCodecString": str(video.get("codec_tag_string") or "avc1"),
        "width": width,
        "height": height,
        "durationSeconds": duration,
        "frameRate": avg_rate,
        "frameRateIsConstant": True,
        "hasAudio": audio is not None,
        "audioCodec": "aac" if audio is not None else "none",
        "audioChannels": channels,
    }


@contextmanager
def _prepare_browser_video_preview(file_path: Path) -> Iterator[tuple[dict[str, Any], Path]]:
    """Create the 30 fps AVC/AAC proxy required by the deployed browser-upload contract."""
    source = _probe_browser_video_source(file_path)
    max_width, max_height = (1280, 720) if source["width"] >= source["height"] else (720, 1280)
    request = {
        "profileVersion": _BROWSER_VIDEO_PREPARATION_PROFILE_VERSION,
        "previewRequired": True,
        "source": source,
    }
    ffmpeg = str(_env("CASSETTE_FFMPEG_BIN") or os.getenv("CASSETTE_FFMPEG_BIN", "") or "ffmpeg")
    with tempfile.TemporaryDirectory(prefix="oh-my-cassette-preview-") as directory:
        target = Path(directory) / "preview.mp4"
        command = [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(file_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            f"scale={max_width}:{max_height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
        ]
        if source["hasAudio"]:
            command.extend(["-c:a", "aac", "-ac", str(source["audioChannels"])])
        else:
            command.append("-an")
        command.extend(["-movflags", "+faststart", str(target)])
        try:
            proc = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(120, min(1800, int(source["durationSeconds"] * 4))),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApiTransportError(
                "browser_video_preparation_failed",
                "ffmpeg could not create the browser-prepared Cassette preview.",
            ) from exc
        if proc.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
            raise ApiTransportError(
                "browser_video_preparation_failed",
                "ffmpeg could not create the browser-prepared Cassette preview.",
            )
        yield request, target


def _credentials() -> tuple[str, str]:
    email = _env("CASSETTE_AUTH_EMAIL") or _env("CASSETTE_AUTH_ACCOUNT") or _env("CASSETTE_EMAIL")
    password = _env("CASSETTE_AUTH_PASSWORD") or _env("CASSETTE_PASSWORD")
    return email, password


def _exports_dir(job_id: str) -> Path:
    path = Path(os.getenv("CASSETTE_ASSET_ROOT", str(get_asset_root()))) / "exports" / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_id(job: dict) -> str:
    return str(job.get("cassette_session_id") or job.get("session_hash") or "default")


AGENT_SESSION_PREFIX = "agent-session-"
TRY_SESSION_PREFIX = "try-session-"

# Namespace -> editor route. agent-session-* is the plugin's namespace: the server classifies
# those requests as the `agent` access tier, so the JWT the plugin already sends is honoured
# (rate limits keyed on the user, the account's real access level applies). try-session-* is the
# anonymous publicTry namespace, kept only so sessions minted before 0.4.6 keep a working link.
SESSION_PREFIX_ROUTES = {AGENT_SESSION_PREFIX: "/agent", TRY_SESSION_PREFIX: "/try"}


def split_session_prefix(session_id: str) -> tuple[str, str] | None:
    """(prefix, hash) for a namespaced session id, else None for un-prefixed legacy ids."""
    for prefix in SESSION_PREFIX_ROUTES:
        if session_id.startswith(prefix):
            return prefix, session_id[len(prefix) :]
    return None


# A `…?projectSessionId=<id>&chatSessionId=<uuid>` deep link used to be built here and
# returned on every job envelope. It is a bearer capability: the backend binds no owner to
# a scratch session, so the only checks on that route are "signed in" and "knows the id" —
# any authenticated account that sees the link can open the project AND run edits on the
# thread. A link with that reach does not belong in tool output that gets relayed into chat
# transcripts, logs, and screen recordings, so the runtime no longer emits one.
#
# Removing it narrows exposure; it does not close it. The route still resolves for anyone
# who reconstructs the URL — the ownership binding has to happen server-side.


def _http_timeout() -> float:
    return _env_num("CASSETTE_API_HTTP_TIMEOUT_SEC", 60.0, 5.0)


def _unattended() -> bool:
    # One switch restoring today's fully headless semantics (auto-approve plans, classifier-answered
    # questions) for CI-style runs.
    return _env("CASSETTE_UNATTENDED").lower() in {"1", "true", "yes", "on"}


def _plan_review_mode() -> str:
    """'user' surfaces edit_plan_review as a real question; 'auto' keeps silent approval.

    Default: 'user' on MCP hosts (the person is present in the chat), 'auto' for gateway/Hermes
    background jobs where a blocking review would stall unattended pipelines."""
    value = _env("CASSETTE_PLAN_REVIEW").lower()
    if value in {"user", "auto"}:
        return value
    try:
        import runtime_config

        if runtime_config.runtime_adapter() == runtime_config.MCP_ADAPTER:
            return "user"
    except Exception:  # noqa: BLE001
        pass
    return "auto"


def _plan_review_resume(answer: str) -> dict:
    """Map a free-text reply onto the bare PlanReviewDecision the graph expects."""
    text = str(answer or "").strip()
    lowered = text.lower()
    if lowered in {"approve", "approved", "yes", "ok", "okay", "lgtm", "同意", "批准", "可以"} or lowered.startswith(
        "approve"
    ):
        return {"action": "approve"}
    if lowered in {"reject", "rejected", "no", "cancel", "拒绝", "取消"}:
        return {"action": "reject"}
    if lowered.startswith("revise"):
        feedback = text[len("revise") :].strip(" :,-") or "Please revise the plan."
        return {"action": "revise", "feedback": feedback}
    # Any other free text is revision feedback — the safest reading of "change it to…".
    return {"action": "revise", "feedback": text}


def _stream_enabled() -> bool:
    # SSE event listener (timeline deltas + plan progress). Default on; CASSETTE_API_STREAM=0
    # restores a pure-poll transport. Status polling stays the run driver either way — the
    # stream is the event channel, never the completion signal.
    return _env("CASSETTE_API_STREAM").lower() not in {"0", "false", "no", "off"}


def _stream_read_timeout() -> float:
    return _env_num("CASSETTE_API_STREAM_TIMEOUT_SEC", 900.0, 30.0)


# Stream modes requested for every run so a later join (plugin listener OR the user's /try tab)
# replays commit/plan events; 'custom' carries ProjectCommitEvents, 'updates' the node progress.
# Mirror the editor's AGENT_STREAM_MODES exactly: a /try tab that live-joins a plugin run gets
# the same event surface as its own submits (chat tokens, tool events, values, custom commits).
# The plugin's own SSE consumer reads only 'custom' frames, so the extra modes cost bandwidth only.
_RUN_STREAM_MODES = ["values", "messages-tuple", "custom", "tools"]


class ApiTransport:
    def __init__(self) -> None:
        self._token: str | None = None
        self._active_stream_stop: threading.Event | None = None
        # Progress state (reset per run in _init_progress; defaults keep helpers safe on the export path).
        self._job: dict | None = None
        self._stage_timings: dict[str, dict] = {}
        self._current_stage: str = ""
        self._last_event: float = 0.0
        self._last_heartbeat: float = 0.0
        self._run_started: float = 0.0
        self._last_terminal_outcome: str | None = None
        self._analysis_receipts: list[dict[str, Any]] = []
        self._uploaded_expiries: dict[str, str] = {}

    @staticmethod
    def _require_job_active(job: dict) -> None:
        expires_at = job.get("expires_at")
        if expires_at and manifest.is_expired(expires_at):
            raise ApiTransportError(
                "session_expired",
                "This Cassette media session reached its 24-hour retention deadline.",
                details={"expires_at": expires_at},
            )

    @staticmethod
    def _bounded_deadline(job: dict, budget_sec: float) -> float:
        remaining = manifest.seconds_until_expiry(job.get("expires_at"))
        if remaining is not None and remaining <= 0:
            ApiTransport._require_job_active(job)
        return time.monotonic() + min(budget_sec, remaining) if remaining is not None else time.monotonic() + budget_sec

    # ── public Transport surface ──────────────────────────────────────────────
    def check_available(self) -> bool:
        if _env("CASSETTE_AUTH_TOKEN"):
            return bool(_api_base())
        email, password = _credentials()
        return bool(_api_base() and email and password)

    def close_sessions(self, session_key: str | None = None) -> None:
        # Stateless over HTTP — just drop the cached token.
        self._token = None

    def export(self, job: dict, decision: dict[str, Any] | None = None) -> dict:
        # Re-drive/collect the export for a Hermes-reviewed completion. Seed from the job so the
        # accumulated questions/errors and prior quality survive; the review decision is
        # recorded in quality.completion_review.
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        decision = decision or {}
        outputs: list[dict] = []
        questions = list(job.get("questions") or [])
        errors = list(job.get("errors") or [])
        prior_quality = dict(job.get("quality") or {})
        export_deadline = self._bounded_deadline(job, self._export_timeout(job))
        self._init_progress(job)
        self._enter_stage(job_id, "export", "Rendering the reviewed export")
        try:
            self._require_job_active(job)
            self._authenticate()
            outputs = self._export_project(session_id, job_id, deadline=export_deadline)
        except _JobCancelled:
            return self._result(
                _CANCELLED,
                questions=questions,
                errors=errors,
                completion_observed=bool(prior_quality.get("completion_observed")),
                export_completed=False,
                risk="medium",
                extra_quality={k: v for k, v in prior_quality.items() if k not in _RESULT_COMPUTED_QUALITY_KEYS},
                final_screenshot=job.get("final_screenshot"),
            )
        except ApiTransportError as exc:
            errors.append(self._error(exc))
        except Exception as exc:  # noqa: BLE001 — never let export crash the job record
            errors.append(self._error(exc))
        # Carry forward only the DESCRIPTIVE prior-quality keys; the outcome keys _result computes
        # (export_pending/completion_observed/risk/…) must reflect THIS export, not the stale run.
        carried = {k: v for k, v in prior_quality.items() if k not in _RESULT_COMPUTED_QUALITY_KEYS}
        review_quality = {
            "completion_source": "hermes_completion_review",
            "completion_review": {
                "decision": str(decision.get("decision") or "export"),
                "reason": str(decision.get("reason") or "")[:500],
            },
            "progress_summary": str(decision.get("summary") or prior_quality.get("progress_summary") or "")[:700]
            or None,
            "current_stage": "export",
        }
        return self._result(
            _SUCCEEDED if outputs else _FAILED,
            outputs=outputs,
            questions=questions,
            errors=errors,
            completion_observed=bool(outputs) or bool(prior_quality.get("completion_observed")),
            export_completed=bool(outputs),
            export_pending=not outputs,
            risk="low" if outputs else "high",
            extra_quality={**carried, **review_quality},
            final_screenshot=self._export_thumbnail(outputs) or job.get("final_screenshot"),
        )

    def run_job(self, job: dict) -> dict:
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        # Direct line: the agent hears the user's verbatim words (message), matching the web chat
        # box. chat_message (the user-facing text of legacy briefs) beats the make_prompt wrapper.
        prompt = str(job.get("message") or job.get("chat_message") or job.get("prompt") or "").strip()
        asset_paths = [p for p in (job.get("asset_paths") or []) if p]
        cached_media_file_ids = [str(value) for value in (job.get("media_file_ids") or []) if value]
        questions: list[dict] = []
        errors: list[dict] = []
        deadline = self._bounded_deadline(job, self._job_timeout(job))
        self._init_progress(job)

        try:
            self._require_job_active(job)
            if not _api_base():
                raise ApiTransportError("api_base_missing", "CASSETTE_API_URL is not configured for the API transport")
            self._raise_if_cancelled(job_id)
            self._authenticate()

            media_file_ids: list[str] = []
            if asset_paths:
                self._enter_stage(job_id, "upload", "Uploading media to Cassette")
                media_file_ids = self._upload_assets(
                    asset_paths, session_id, deadline, job_id, self._display_names(job)
                )
            if cached_media_file_ids:
                available = self._available_remote_media(session_id, cached_media_file_ids)
                missing = sorted(set(cached_media_file_ids) - available)
                if missing:
                    raise ApiTransportError(
                        "media_reingest_required",
                        "Previously uploaded media is no longer available. Ingest the source into a new session.",
                        details={"missing_count": len(missing)},
                    )
                media_file_ids.extend(value for value in cached_media_file_ids if value not in media_file_ids)

            # Media analysis evidence and the render source are generated asynchronously after upload.
            # Starting the run early makes the agent commit an empty edit ("succeeds" but exports
            # a blank 1-frame video); exporting
            # early fails with "render-source is missing". Wait for full readiness first.
            if media_file_ids:
                self._enter_stage(job_id, "media_ready", "Processing uploaded media")
                self._await_media_ready(session_id, media_file_ids, deadline, job_id)

            self._notify_model_selection(job, self._resolve_model_id(job), self._resolve_thinking_config(job))
            self._enter_stage(job_id, "agent", "Cassette agent is editing")
            thread_id = self._ensure_thread(session_id, job)
            run_status, run_questions = self._run_agent(thread_id, session_id, prompt, job, deadline)
            questions.extend(run_questions)

            if run_status == _NEEDS_USER:
                return self._result(
                    _NEEDS_USER,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    risk="medium",
                    extra_quality={
                        "progress_summary": self._questions_summary(questions),
                        # Plan review is judged against the timeline, so attach the digest.
                        **(
                            self._timeline_review_context(session_id, questions)
                            if any(q.get("reason") == "edit_plan_review" for q in questions)
                            else {}
                        ),
                    },
                )
            if run_status == _TIMED_OUT:
                errors.append(
                    {
                        "code": "agent_run_timeout",
                        "message": "Agent run did not finish before the job timeout",
                        "details": {},
                    }
                )
                return self._result(
                    _TIMED_OUT,
                    questions=questions,
                    errors=errors,
                    completion_observed=False,
                    export_completed=False,
                    risk="medium",
                )
            if run_status != _SUCCEEDED:
                errors.append(
                    {
                        "code": "agent_run_incomplete",
                        "message": f"Agent run ended with status '{run_status}'",
                        "details": {},
                    }
                )
                return self._result(
                    _FAILED,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    risk="high",
                )

            edit_summary = self._latest_agent_summary(thread_id) or "Cassette reports the requested edit is complete."

            # The agent committed the edit. Unless auto-export is opted into, hand completion to the
            # Hermes supervisor for semantic review (export/continue/needs_user/failed) via a
            # needs_user gate that cassette_review_completion -> ApiTransport.export() resolves.
            # Only auto-export when CASSETTE_API_AUTO_EXPORT is set, which treats the agent's own
            # success signal as authoritative and skips the review.
            if _export_on_complete(job) and not _auto_export():
                questions.append(
                    {
                        "question": edit_summary[:500],
                        "requires_user": False,
                        "reason": "completion_requires_hermes_review",
                        "answer": (
                            "The latest Cassette reply needs Hermes supervisor semantic review before deciding "
                            "whether to export, continue, fail, or ask the user."
                        ),
                    }
                )
                return self._result(
                    _NEEDS_USER,
                    questions=questions,
                    errors=errors,
                    completion_observed=False,
                    export_completed=False,
                    risk="medium",
                    extra_quality={
                        "completion_review_required": True,
                        "completion_source": "cassette_agent_success",
                        "progress_summary": edit_summary,
                        "current_stage": "agent",
                        # The export gate must never again be judged blind: attach the timeline
                        # digest + contact sheet so the reviewer sees what would be exported.
                        **self._timeline_review_context(session_id),
                    },
                )
            if not _export_on_complete(job) and not _auto_export():
                # Conversational turn: the edit is committed, nothing rendered. Attach the CTL
                # digest + contact sheet as the per-turn preview so the user judges the result
                # visually (0.4.0 preview stack) and decides when to export.
                return self._result(
                    _SUCCEEDED,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    export_pending=False,
                    risk="medium",
                    extra_quality={
                        "progress_summary": edit_summary,
                        "current_stage": "agent",
                        **self._timeline_review_context(session_id),
                    },
                )

            # Auto-export (opt-in). If it fails, the edit still happened, so report 'succeeded' with
            # export_pending rather than masking it as a failure (consumed by _job_report).
            self._enter_stage(job_id, "export", "Rendering the export")
            try:
                outputs = self._export_project(session_id, job_id, deadline=deadline)
            except _JobCancelled:
                raise
            except ApiTransportError as exc:
                errors.append(self._error(exc))
                return self._result(
                    _SUCCEEDED,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    export_pending=True,
                    risk="medium",
                    extra_quality={
                        "progress_summary": edit_summary
                        or "Cassette edit committed; the export did not complete in time.",
                        "current_stage": "export",
                    },
                )
            has_local = any(o.get("local_path") for o in outputs)
            return self._result(
                _SUCCEEDED,
                outputs=outputs,
                questions=questions,
                errors=errors,
                completion_observed=True,
                export_completed=bool(outputs),
                export_pending=not outputs,
                risk="low" if has_local else "medium",
                extra_quality={"progress_summary": edit_summary or None, "current_stage": "export"},
                final_screenshot=self._export_thumbnail(outputs),
            )
        except _JobCancelled:
            return self._result(
                _CANCELLED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="medium",
                extra_quality={"progress_summary": "Cassette job was cancelled before it finished."},
            )
        except ApiTransportError as exc:
            errors.append(self._error(exc))
            return self._result(
                _FAILED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="high",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(self._error(exc))
            return self._result(
                _FAILED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="high",
            )

    def resume(self, job: dict, response: str) -> dict:
        """Resume a persisted API ``ask_user`` interrupt on the same LangGraph thread."""
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        continuation = job.get("continuation") if isinstance(job.get("continuation"), dict) else {}
        questions = list(job.get("questions") or [])
        errors = list(job.get("errors") or [])
        deadline = self._bounded_deadline(job, self._job_timeout(job))
        self._init_progress(job)
        try:
            self._require_job_active(job)
            if continuation.get("transport") != "api":
                raise ApiTransportError(
                    "resume_state_missing",
                    "This API job has no persisted continuation state to resume.",
                )
            thread_id = str(continuation.get("thread_id") or "")
            config = continuation.get("config")
            if not thread_id or not isinstance(config, dict):
                raise ApiTransportError(
                    "resume_state_missing",
                    "The persisted API continuation is incomplete.",
                )
            answer = str(response or "").strip()
            if not answer:
                raise ApiTransportError("missing_required_arg", "response is required to resume a Cassette job")
            self._authenticate()
            interrupts = self._pending_interrupts(thread_id)
            pending_kinds = {self._interrupt_kind(item.get("value")) for item in interrupts}
            if "edit_plan_review" in pending_kinds:
                # Bare PlanReviewDecision — approve / revise <feedback> / reject, mapped from the
                # user's free-text reply. First-answer-wins with an open editor tab: if the tab
                # already decided, the pending set is empty and we raise the same clean error.
                resume_payload: dict[str, Any] = _plan_review_resume(answer)
            elif "ask_user" in pending_kinds:
                resume_payload = {"action": "respond", "userResponse": answer}
            else:
                raise ApiTransportError(
                    "resume_not_waiting_for_user",
                    "The persisted API thread is no longer waiting for a user response.",
                )
            self._enter_stage(job_id, "agent", "Resuming the Cassette agent")
            run_id = self._post_run(
                thread_id,
                {
                    "assistant_id": "cassette-chat",
                    "command": {"resume": resume_payload},
                    "config": config,
                    "multitask_strategy": "interrupt",
                    "stream_mode": _RUN_STREAM_MODES,
                },
            )
            self._persist_continuation(job_id, thread_id, session_id, config, run_id, interrupts=[])
            run_status, new_questions = self._drive_run(thread_id, run_id, config, job, deadline)
            questions.append(
                {
                    "question": "Cassette requested user input.",
                    "requires_user": False,
                    "reason": "user_response",
                    "answer": "Response supplied by the user.",
                }
            )
            questions.extend(new_questions)
            if run_status == _NEEDS_USER:
                return self._result(
                    _NEEDS_USER,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    risk="medium",
                    extra_quality={
                        "progress_summary": self._questions_summary(questions),
                        # Plan review is judged against the timeline, so attach the digest.
                        **(
                            self._timeline_review_context(session_id, questions)
                            if any(q.get("reason") == "edit_plan_review" for q in questions)
                            else {}
                        ),
                    },
                )
            if run_status == _TIMED_OUT:
                errors.append(
                    {
                        "code": "agent_run_timeout",
                        "message": "Agent run did not finish before the job timeout",
                        "details": {},
                    }
                )
                return self._result(
                    _TIMED_OUT,
                    questions=questions,
                    errors=errors,
                    completion_observed=False,
                    export_completed=False,
                    risk="medium",
                )
            if run_status != _SUCCEEDED:
                raise ApiTransportError("agent_run_incomplete", f"Agent run ended with status '{run_status}'")

            edit_summary = self._latest_agent_summary(thread_id) or "Cassette reports the requested edit is complete."
            if _export_on_complete(job) and not _auto_export():
                questions.append(
                    {
                        "question": edit_summary[:500],
                        "requires_user": False,
                        "reason": "completion_requires_hermes_review",
                        "answer": "The latest Cassette reply needs completion review before export.",
                    }
                )
                return self._result(
                    _NEEDS_USER,
                    questions=questions,
                    errors=errors,
                    completion_observed=False,
                    export_completed=False,
                    risk="medium",
                    extra_quality={
                        "completion_review_required": True,
                        "completion_source": "cassette_agent_success",
                        "progress_summary": edit_summary,
                        "current_stage": "agent",
                        # The export gate must never again be judged blind: attach the timeline
                        # digest + contact sheet so the reviewer sees what would be exported.
                        **self._timeline_review_context(session_id),
                    },
                )
            if not _export_on_complete(job) and not _auto_export():
                return self._result(
                    _SUCCEEDED,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    export_pending=False,
                    risk="medium",
                    extra_quality={
                        "progress_summary": edit_summary,
                        "current_stage": "agent",
                        **self._timeline_review_context(session_id),
                    },
                )
            self._enter_stage(job_id, "export", "Rendering the export")
            outputs = self._export_project(session_id, job_id, deadline=deadline)
            return self._result(
                _SUCCEEDED,
                outputs=outputs,
                questions=questions,
                errors=errors,
                completion_observed=True,
                export_completed=bool(outputs),
                export_pending=not outputs,
                risk="low" if outputs else "medium",
                extra_quality={"progress_summary": edit_summary, "current_stage": "export"},
                final_screenshot=self._export_thumbnail(outputs),
            )
        except _JobCancelled:
            return self._result(
                _CANCELLED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="medium",
            )
        except ApiTransportError as exc:
            errors.append(self._error(exc))
            return self._result(
                _FAILED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="high",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(self._error(exc))
            return self._result(
                _FAILED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="high",
            )

    # ── auth ──────────────────────────────────────────────────────────────────
    def _authenticate(self) -> None:
        if self._token:
            return
        override = _env("CASSETTE_AUTH_TOKEN")
        if override:
            # Pre-issued bearer (local dev's local-dev-access-token, CI-minted JWTs); skips
            # /api/agent-auth/verify entirely.
            self._token = override
            return
        email, password = _credentials()
        if not email or not password:
            raise ApiTransportError(
                "auth_missing_credentials", "CASSETTE_AUTH_EMAIL/PASSWORD are required for the API transport"
            )
        status, body = self._request(
            "POST", "/api/agent-auth/verify", json_body={"email": email, "password": password}, authed=False
        )
        if status in {400, 401}:
            # "Stale", not "wrong": Cassette passwords are server-generated and mailed, never
            # chosen, so the user did not mistype anything. Replacing it is the only fix.
            raise ApiTransportError(
                "auth_failed",
                f"The stored Cassette password is no longer valid (HTTP {status}); it needs replacing.",
            )
        if status == 403 and isinstance(body, dict) and _is_edge_access_denied(body):
            raise ApiTransportError(
                "auth_edge_access_denied",
                "Cloudflare denied the plugin HTTP client before Cassette could verify the account.",
            )
        if status == 403:
            raise ApiTransportError(
                "auth_not_authorized",
                "The stored Cassette account is not authorized for agent access.",
            )
        if status != 200 or not isinstance(body, dict):
            raise ApiTransportError(
                "auth_verify_failed",
                f"Cassette credential verification failed (HTTP {status}); the stored password was not changed.",
            )
        session = body.get("session") or {}
        token = session.get("access_token")
        if not token:
            raise ApiTransportError("auth_failed", "Cassette sign-in returned no access token")
        self._token = str(token)

    # ── media upload ────────────────────────────────────────────────────────
    def _upload_asset(
        self, path: str, session_id: str, deadline: float, job_id: str = "", display_name: str = ""
    ) -> str:
        self._raise_if_cancelled(job_id)
        file_path = Path(path)
        if not file_path.exists():
            raise ApiTransportError("asset_missing", f"Asset not found on disk: {path}")
        # Session media is stored content-addressed (<sha256><ext>), so the on-disk name carries no
        # meaning. Uploading that hash makes every instruction that names a file ("use jazz1", "put
        # Beach.mp4 first") unresolvable against the agent's media catalog, and the agent has to
        # stop and ask which asset was meant.
        file_name = display_name or file_path.name
        mime, _ = mimetypes.guess_type(file_name)
        mime = mime or "application/octet-stream"
        # The editor scopes uploads by BOTH x-session-id (media catalog the agent reads) and
        # x-project-id (project<->asset binding used by export). Send the same id for both so the
        # uploaded media is visible to the agent run AND bound to the project that gets exported.
        headers = {"x-session-id": session_id, "x-project-id": session_id}

        init_status, init = self._request(
            "POST",
            "/api/media/upload/init",
            json_body={"fileName": file_name, "mimeType": mime},
            headers=headers,
        )
        requires_browser_preparation = (
            init_status == 428 and isinstance(init, dict) and init.get("code") == "BROWSER_VIDEO_PREPARATION_REQUIRED"
        )
        if init_status != 200 and not requires_browser_preparation:
            detail = init.get("error") if isinstance(init, dict) else None
            raise ApiTransportError(
                "http_error",
                f"POST /api/media/upload/init -> HTTP {init_status}{f': {detail}' if detail else ''}",
                details={"status": init_status, "path": "/api/media/upload/init"},
            )
        if requires_browser_preparation and not str(mime).startswith("video/"):
            raise ApiTransportError(
                "browser_video_preparation_failed",
                "Cassette requested browser video preparation for a non-video asset.",
            )

        preparation = (
            _prepare_browser_video_preview(file_path) if requires_browser_preparation else nullcontext((None, None))
        )
        with preparation as (video_preparation, preview_path):
            if video_preparation is not None:
                _, init = self._request(
                    "POST",
                    "/api/media/upload/init",
                    json_body={
                        "fileName": file_name,
                        "mimeType": mime,
                        "videoPreparation": video_preparation,
                    },
                    headers=headers,
                    expect=200,
                )
            if not isinstance(init, dict) or not init.get("uploadUrl") or not init.get("key"):
                raise ApiTransportError(
                    "upload_init_failed", f"upload/init returned an unexpected body for {file_name}"
                )
            key = str(init["key"])
            upload_content_type = str(init.get("uploadContentType") or mime)
            storage_backend = str(init.get("storageBackend") or "r2")
            upload_attempt_id = init.get("uploadAttemptId")

            self._put_bytes(str(init["uploadUrl"]), file_path.read_bytes(), upload_content_type)

            prepared_preview: dict[str, Any] | None = None
            if preview_path is not None:
                preview_upload = init.get("previewUpload")
                if (
                    not isinstance(preview_upload, dict)
                    or not preview_upload.get("uploadUrl")
                    or not preview_upload.get("key")
                ):
                    raise ApiTransportError(
                        "upload_init_failed",
                        f"upload/init returned no prepared-preview target for {file_name}",
                    )
                preview_bytes = preview_path.read_bytes()
                self._put_bytes(
                    str(preview_upload["uploadUrl"]),
                    preview_bytes,
                    str(preview_upload.get("uploadContentType") or "video/mp4"),
                )
                prepared_preview = {
                    "key": str(preview_upload["key"]),
                    "byteSize": len(preview_bytes),
                    "profileVersion": _BROWSER_VIDEO_PREPARATION_PROFILE_VERSION,
                }

            # upload/complete merges this into the stored row, so a duration supplied here is what
            # media import reads back. Send it for every type: it is authoritative for audio (never
            # probed server-side) and harmless for video, where the readiness probe overwrites it.
            metadata: dict[str, Any] = {}
            probed_duration = _probe_duration_sec(file_path)
            if probed_duration is not None:
                metadata["duration"] = probed_duration
            complete_body = {
                "key": key,
                "fileName": file_name,
                "mimeType": mime,
                "storageBackend": storage_backend,
                "metadata": metadata,
            }
            if upload_attempt_id:
                complete_body["uploadAttemptId"] = upload_attempt_id
            if prepared_preview is not None:
                complete_body["preparedPreview"] = prepared_preview
            _, complete = self._request(
                "POST", "/api/media/upload/complete", json_body=complete_body, headers=headers, expect=200
            )
        if not isinstance(complete, dict) or not complete.get("mediaFileId"):
            raise ApiTransportError(
                "upload_complete_failed", f"upload/complete returned no mediaFileId for {file_name}"
            )

        media_file_id = str(complete["mediaFileId"])
        remote_expiry = str(complete.get("expiresAt") or complete.get("expires_at") or "").strip()
        if remote_expiry:
            self._uploaded_expiries[media_file_id] = remote_expiry
        if str(mime).startswith("video/") and complete.get("uploadStatus") != "completed":
            self._poll_upload_completed(key, session_id, deadline, job_id)
        return media_file_id

    @staticmethod
    def _safe_analysis_receipt(value: object) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        allowed = {
            "provider",
            "model",
            "api",
            "processing",
            "fileTransport",
            "serviceTier",
            "store",
            "responseId",
            "agenticNavigationStepCount",
            "startedAt",
            "completedAt",
            "evidenceCount",
            "expiresAt",
        }
        receipt = {
            str(key): child
            for key, child in value.items()
            if str(key) in allowed
            and (isinstance(child, (str, int, float, bool)) or (key == "responseId" and child is None))
        }
        return receipt or None

    def _media_status_map(self, session_id: str, media_file_ids: list[str]) -> dict[str, dict]:
        wanted = sorted({str(value) for value in media_file_ids if value})
        if not wanted:
            return {}
        query = "?" + urlencode({"ids": ",".join(wanted)})
        headers = {"x-session-id": session_id, "x-project-id": session_id}
        _, body = self._request("GET", "/api/media/operations/status" + query, headers=headers, expect=200)
        statuses = (body or {}).get("statuses") or [] if isinstance(body, dict) else []
        return {str(item.get("mediaFileId")): item for item in statuses if isinstance(item, dict)}

    def _available_remote_media(self, session_id: str, media_file_ids: list[str]) -> set[str]:
        statuses = self._media_status_map(session_id, media_file_ids)
        available: set[str] = set()
        for media_file_id in media_file_ids:
            status = statuses.get(str(media_file_id))
            if not status:
                continue
            if status.get("expired") or str(status.get("readinessPhase") or "").lower() == "expired":
                continue
            if status.get("exists") is False or status.get("terminalState") in {"deleted", "expired"}:
                continue
            available.add(str(media_file_id))
        return available

    def _poll_upload_completed(self, key: str, session_id: str, deadline: float, job_id: str = "") -> None:
        # The status endpoint returns 200 + uploadStatus 'completed' when finalized, 202 +
        # 'processing' while in flight, and 409 + 'failed' on error — so do not force expect=200.
        headers = {"x-session-id": session_id, "x-project-id": session_id}
        query = "?" + urlencode({"key": key})
        while time.monotonic() < deadline:
            self._raise_if_cancelled(job_id)
            status_code, body = self._request("GET", "/api/media/upload/status" + query, headers=headers)
            upload_status = str((body or {}).get("uploadStatus") or "") if isinstance(body, dict) else ""
            if upload_status == "completed":
                return
            if upload_status == "failed" or status_code == 409:
                raise ApiTransportError(
                    "upload_processing_failed",
                    str((body or {}).get("error") or f"Media processing failed for key {key}"),
                )
            time.sleep(self._poll_interval())
        raise ApiTransportError("upload_processing_timeout", f"Media processing did not complete for key {key}")

    def _await_media_ready(self, session_id: str, media_file_ids: list[str], deadline: float, job_id: str = "") -> None:
        """Wait until every uploaded media file is fully processed before starting the agent run.

        GET /api/media/operations/status?ids= reports per-file readiness: aiReady/analysisReady (the
        agent's session catalog is filtered to analysis-ready media) and exportReady/renderStatus (the
        render-source derivative the export needs). Both derivatives are async; running before they
        finish yields an empty edit or an "render-source is missing" export failure. Bounded by a
        media-ready timeout and the job deadline; a hard processing failure or timeout is surfaced
        (a clear error beats a blank video)."""
        wanted = {str(m) for m in media_file_ids if m}
        if not wanted:
            return
        ready_deadline = min(deadline, time.monotonic() + self._media_ready_timeout())
        last_phase = ""
        while time.monotonic() < ready_deadline:
            self._raise_if_cancelled(job_id)
            by_id = self._media_status_map(session_id, sorted(wanted))
            ready: set[str] = set()
            for mid in wanted:
                s = by_id.get(mid) or {}
                # A failed analysis or render derivative won't self-heal (same bytes re-fail), and the
                # server leaves terminalState 'active' on a failed analyze chunk — so surface the real
                # error fast instead of spinning until the media-ready timeout.
                if (
                    s.get("terminalState") == "failed"
                    or s.get("renderStatus") == "failed"
                    or s.get("analyzeStatus") == "analyze_failed"
                ):
                    raise ApiTransportError(
                        "media_processing_failed",
                        str(s.get("errorMessage") or f"Media {mid} failed processing"),
                        details={
                            "media_file_id": mid,
                            "analyze_status": s.get("analyzeStatus"),
                            "render_status": s.get("renderStatus"),
                        },
                    )
                if s.get("fullyReady") or (self._ai_ready(s) and self._export_ready(s)):
                    ready.add(mid)
                    receipt = self._safe_analysis_receipt(s.get("analysisReceipt"))
                    if receipt and receipt not in self._analysis_receipts:
                        self._analysis_receipts.append(receipt)
                else:
                    last_phase = str(s.get("readinessPhase") or last_phase)
            if wanted <= ready:
                return
            self._tick(job_id, "Processing uploaded media (" + (last_phase or "analyzing") + ")")
            time.sleep(self._poll_interval())
        raise ApiTransportError(
            "media_analysis_timeout",
            f"Uploaded media did not finish processing in time (last phase: {last_phase or 'unknown'}); "
            "the agent cannot edit and the render cannot export media that is not ready.",
            details={"session_id": session_id, "readiness_phase": last_phase},
        )

    @staticmethod
    def _ai_ready(status: dict) -> bool:
        return bool(status.get("aiReady") or status.get("analysisReady"))

    @staticmethod
    def _export_ready(status: dict) -> bool:
        return bool(status.get("exportReady") or status.get("renderStatus") == "completed")

    # ── upload (with incremental dedupe) ──
    @staticmethod
    def _display_names(job: dict) -> dict[str, str]:
        """Map each stored asset path to the name the user knows the file by.

        The session manifest is the only place the original name survives ingestion, so uploads
        must read it from there rather than from the content-addressed path on disk.
        """
        session_hash = str(job.get("session_hash") or "").strip()
        if not session_hash:
            return {}
        try:
            manifest = load_manifest(session_hash)
        except (OSError, ValueError):  # a damaged manifest must not fail the upload
            return {}
        names: dict[str, str] = {}
        for asset in manifest.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            saved = str(asset.get("saved_path") or "")
            # .name defends against a manifest carrying a path-like original name.
            original = Path(str(asset.get("original_name") or "").strip()).name
            if saved and original:
                names[saved] = original
        return names

    def _upload_assets(
        self,
        asset_paths: list[str],
        session_id: str,
        deadline: float,
        job_id: str = "",
        display_names: dict[str, str] | None = None,
    ) -> list[str]:
        """Upload each asset once. Skips assets already uploaded in this session (a reused gateway
        session that edits then refines would otherwise accumulate duplicate media in the project)
        via a per-session uploaded-asset cache."""
        cache = self._load_upload_cache(session_id)
        session_expiry = manifest.session_expires_at(session_id=session_id) or manifest.retention_deadline()
        batch: dict[str, str] = {}
        ids: list[str] = []
        changed = False
        candidates = {
            str(entry.get("media_file_id"))
            for fp, entry in cache.items()
            if fp and isinstance(entry, dict) and entry.get("media_file_id")
        }
        available = self._available_remote_media(session_id, sorted(candidates)) if candidates else set()
        for path in asset_paths:
            fp = self._asset_fingerprint(path)
            if fp and fp in batch:
                ids.append(batch[fp])
                continue
            cached = cache.get(fp) if fp else None
            cached_id = str(cached.get("media_file_id") or "") if isinstance(cached, dict) else ""
            if cached_id and cached_id in available and not manifest.is_expired(cached.get("expires_at")):
                batch[fp] = cached_id
                ids.append(cached_id)
                manifest.mark_managed_asset_uploaded(
                    session_id,
                    path,
                    cached_id,
                    fingerprint=fp,
                    remote_expires_at=str(cached.get("expires_at") or "") or None,
                )
                continue
            if fp and fp in cache:
                cache.pop(fp, None)
                changed = True
            media_id = self._upload_asset(path, session_id, deadline, job_id, (display_names or {}).get(path, ""))
            if self._job is not None:
                self._require_job_active(self._job)
            ids.append(media_id)
            if fp:
                batch[fp] = media_id
                expires_at = manifest.earliest_expiry(session_expiry, self._uploaded_expiries.get(media_id))
                cache[fp] = {"media_file_id": media_id, "expires_at": expires_at}
                selected = manifest.mark_managed_asset_uploaded(
                    session_id,
                    path,
                    media_id,
                    fingerprint=fp,
                    remote_expires_at=expires_at,
                )
                if selected:
                    session_expiry = selected
                    if self._job is not None:
                        self._job["expires_at"] = selected
                changed = True
        if changed:
            self._save_upload_cache(session_id, cache, expires_at=session_expiry)
        return ids

    @staticmethod
    def _asset_fingerprint(path: str) -> str:
        # Gateway media filenames are content-digest based (manifest.py), so name+size is a stable key.
        try:
            p = Path(path)
            return p.name + ":" + str(p.stat().st_size)
        except OSError:
            return ""

    @staticmethod
    def _upload_cache_path(session_id: str) -> Path:
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(session_id))[:96] or "default"
        base = Path(os.getenv("CASSETTE_ASSET_ROOT", str(get_asset_root()))) / "api_uploads"
        base.mkdir(parents=True, exist_ok=True)
        return base / (safe + ".json")

    def _load_upload_cache(self, session_id: str) -> dict[str, dict[str, Any]]:
        try:
            path = self._upload_cache_path(session_id)
            payload = json.loads(path.read_text("utf-8")) or {}
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict) or payload.get("version") != 2:
            return {}
        if manifest.is_expired(payload.get("expires_at")):
            try:
                path.unlink()
            except OSError:
                pass
            return {}
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return {}
        return {
            str(fp): entry
            for fp, entry in entries.items()
            if isinstance(entry, dict)
            and entry.get("media_file_id")
            and not manifest.is_expired(entry.get("expires_at"))
        }

    def _save_upload_cache(
        self, session_id: str, cache: dict[str, dict[str, Any]], *, expires_at: str | None = None
    ) -> None:
        try:
            self._upload_cache_path(session_id).write_text(
                json.dumps({"version": 2, "expires_at": expires_at, "entries": cache}, sort_keys=True),
                "utf-8",
            )
        except OSError:
            pass

    def _latest_agent_summary(self, thread_id: str) -> str:
        """Latest assistant message text from the thread state — a real edit summary for the
        terminal report/notification."""
        try:
            _, state = self._request("GET", f"/api/langgraph/threads/{thread_id}/state", expect=200)
            values = (state or {}).get("values") or {}
            # Read the backend's own verdict before the message loop, not inside it:
            # a turn whose last assistant message is blank would otherwise discard a
            # successfully-read refusal and fall back to reporting completion.
            self._last_terminal_outcome = (values.get("terminalDecision") or {}).get("outcome")
            messages = values.get("messages") or []
            for message in reversed(messages):
                if not isinstance(message, dict):
                    continue
                if (message.get("type") or message.get("role")) not in ("ai", "assistant"):
                    continue
                content = message.get("content")
                if isinstance(content, list):
                    content = " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
                content = str(content or "").strip()
                if content:
                    return content[:700]
        except Exception:  # noqa: BLE001
            pass
        return ""

    # Skip the black scan on long exports — a full decode pass is cheap on a 30s cut and
    # not on a feature. Container facts are still reported.
    _QC_BLACK_SCAN_MAX_SEC = 600.0

    def _export_qc(self, outputs: list[dict]) -> dict | None:
        """Measure the finished export once, here, so the caller never shells out for it.

        The orchestration guard tells callers not to reach for ffprobe/ffmpeg themselves, but
        the envelope carried no measurements — so every reviewer improvised its own probe:
        several tool round trips and a permission prompt each, for facts the runtime can read
        in one pass. Container facts only (duration, fps, resolution, audio span, black
        stretches); creative judgement stays with Cassette.
        """
        if _env("CASSETTE_API_EXPORT_QC").lower() in {"0", "false", "no", "off"}:
            return None
        path = next((o.get("local_path") for o in (outputs or []) if isinstance(o, dict) and o.get("local_path")), None)
        if not path:
            return None
        file_path = Path(str(path))
        if not file_path.exists():
            return None
        try:
            qc: dict[str, Any] = {"file": str(file_path), "size_bytes": file_path.stat().st_size}
            duration = self._qc_container_facts(file_path, qc)
            self._qc_black_segments(file_path, duration, qc)
            return qc
        except Exception:  # noqa: BLE001 — QC is advisory; never fail an export over it
            return None

    def _qc_container_facts(self, file_path: Path, qc: dict[str, Any]) -> float | None:
        """Fill duration/video/audio facts from one ffprobe call; return the container duration."""
        binary = str(_env("CASSETTE_FFPROBE_BIN") or os.getenv("CASSETTE_FFPROBE_BIN", "") or "ffprobe")
        try:
            proc = subprocess.run(
                [
                    binary,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-show_entries",
                    "stream=codec_type,codec_name,width,height,r_frame_rate,duration",
                    "-of",
                    "json",
                    str(file_path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            qc["probe"] = "unavailable"
            return None
        if proc.returncode != 0:
            qc["probe"] = "unavailable"
            return None
        payload = json.loads(proc.stdout or "{}")
        duration: float | None = None
        try:
            duration = round(float((payload.get("format") or {}).get("duration")), 3)
        except (AttributeError, TypeError, ValueError):
            duration = None
        if duration is not None:
            qc["duration_sec"] = duration
        for stream in payload.get("streams") or []:
            if not isinstance(stream, dict):
                continue
            kind = stream.get("codec_type")
            if kind not in {"video", "audio"} or kind in qc:
                continue
            entry: dict[str, Any] = {"codec": stream.get("codec_name")}
            if kind == "video":
                entry["width"] = stream.get("width")
                entry["height"] = stream.get("height")
                rate = str(stream.get("r_frame_rate") or "")
                if "/" in rate:
                    num, _, den = rate.partition("/")
                    try:
                        entry["fps"] = round(float(num) / float(den), 3) if float(den) else None
                    except (TypeError, ValueError, ZeroDivisionError):
                        entry["fps"] = None
            try:
                entry["duration_sec"] = round(float(stream.get("duration")), 3)
            except (TypeError, ValueError):
                pass
            qc[kind] = entry
        return duration

    def _qc_black_segments(self, file_path: Path, duration: float | None, qc: dict[str, Any]) -> None:
        """Scan for black stretches and audio level — the defects a duration-only check cannot see.

        Both filters ride the same decode. Level is here rather than in its own pass because the
        file is already being read: a reviewer asking "is the audio real" otherwise pays a second
        full decode, plus a permission prompt, for a number this pass had in hand.
        """
        if duration is not None and duration > self._QC_BLACK_SCAN_MAX_SEC:
            qc["black_scan"] = "skipped_long_export"
            return
        ffmpeg = str(_env("CASSETTE_FFMPEG_BIN") or os.getenv("CASSETTE_FFMPEG_BIN", "") or "ffmpeg")
        try:
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "info",
                    "-i",
                    str(file_path),
                    "-vf",
                    "blackdetect=d=0.25:pix_th=0.10",
                    "-af",
                    "volumedetect",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError):
            qc["black_scan"] = "unavailable"
            return
        # An aborted pass emits no blackdetect lines, which parses as "no black found" —
        # indistinguishable from a clean export, on exactly the defect this scan exists to
        # catch. Dropping -an made that reachable: the audio path can now fail on its own.
        if proc.returncode != 0:
            qc["black_scan"] = "unavailable"
            return
        segments = _parse_black_segments(proc.stderr or "")
        qc["black_scan"] = "complete"
        qc["black_segments"] = segments
        qc["black_total_sec"] = round(sum(s["duration_sec"] for s in segments), 3)
        levels = _parse_volume_levels(proc.stderr or "")
        if levels:
            qc["audio_levels"] = levels

    def _export_thumbnail(self, outputs: list[dict]) -> str | None:
        """Best-effort still frame from the exported mp4 — there is no live UI to screenshot, so
        this gives final_screenshot consumers (web demo, terminal image) a real visual artifact."""
        if _env("CASSETTE_API_EXPORT_THUMBNAIL").lower() in {"0", "false", "no", "off"}:
            return None
        try:
            path = next(
                (o.get("local_path") for o in (outputs or []) if isinstance(o, dict) and o.get("local_path")), None
            )
            if not path or not Path(path).exists():
                return None
            import subprocess

            ffmpeg = _env("CASSETTE_FFMPEG_BIN") or "ffmpeg"
            target = Path(path).with_suffix(".thumb.jpg")
            subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-ss", "0.5", "-i", path, "-frames:v", "1", str(target)],
                capture_output=True,
                timeout=30,
            )
            return str(target) if target.exists() and target.stat().st_size > 0 else None
        except Exception:  # noqa: BLE001
            return None

    def _put_bytes(self, url: str, data: bytes, mime: str) -> None:
        request = Request(url, data=data, method="PUT", headers={"Content-Type": mime})
        try:
            with urlopen(request, timeout=max(60.0, self._http_timeout_for_upload(len(data)))) as response:
                if response.status not in (200, 201, 204):
                    raise ApiTransportError("upload_put_failed", f"Presigned PUT failed (HTTP {response.status})")
        except HTTPError as exc:
            raise ApiTransportError("upload_put_failed", f"Presigned PUT failed (HTTP {exc.code})") from exc
        except URLError as exc:
            raise ApiTransportError("upload_put_failed", f"Presigned PUT failed: {exc.reason}") from exc

    def _timeline_review_context(self, session_id: str, questions: list[dict] | None = None) -> dict:
        """Best-effort CTL + contact sheet for review moments — never fails the run.

        At a plan review, ``questions`` carries the decoded storyboard beat cells; they
        are rendered into a per-plan storyboard sheet (planned source frames, zero
        render) alongside the current-timeline contact sheet."""
        context: dict[str, Any] = {}
        try:
            from . import timeline as timeline_mod
            from . import tools as tools_mod

            document = self.get_project_document(session_id)
            context["timeline_ctl"] = timeline_mod.render_ctl(document)
            sheet = tools_mod.build_contact_sheet(document, session_id)
            if sheet:
                context["contact_sheet"] = sheet
                # Clickable in most terminals (cmd+click) — real pixels one gesture away.
                context["contact_sheet_uri"] = Path(sheet).as_uri()
        except Exception:  # noqa: BLE001
            pass
        try:
            from . import tools as tools_mod

            frames = next(
                (q["storyboard"] for q in (questions or []) if isinstance(q, dict) and q.get("storyboard")),
                None,
            )
            if frames:
                context["storyboard"] = frames
                storyboard_sheet = tools_mod.build_storyboard_sheet(session_id, frames)
                if storyboard_sheet:
                    context["storyboard_sheet"] = storyboard_sheet
                    context["storyboard_sheet_uri"] = Path(storyboard_sheet).as_uri()
        except Exception:  # noqa: BLE001
            pass
        return context

    # ── project document read ─────────────────────────────────────────────────
    def get_project_document(self, session_id: str) -> dict:
        """Fetch the live ProjectDocument for a session's project (agent-tier read)."""
        from urllib.parse import quote

        self._authenticate()
        _, body = self._request("GET", f"/api/projects/{quote(str(session_id), safe='')}", expect=200)
        document = body.get("document") if isinstance(body, dict) else None
        if not isinstance(document, dict):
            raise ApiTransportError("project_document_missing", "Cassette returned no project document")
        return document

    def post_project_command(self, session_id: str, envelope: dict) -> dict:
        """POST a no-LLM project command (the manual-editor lane); returns the ProjectCommitEvent.

        The route replies 200 for both outcomes: a commit event on success, or
        {ok:false, code, message} on validation/commit failure."""
        from urllib.parse import quote

        self._authenticate()
        _, body = self._request(
            "POST",
            f"/api/projects/{quote(str(session_id), safe='')}/commands",
            json_body=envelope,
            expect=200,
        )
        if not isinstance(body, dict):
            raise ApiTransportError("command_failed", "Cassette returned a malformed command response")
        if body.get("ok") is False:
            code = str(body.get("code") or "command_failed").lower()
            raise ApiTransportError(code, str(body.get("message") or "Project command failed"))
        return body

    # ── agent run ─────────────────────────────────────────────────────────────
    def _thread_metadata(self, session_id: str, thread_id: str, job: dict) -> dict:
        # Full cassette shape, mirroring the editor's buildThreadMetadata: the /try tab's resume
        # path (a plan review answered in the Cassette web UI) hard-requires isCassetteThreadMetadata
        # to pass, so every field below is load-bearing — a partial dict breaks tab-side resume.
        return {
            "schemaVersion": 1,
            "threadKind": "cassette-chat",
            "chatSessionId": thread_id,
            "projectId": session_id,
            "mediaSessionId": session_id,
            "mode": "auto",
            "turnStrategy": "default",
            "turnKind": "conversation",
            "reinitMode": None,
            "modelId": self._resolve_model_id(job),
            "thinkingConfig": self._resolve_thinking_config(job),
            "locale": job.get("cassette_language") or None,
        }

    def _ensure_thread(self, session_id: str, job: dict) -> str:
        # One thread per SESSION, reused across jobs: each job is one conversational turn on the
        # same LangGraph thread, so the agent keeps memory and the deep link stays stable. The
        # upstream LangGraph server 422s non-UUID thread ids, so the id is minted client-side;
        # if_exists:'do_nothing' makes the ensure idempotent (and heals a server-side deletion —
        # the checkpointed conversation is lost then, but the session keeps working).
        from . import manifest as manifest_mod

        session_hash = manifest_mod.resolve_session_hash(session_id=session_id)
        saved_thread = str(manifest_mod.load_session_thread(session_hash).get("thread_id") or "")
        thread_id = saved_thread or str(uuid.uuid4())
        metadata = self._thread_metadata(session_id, thread_id, job)
        _, body = self._request(
            "POST",
            "/api/langgraph/threads",
            json_body={"thread_id": thread_id, "metadata": metadata, "if_exists": "do_nothing"},
            expect=200,
        )
        created = (body.get("thread_id") or body.get("threadId")) if isinstance(body, dict) else None
        if created:
            thread_id = str(created)
        # if_exists:'do_nothing' never updates an existing thread, but the tab rebuilds its resume
        # session context from this metadata — keep modelId/thinkingConfig fresh per turn.
        if saved_thread:
            try:
                self._request(
                    "PATCH",
                    f"/api/langgraph/threads/{thread_id}",
                    json_body={"metadata": self._thread_metadata(session_id, thread_id, job)},
                )
            except Exception:  # noqa: BLE001 — the run's own sessionContext stays authoritative
                pass
        manifest_mod.save_session_thread(session_hash, thread_id)
        job["chat_thread_id"] = thread_id
        job_id = str(job.get("job_id") or "")
        if job_id:
            try:
                from . import jobs

                jobs.update_job(job_id, chat_thread_id=thread_id)
            except Exception:  # noqa: BLE001 — persisting the thread must not fail the run
                pass
        return thread_id

    def _session_context(self, session_id: str, job: dict, prompt: str, thread_id: str | None = None) -> dict:
        # Mirrors CassetteAgentSessionContext (buildCurrentSessionContext in the editor):
        # projectId/mediaSessionId collapse onto the project session id, while chatSessionId/
        # threadId carry the UUID thread the editor's ChatPanel also uses as its stream thread.
        chat_id = thread_id or str(job.get("chat_thread_id") or session_id)
        return {
            "chatSessionId": chat_id,
            "threadId": chat_id,
            "mediaSessionId": session_id,
            "projectId": session_id,
            "mode": "auto",
            "turnStrategy": "default",
            "turnKind": "conversation",
            "reinitMode": None,
            "editorSnapshot": None,
            "mentionedTimelineEntities": None,
            "queryImageIds": None,
            "modelId": self._resolve_model_id(job),
            "thinkingConfig": self._resolve_thinking_config(job),
            "locale": job.get("cassette_language") or None,
            "currentUserRequest": prompt,
            "stoppedTurn": None,
        }

    @staticmethod
    def _project_context() -> dict:
        # A headless run starts from an empty project (no prior editor state); the graph builds it.
        return {
            "cassetteContext": "",
            "revision": 0,
            "sourceKind": None,
            "updatedAt": None,
            "status": "missing",
        }

    def _run_context(self, session_context: dict, turn_id: str) -> dict:
        # Mirrors buildConfigurable().runContext — the connectionState the graph uses to load the
        # session media catalog and commit edits to the project keyed by projectId.
        return {
            "connectionState": {
                "threadId": session_context["threadId"],
                "sessionId": session_context["mediaSessionId"],
                "chatSessionId": session_context["chatSessionId"],
                "mediaSessionId": session_context["mediaSessionId"],
                "projectId": session_context["projectId"],
                "activeMode": session_context["mode"],
                "queryImageIds": None,
                "locale": session_context["locale"],
                "modelId": session_context["modelId"],
                "thinkingConfig": session_context["thinkingConfig"],
                "currentTurnId": turn_id,
            },
            "executionBudget": None,
            "contextCompactionPolicy": None,
            "agentGateRequirements": None,
        }

    @staticmethod
    def _resolve_model_id(job: dict) -> str:
        # Honor the user's model choice. model_selection stores a UI label under 'model'; an explicit
        # id ('model_id'/'modelId') wins if present, otherwise map the label -> id so the run uses the
        # SAME model the web editor would select. Fall back to the env override or the editor default
        # only when nothing maps.
        ms = job.get("model_selection") or {}
        explicit = str(ms.get("model_id") or ms.get("modelId") or "").strip()
        if explicit in _SUPPORTED_AGENT_MODEL_IDS:
            return explicit
        label = str(ms.get("model") or "").strip()
        if label in _SUPPORTED_AGENT_MODEL_IDS:  # already an id
            return label
        mapped = _model_id_from_label(label) if label else None
        if mapped in _SUPPORTED_AGENT_MODEL_IDS:
            return mapped
        env_model = _env("CASSETTE_API_MODEL_ID")
        if env_model in _SUPPORTED_AGENT_MODEL_IDS:
            return env_model
        # A model was explicitly chosen but could not be mapped: fail loudly when selection is
        # required, rather than silently running the default.
        if label and _require_model_selection():
            raise ApiTransportError(
                "model_selection_failed",
                f"Could not map the selected Cassette model '{label}' to a supported model id; "
                "set CASSETTE_API_MODEL_ID or disable CASSETTE_REQUIRE_MODEL_SELECTION.",
            )
        return DEFAULT_AGENT_MODEL_ID

    @staticmethod
    def _resolve_thinking_config(job: dict) -> str:
        # Mirror the GPT reasoning presets exposed by cassette-config. Honor an env override or the
        # job's thinking selection (case-insensitive); default 'xhigh' is the quality-first preset.
        valid = set(AGENT_THINKING_LEVELS)
        override = _env("CASSETTE_API_THINKING").lower()
        if override in valid:
            return override
        ms = job.get("model_selection") or {}
        raw = str(ms.get("thinkingConfig") or ms.get("thinking_level") or "").strip().lower()
        if raw in valid:
            return raw
        # Honor CASSETTE_DEFAULT_THINKING_LEVEL before falling back to the hard-coded default.
        env_default = _env("CASSETTE_DEFAULT_THINKING_LEVEL").lower()
        return env_default if env_default in valid else _DEFAULT_THINKING

    def _run_agent(
        self, thread_id: str, session_id: str, prompt: str, job: dict, deadline: float
    ) -> tuple[str, list[dict]]:
        """Start the run, satisfy interrupts headlessly, return (terminal_status, questions).

        Uploaded media is NOT passed as ids — the cassette-chat graph reads the session-scoped media
        catalog keyed by sessionContext.mediaSessionId (== the upload x-session-id)."""
        job_id = str(job.get("job_id") or "")
        turn_id = f"{job_id or session_id}-turn"
        session_context = self._session_context(session_id, job, prompt, thread_id=thread_id)
        config = {
            # recursion_limit is what the upstream LangGraph server reads; the editor also sends the
            # camelCase duplicate, harmless to include for parity.
            "recursion_limit": self._recursion_limit(),
            "recursionLimit": self._recursion_limit(),
            "configurable": {
                "sessionContext": session_context,
                "projectContext": self._project_context(),
                "runContext": self._run_context(session_context, turn_id),
            },
        }
        run_body = {
            "assistant_id": "cassette-chat",
            "input": {"messages": [{"type": "human", "content": prompt}]},
            "config": config,
            # 'reject' (not the editor's 'rollback'): a plugin turn must never cancel a run the
            # user started from the open editor tab — the busy turn fails typed instead.
            "multitask_strategy": "reject",
            "stream_mode": _RUN_STREAM_MODES,
        }
        run_id = self._post_run(thread_id, run_body)
        self._persist_continuation(
            job_id,
            thread_id,
            session_id,
            config,
            run_id,
            interrupts=[],
        )
        return self._drive_run(thread_id, run_id, config, job, deadline)

    def _drive_run(
        self,
        thread_id: str,
        run_id: str,
        config: dict[str, Any],
        job: dict,
        deadline: float,
    ) -> tuple[str, list[dict]]:
        """Drive a new or resumed run until success, timeout, or genuine user input."""
        questions: list[dict] = []
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        try:
            return self._drive_run_inner(thread_id, run_id, config, job, deadline, questions, job_id, session_id)
        finally:
            if (stop := getattr(self, "_active_stream_stop", None)) is not None:
                stop.set()
                self._active_stream_stop = None

    def _drive_run_inner(
        self,
        thread_id: str,
        run_id: str,
        config: dict[str, Any],
        job: dict,
        deadline: float,
        questions: list[dict],
        job_id: str,
        session_id: str,
    ) -> tuple[str, list[dict]]:
        # Interrupts we have already answered. If the same ones come back still pending, our resume
        # did not advance the graph, and resuming again would spin until the job deadline.
        resumed_interrupts: set[str] = set()
        while True:
            self._active_stream_stop = self._refresh_stream_listener(
                thread_id, run_id, job, getattr(self, "_active_stream_stop", None)
            )
            status = self._await_run(thread_id, run_id, deadline, job_id)
            if status in {"interrupted", "success"}:
                # A run parked on an interrupt is reported `success` by the LangGraph server — the
                # *run* did finish; the *graph* is what is waiting. So the run status alone cannot
                # tell "done" from "waiting for an answer", and trusting it reports an edit the
                # agent never made. The thread's pending interrupts are the authoritative signal.
                interrupts = self._pending_interrupts(thread_id)
                pending_keys = {self._interrupt_key(item) for item in interrupts}
                if interrupts and pending_keys <= resumed_interrupts:
                    # Answering these already failed to move the graph on. It is still parked, so
                    # this is a job that needs the user — never a finished edit.
                    self._persist_continuation(job_id, thread_id, session_id, config, run_id, interrupts=interrupts)
                    return _NEEDS_USER, questions
                if interrupts:
                    resume_value, new_questions, needs_user = self._resume_value(interrupts)
                    questions.extend(new_questions)
                    if needs_user:
                        # A genuine user question (e.g. ask_user) with no auto-reply configured:
                        # leave the thread interrupted and hand back to the user/review loop.
                        self._persist_continuation(
                            job_id,
                            thread_id,
                            session_id,
                            config,
                            run_id,
                            interrupts=interrupts,
                        )
                        return _NEEDS_USER, questions
                    run_id = self._post_run(
                        thread_id,
                        {
                            "assistant_id": "cassette-chat",
                            "command": {"resume": resume_value},
                            "config": config,
                            "multitask_strategy": "interrupt",
                            "stream_mode": _RUN_STREAM_MODES,
                        },
                    )
                    self._persist_continuation(job_id, thread_id, session_id, config, run_id, interrupts=[])
                    resumed_interrupts |= pending_keys
                    continue
                if status == "success":
                    self._clear_continuation(job_id)
                    return _SUCCEEDED, questions
                # Interrupted with nothing pending == treat as needing user. Carry a summary so
                # the terminal message is not a bare headline.
                summary = self._latest_agent_summary(thread_id) or "Cassette paused and needs input to continue."
                questions.append(
                    {
                        "question": summary[:500],
                        "requires_user": True,
                        "reason": "cassette_agent_question",
                        "answer": "",
                    }
                )
                self._persist_continuation(job_id, thread_id, session_id, config, run_id, interrupts=[])
                return _NEEDS_USER, questions
            if status == "timeout":
                return _TIMED_OUT, questions
            self._clear_continuation(job_id)
            raise ApiTransportError("agent_run_error", f"Agent run failed: {self._run_error_detail(thread_id)}")

    @staticmethod
    def _interrupt_kind(value: Any) -> str:
        """Discriminator for a LangGraph interrupt value.

        The two interrupt families label themselves differently and both are load-bearing:
        headless *tool* interrupts carry ``type: 'tool'``, while typed agent interrupts
        (ask_user / edit_plan_review / mode_switch / init_questions) carry ``kind``. Reading only
        one of the two makes the whole other family invisible, which silently turns a pending
        question into a finished job.
        """
        if not isinstance(value, dict):
            return "unknown"
        return str(value.get("kind") or value.get("type") or "unknown")

    @staticmethod
    def _interrupt_key(item: Any) -> str:
        """Stable identity for one pending interrupt, used to notice one that never clears."""
        if not isinstance(item, dict):
            return "unknown"
        identifier = str(item.get("id") or "").strip()
        if identifier:
            return identifier
        # No id: fall back to the value itself so two different questions stay distinguishable.
        return json.dumps(item.get("value"), sort_keys=True, default=str)[:512]

    @staticmethod
    def _interrupt_metadata(interrupts: list[dict]) -> list[dict]:
        metadata: list[dict] = []
        for item in interrupts:
            value = item.get("value") if isinstance(item, dict) else {}
            if not isinstance(value, dict):
                value = {}
            metadata.append(
                {
                    "id": str(item.get("id") or "") if isinstance(item, dict) else "",
                    "type": ApiTransport._interrupt_kind(value),
                    "tool_call_id": str((value.get("toolCall") or {}).get("id") or "")
                    if isinstance(value.get("toolCall"), dict)
                    else "",
                }
            )
        return metadata

    def _persist_continuation(
        self,
        job_id: str,
        thread_id: str,
        session_id: str,
        config: dict[str, Any],
        run_id: str,
        *,
        interrupts: list[dict],
    ) -> None:
        if not job_id:
            return
        try:
            from . import jobs

            jobs.update_job(
                job_id,
                continuation={
                    "transport": "api",
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "session_id": session_id,
                    "config": config,
                    "interrupts": self._interrupt_metadata(interrupts),
                    "updated_at": self._now_iso(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            try:
                import runtime_config

                restart_safe_required = runtime_config.is_mcp_runtime()
            except Exception:  # noqa: BLE001
                restart_safe_required = False
            if restart_safe_required:
                raise ApiTransportError(
                    "continuation_persist_failed",
                    "Could not persist the private API continuation required for restart-safe resume.",
                    details={"type": type(exc).__name__},
                ) from exc

    @staticmethod
    def _clear_continuation(job_id: str) -> None:
        if not job_id:
            return
        try:
            from . import jobs

            jobs.update_job(job_id, continuation=None, resume_request=None)
        except Exception:  # noqa: BLE001
            pass

    def _run_error_detail(self, thread_id: str) -> str:
        """Best-effort extraction of why a run reached 'error' (thread-state task errors)."""
        try:
            _, state = self._request("GET", f"/api/langgraph/threads/{thread_id}/state", expect=200)
        except Exception:  # noqa: BLE001
            return "unknown error"
        details: list[str] = []
        if isinstance(state, dict):
            for task in state.get("tasks") or []:
                err = task.get("error") if isinstance(task, dict) else None
                if err:
                    details.append(str(err)[:300])
        return "; ".join(details) or "unknown error"

    # ── run event stream (enhancement channel; poll drives completion) ───────
    @staticmethod
    def _iter_sse(resp):
        """Yield (event, data) pairs from a line-iterable SSE response body."""
        event: str | None = None
        data_lines: list[str] = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n") if isinstance(raw, bytes) else str(raw).rstrip("\r\n")
            if line == "":
                if data_lines:
                    yield (event or "message", "\n".join(data_lines))
                event, data_lines = None, []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        if data_lines:
            yield (event or "message", "\n".join(data_lines))

    def _refresh_stream_listener(self, thread_id: str, run_id: str, job: dict, prev: threading.Event | None):
        """(Re)start the run-stream listener when the run id changes; return the live stop event."""
        if not _stream_enabled() or not str(job.get("job_id") or ""):
            return prev
        if prev is not None and getattr(prev, "run_id", None) == run_id:
            return prev
        if prev is not None:
            prev.set()
        stop = threading.Event()
        stop.run_id = run_id  # type: ignore[attr-defined]
        threading.Thread(
            target=self._consume_run_stream,
            args=(thread_id, run_id, dict(job), stop),
            daemon=True,
            name=f"cassette-run-stream-{run_id[:8]}",
        ).start()
        return stop

    def _consume_run_stream(self, thread_id: str, run_id: str, job: dict, stop: threading.Event) -> None:
        """Join the run's SSE stream and fold events onto the persisted job.

        Best-effort by design: any failure (drop, parse error, timeout) simply ends the listener —
        the poll loop still drives the run, so the only loss is event granularity."""
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        try:
            from . import jobs as jobs_mod
            from . import timeline as timeline_mod

            try:
                baseline = self.get_project_document(session_id)
            except Exception:  # noqa: BLE001 — fresh project: delta baseline is the empty document
                baseline = {"version": 0, "entities": {}, "order": {}}
            url = f"{_api_base()}/api/langgraph/threads/{thread_id}/runs/{run_id}/stream"
            request = Request(url, headers=self._auth_headers({"Accept": "text/event-stream"}))
            with urlopen(request, timeout=_stream_read_timeout()) as resp:
                progress: list[str] = list(job.get("plan_progress") or [])
                for event, data in self._iter_sse(resp):
                    if stop.is_set():
                        return
                    if event != "custom":
                        continue
                    try:
                        payload = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") == "project_operation_committed" and isinstance(
                        payload.get("document"), dict
                    ):
                        delta = timeline_mod.render_delta(baseline, payload["document"])
                        jobs_mod.update_job(job_id, timeline_delta=delta)
                    else:
                        label = str(
                            payload.get("label")
                            or payload.get("status")
                            or payload.get("title")
                            or payload.get("type")
                            or ""
                        ).strip()[:80]
                        if label:
                            progress = (progress + [label])[-10:]
                            jobs_mod.update_job(job_id, plan_progress=progress)
        except Exception:  # noqa: BLE001 — enhancement channel only
            return

    def _post_run(self, thread_id: str, body: dict) -> str:
        status, resp = self._request("POST", f"/api/langgraph/threads/{thread_id}/runs", json_body=body)
        if status == 422 and body.get("multitask_strategy") == "reject":
            raise ApiTransportError(
                "thread_busy",
                "The session's editor thread is already running a turn (likely started from the "
                "open editor tab); wait for it to finish and retry.",
            )
        if status != 200:
            raise ApiTransportError("run_create_failed", f"LangGraph run create failed with HTTP {status}")
        run_id = (resp.get("run_id") or resp.get("runId")) if isinstance(resp, dict) else None
        if not run_id:
            raise ApiTransportError("run_create_failed", "LangGraph run create returned no run_id")
        return str(run_id)

    def _await_run(self, thread_id: str, run_id: str, deadline: float, job_id: str = "") -> str:
        # Fail fast if the run never leaves 'pending' — a healthy LangGraph worker moves a run to
        # 'running' within seconds, so a run stuck 'pending' means the run queue is not being drained
        # (worker down/misconfigured). Without this the job would hang until the full job timeout.
        start = time.monotonic()
        start_timeout = self._run_start_timeout()
        ever_started = False
        while time.monotonic() < deadline:
            if self._cancelled(job_id):
                self._cancel_run(thread_id, run_id)
                raise _JobCancelled()
            _, body = self._request("GET", f"/api/langgraph/threads/{thread_id}/runs/{run_id}", expect=200)
            status = str((body or {}).get("status") or "") if isinstance(body, dict) else ""
            if status in _LG_TERMINAL:
                return status
            if status and status != "pending":
                ever_started = True
            self._tick(job_id, "Cassette agent is editing (" + (status or "running") + ")")
            if not ever_started and (time.monotonic() - start) > start_timeout:
                raise ApiTransportError(
                    "agent_run_not_started",
                    f"Agent run stayed '{status or 'pending'}' for {int(start_timeout)}s without starting — "
                    "the Cassette agent run queue is not draining (backend worker unavailable).",
                    details={"run_id": run_id, "status": status or "pending"},
                )
            time.sleep(self._poll_interval())
        return "timeout"

    def _cancel_run(self, thread_id: str, run_id: str) -> None:
        """Best-effort server-side cancel of an in-flight LangGraph run (so it actually stops,
        not just locally abandoned). Failures are swallowed — the job is already terminating."""
        try:
            self._request(
                "POST", f"/api/langgraph/threads/{thread_id}/runs/{run_id}/cancel?action=interrupt", json_body={}
            )
        except Exception:  # noqa: BLE001
            pass

    def _cancel_export(self, export_job_id: str) -> None:
        """Best-effort server-side cancel of an in-flight export/render job on cancellation."""
        try:
            self._request("POST", f"/api/export/jobs/{export_job_id}/cancel", json_body={})
        except Exception:  # noqa: BLE001
            pass

    def _pending_interrupts(self, thread_id: str) -> list[dict]:
        _, state = self._request("GET", f"/api/langgraph/threads/{thread_id}/state", expect=200)
        out: list[dict] = []
        if not isinstance(state, dict):
            return out
        for task in state.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for interrupt in task.get("interrupts") or []:
                value = interrupt.get("value") if isinstance(interrupt, dict) else None
                if isinstance(value, dict):
                    out.append({"id": interrupt.get("id"), "value": value})
        # Some LangGraph versions surface interrupts on the top-level __interrupt__ channel. Guard
        # against an explicit null (values["__interrupt__"] == None) which .get(..., []) would return.
        for interrupt in (state.get("values") or {}).get("__interrupt__") or []:
            if isinstance(interrupt, dict) and isinstance(interrupt.get("value"), dict):
                out.append({"id": interrupt.get("id"), "value": interrupt["value"]})
        return out

    def _resume_value(self, interrupts: list[dict]) -> tuple[Any, list[dict], bool]:
        """Build the resume payload. Returns (resume_value, questions, needs_user).

        Headless tool interrupts (editor_navigate) resume KEYED by toolCall.id; typed interrupts
        (edit_plan_review/mode_switch/init_questions) resume with a BARE object. A genuine ``ask_user``
        question hands control back to the user (needs_user=True) unless CASSETTE_API_DEFAULT_ASK_USER_REPLY
        is set: only *routine* interactions are auto-handled, and real questions surface as
        needs_user. A typed interrupt is resolved before any batched tool acks so its bare
        payload is never shadowed by the keyed map (LangGraph resumes one interrupt at a time)."""
        questions: list[dict] = []
        keyed: dict[str, Any] = {}
        for item in interrupts:
            value = item["value"]
            kind = self._interrupt_kind(value)
            if kind == "tool":
                tool_call = value.get("toolCall") or {}
                call_id = tool_call.get("id")
                if call_id:
                    # Only editor_navigate is browser-bound; ack any tool interrupt as a no-op so
                    # the headless run never hangs.
                    keyed[str(call_id)] = {"result": dict(_NAVIGATE_NOOP_RESULT)}
                continue
            # Typed interrupt — resume bare. Resolve it first so its payload wins over any keyed acks.
            # Each auto-handled interrupt leaves an audit record (requires_user=False).
            if kind == "edit_plan_review":
                if _unattended() or _plan_review_mode() != "user":
                    questions.append(
                        {
                            "question": "Cassette requested plan approval.",
                            "requires_user": False,
                            "reason": "routine_plan_approval",
                            "answer": "Auto-approved the edit plan.",
                        }
                    )
                    return {"action": "approve"}, questions, False
                # Web-agent-page parity: the plan is a decision, not a formality. Surface it as a
                # real question (approve / revise <feedback> / reject) instead of eating it.
                from . import timeline as timeline_mod

                payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
                block = timeline_mod.plan_review_block(payload)
                question: dict[str, Any] = {
                    "question": block,
                    "requires_user": True,
                    "reason": "edit_plan_review",
                    "answer": "",
                }
                # Typed beat cells decoded from the plan's storyboard links (the digest
                # carries only their labels); _timeline_review_context renders them into
                # a storyboard-sheet image from the locally ingested sources.
                frames = timeline_mod.storyboard_frames(str(payload.get("reviewMarkdown") or ""))
                if frames:
                    question["storyboard"] = frames
                questions.append(question)
                return None, questions, True
            if kind == "mode_switch":
                questions.append(
                    {
                        "question": "Cassette requested a mode switch.",
                        "requires_user": False,
                        "reason": "routine_mode_switch",
                        "answer": "Auto-switched to auto mode.",
                    }
                )
                return {"action": "switch_mode", "selectedMode": "auto"}, questions, False
            if kind == "init_questions":
                questions.append(
                    {
                        "question": "Cassette asked initialization questions.",
                        "requires_user": False,
                        "reason": "routine_init_questions",
                        "answer": "Proceeded with defaults.",
                    }
                )
                return {}, questions, False
            if kind == "ask_user":
                # The question lives in the typed payload (AskUserInterruptPayload); the flat keys
                # are only a fallback. Reading the flat ones alone yields an empty question, which
                # classifies as routine and auto-answers a decision the user never saw.
                ask_payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
                text = str(ask_payload.get("question") or value.get("prompt") or value.get("question") or "")
                # Classify with prompt.classify_cassette_question: a *routine* ambiguity
                # is auto-answered with a safe default and the run continues; only a genuine user
                # choice or a missing-required-asset returns needs_user (carrying the specific reason).
                from . import prompt as _prompt

                classification = _prompt.classify_cassette_question(text)
                reason = classification.get("reason") or "cassette_agent_question"
                default_answer = classification.get("answer") or ""
                auto_reply = _env("CASSETTE_API_DEFAULT_ASK_USER_REPLY")
                if not classification.get("requires_user"):
                    reply = auto_reply or default_answer or "Please proceed using your best judgment."
                    questions.append(
                        {"question": text[:500], "requires_user": False, "reason": reason, "answer": reply}
                    )
                    return {"action": "respond", "userResponse": reply}, questions, False
                if auto_reply:  # operator opted into unattended auto-answering even real questions
                    questions.append(
                        {"question": text[:500], "requires_user": False, "reason": reason, "answer": auto_reply}
                    )
                    return {"action": "respond", "userResponse": auto_reply}, questions, False
                questions.append(
                    {"question": text[:500], "requires_user": True, "reason": reason, "answer": default_answer}
                )
                return None, questions, True
        if keyed:
            return keyed, questions, False
        # Unknown interrupt shape — resume empty rather than hang.
        return {}, questions, False

    # ── export ────────────────────────────────────────────────────────────────
    def _export_project(self, session_id: str, job_id: str, deadline: float | None = None) -> list[dict]:
        if deadline is None:
            deadline = time.monotonic() + 600.0
        from urllib.parse import quote

        if self._job is not None:
            self._require_job_active(self._job)

        _, created = self._request(
            "POST", f"/api/export/projects/{quote(str(session_id), safe='')}/jobs", json_body={}, expect=202
        )
        export_job_id = created.get("jobId") if isinstance(created, dict) else None
        if not export_job_id:
            raise ApiTransportError("export_create_failed", "Export create returned no jobId")
        export_job_id = str(export_job_id)

        file_url: str | None = None
        while time.monotonic() < deadline:
            if self._cancelled(job_id):
                self._cancel_export(export_job_id)  # stop the server-side Lambda render, not just abandon it
                raise _JobCancelled()
            _, body = self._request("GET", f"/api/export/jobs/{export_job_id}", expect=200)
            status = str((body or {}).get("status") or "") if isinstance(body, dict) else ""
            if status == "done":
                file_url = (body or {}).get("fileUrl") or f"/api/export/jobs/{export_job_id}/file"
                break
            if status == "error":
                raise ApiTransportError("export_failed", str((body or {}).get("error") or "Export job failed"))
            pct = (body or {}).get("progressPercent") if isinstance(body, dict) else None
            self._tick(
                job_id,
                "Rendering the export (" + (status or "rendering") + (f" {pct}%" if pct is not None else "") + ")",
            )
            time.sleep(self._poll_interval())
        if not file_url:
            raise ApiTransportError("export_timeout", "Export job did not complete in time")

        if self._job is not None:
            self._require_job_active(self._job)
        target = _exports_dir(job_id) / f"{job_id}.mp4"
        self._download(f"/api/export/jobs/{export_job_id}/file", target)
        return [
            {
                "text": target.name,
                "href": file_url,
                "download": target.name,
                "local_path": str(target),
                "kind": "video",
            }
        ]

    def _download(self, path: str, target: Path, *, _retried: bool = False) -> None:
        request = Request(_api_base() + path, method="GET", headers=self._auth_headers({}))
        try:
            job_id = str((getattr(self, "_job", None) or {}).get("job_id") or "")
            with (
                urlopen(request, timeout=max(120.0, self._http_timeout_for_upload(0))) as response,
                target.open("wb") as fh,
            ):
                received = 0
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
                    # Downloading a finished render is the longest stretch with nothing
                    # else to say. A host that waits on one blocking call judges silence
                    # as an idle call, so the bytes have to speak.
                    self._tick(job_id, f"Downloading the export ({received // (1024 * 1024)} MB)")
        except HTTPError as exc:
            if exc.code == 401 and not _retried:
                self._token = None
                self._authenticate()
                self._download(path, target, _retried=True)
                return
            raise ApiTransportError("export_download_failed", f"Export download failed (HTTP {exc.code})") from exc
        except URLError as exc:
            raise ApiTransportError("export_download_failed", f"Export download failed: {exc.reason}") from exc

    # ── progress telemetry (job-record updates) ──
    def _init_progress(self, job: dict) -> None:
        # Fresh per run_job (get_transport() returns a new instance per call).
        self._job = job
        self._stage_timings: dict[str, dict] = {}
        self._current_stage = ""
        now = time.monotonic()
        self._last_event = 0.0  # force an event on the first stage
        self._last_heartbeat = now  # first heartbeat waits one full interval
        self._run_started = now
        self._last_terminal_outcome = None
        self._analysis_receipts = []
        self._uploaded_expiries = {}

    def _enter_stage(self, job_id: str, stage: str, summary: str) -> None:
        """Mark the start of a phase: finalize the previous stage timing, write current_stage +
        stage_timings + an immediate progress event."""
        iso = self._now_iso()
        if self._current_stage and self._current_stage in self._stage_timings:
            prev = self._stage_timings[self._current_stage]
            prev["status"] = "succeeded"
            prev["finished_at"] = iso
            prev["duration_sec"] = round(time.monotonic() - prev.get("started_mono", time.monotonic()), 1)
        self._current_stage = stage
        entry = self._stage_timings.get(stage) or {"attempts": 0, "started_at": iso, "started_mono": time.monotonic()}
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["status"] = "running"
        entry.setdefault("started_at", iso)
        entry.setdefault("started_mono", time.monotonic())
        entry["duration_sec"] = round(time.monotonic() - entry["started_mono"], 1)
        self._stage_timings[stage] = entry
        self._last_event = 0.0  # always emit an event at a stage boundary
        self._tick(job_id, summary, force_event=True)

    def _finish_progress(self, status: str, summary: str, outputs: list[dict]) -> None:
        """Close the active stage and persist one truthful terminal progress event."""
        now = time.monotonic()
        iso = self._now_iso()
        if self._current_stage in self._stage_timings:
            current = self._stage_timings[self._current_stage]
            current["status"] = status
            current["finished_at"] = iso
            current["duration_sec"] = round(now - current.get("started_mono", now), 1)
        job_id = str((self._job or {}).get("job_id") or "")
        if job_id:
            self._append_event(job_id, summary or f"Cassette job {status}", status, outputs)

    def _tick(
        self, job_id: str, summary: str, status: str = "running", outputs: list | None = None, force_event: bool = False
    ) -> None:
        """Called from phase boundaries and inside the poll loops. Appends a bounded progress_events
        entry on the event interval and sends a TEXT progress heartbeat on the snapshot interval —
        there is no live UI, so the heartbeat carries text rather than a screenshot."""
        if not job_id:
            return
        now = time.monotonic()
        if self._current_stage in self._stage_timings:
            self._stage_timings[self._current_stage]["duration_sec"] = round(
                now - self._stage_timings[self._current_stage].get("started_mono", now), 1
            )
        if force_event or (now - self._last_event) >= self._event_interval():
            self._last_event = now
            self._append_event(job_id, summary, status, outputs)
        if (now - self._last_heartbeat) >= self._heartbeat_interval():
            self._last_heartbeat = now
            self._send_heartbeat(summary)
        self._emit_host_progress(now, summary, force=force_event)

    def _emit_host_progress(self, now: float, summary: str, *, force: bool = False) -> None:
        """Feed the MCP host's progress channel so one blocking call replaces a poll loop.

        A host that waits on a single tool call needs two things: something to show the user,
        and traffic often enough that the call is not judged idle and aborted. Both come from
        here, on their own cadence — the event/heartbeat intervals above are tuned for the job
        record and the gateway, and are far too slow to hold a call open.
        """
        sink = _PROGRESS_SINK
        if sink is None:
            return
        if not force and (now - getattr(self, "_last_host_progress", 0.0)) < _HOST_PROGRESS_INTERVAL_SEC:
            return
        self._last_host_progress = now
        try:
            elapsed = now - getattr(self, "_run_started", now)
            sink(round(elapsed, 1), f"{self._current_stage or 'running'} — {str(summary)[:160]}".strip(" —"))
        except Exception:  # noqa: BLE001 — progress must never break the run
            pass

    def _append_event(self, job_id: str, summary: str, status: str, outputs: list | None) -> None:
        try:
            from . import jobs

            events = list(jobs.load_job(job_id).get("progress_events") or [])[-9:]
            events.append(
                {
                    "at": self._now_iso(),
                    "status": status,
                    "summary": str(summary)[:500],
                    "stage": self._current_stage,
                    "output_link_count": len(outputs or []),
                }
            )
            jobs.update_job(
                job_id,
                progress_events=events,
                current_stage=self._current_stage,
                stage_timings=self._public_stage_timings(),
            )
        except Exception:  # noqa: BLE001 — progress recording must never break the run
            pass

    def _send_heartbeat(self, summary: str) -> None:
        job = getattr(self, "_job", None)
        if not isinstance(job, dict):
            return
        delivery = job.get("delivery") or {}
        if not delivery.get("chat_id"):
            return
        try:
            from . import notifier

            elapsed = int(time.monotonic() - getattr(self, "_run_started", time.monotonic()))
            stage = self._current_stage or "running"
            message = f"Cassette job in progress — {stage} ({elapsed}s elapsed)."
            if summary:
                message += f"\n{str(summary)[:300]}"
            notifier.notify_gateway_text(delivery, message, reason="cassette_progress")
        except Exception:  # noqa: BLE001
            pass

    def _public_stage_timings(self) -> dict:
        return {k: {kk: vv for kk, vv in v.items() if kk != "started_mono"} for k, v in self._stage_timings.items()}

    def _notify_model_selection(self, job: dict, model_id: str, thinking: str) -> None:
        # Deliver the 'Cassette model selected' gateway notice and persist it.
        job_id = str(job.get("job_id") or "")
        selection = job.get("model_selection") or {}
        if not job_id or (selection.get("source") == "session_preference"):
            return
        try:
            from . import jobs, notifier

            enriched = dict(job)
            enriched["model_selection"] = {**selection, "resolved_model_id": model_id, "resolved_thinking": thinking}
            result = notifier.notify_model_selection(enriched)
            if result:
                jobs.update_job(job_id, model_selection_notification=result)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _now_iso() -> str:
        from . import jobs

        return jobs.now_iso()

    @staticmethod
    def _event_interval() -> float:
        return _env_num("CASSETTE_PROGRESS_INTERVAL_SEC", 30.0, 5.0, getter=os.getenv)

    @staticmethod
    def _heartbeat_interval() -> float:
        return _env_num("CASSETTE_PROGRESS_SNAPSHOT_SEC", 180.0, 30.0, getter=os.getenv)

    # ── http + result helpers ──────────────────────────────────────────────────
    def _auth_headers(self, headers: dict[str, str]) -> dict[str, str]:
        merged = {"User-Agent": _API_USER_AGENT, **headers}
        if self._token:
            merged["Authorization"] = f"Bearer {self._token}"
        return merged

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        authed: bool = True,
        expect: int | None = None,
        timeout: float | None = None,
        _retried: bool = False,
    ) -> tuple[int, Any]:
        url = _api_base() + path
        data = None
        req_headers = {"User-Agent": _API_USER_AGENT, **dict(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        if authed:
            req_headers = self._auth_headers(req_headers)
        request = Request(url, data=data, method=method, headers=req_headers)
        try:
            with urlopen(request, timeout=timeout or _http_timeout()) as response:
                status = response.status
                raw = response.read()
        except HTTPError as exc:
            status = exc.code
            raw = exc.read()
            # Re-verify once on auth expiry, then retry.
            if status == 401 and authed and not _retried:
                self._token = None
                self._authenticate()
                return self._request(
                    method,
                    path,
                    json_body=json_body,
                    headers=headers,
                    authed=authed,
                    expect=expect,
                    timeout=timeout,
                    _retried=True,
                )
        except URLError as exc:
            raise ApiTransportError("network_error", f"{method} {path} failed: {exc.reason}") from exc

        body: Any
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            body = None
        if status == 403 and authed:
            detail = body.get("error") if isinstance(body, dict) else None
            raise ApiTransportError(
                "forbidden",
                f"{method} {path} -> 403{f': {detail}' if detail else ''}. The Cassette server refused this "
                "request for this account; every endpoint the plugin calls is an agent operation, so report "
                "it to the Cassette team rather than changing anything here.",
                details={"status": 403, "path": path},
            )
        if expect is not None and status != expect:
            detail = body.get("error") if isinstance(body, dict) else None
            raise ApiTransportError(
                "http_error",
                f"{method} {path} -> HTTP {status}{f': {detail}' if detail else ''}",
                details={"status": status, "path": path},
            )
        return status, body

    def _result(
        self,
        status: str,
        *,
        outputs: list[dict] | None = None,
        questions: list[dict] | None = None,
        errors: list[dict] | None = None,
        completion_observed: bool = False,
        export_completed: bool = False,
        export_pending: bool = False,
        risk: str = "medium",
        extra_quality: dict[str, Any] | None = None,
        final_screenshot: Any | None = None,
    ) -> dict:
        outputs = outputs or []
        errors = list(errors or [])
        agent_not_done = getattr(self, "_last_terminal_outcome", None) == "not_done"
        completion_review_pending = (
            status == _NEEDS_USER
            and bool((extra_quality or {}).get("completion_review_required"))
            and _export_on_complete(getattr(self, "_job", None) or {})
        )
        # LangGraph success only means the graph settled. The agent's terminal decision is the
        # product outcome for ordinary edit turns. An explicit export turn is different: rendering
        # is intentionally owned by this transport, not the Cassette agent. Keep not_done as review
        # evidence there, then let the typed completion-review decision choose export/continue/fail.
        if agent_not_done and not completion_review_pending:
            status = _FAILED
            completion_observed = False
            export_completed = False
            export_pending = False
            risk = "high"
            if not any(error.get("code") == "agent_reported_not_done" for error in errors):
                errors.append(
                    {
                        "code": "agent_reported_not_done",
                        "message": "Cassette settled the turn but reported that the requested edit was not completed.",
                        "details": {"terminal_outcome": "not_done"},
                    }
                )
            extra_quality = {**(extra_quality or {}), "agent_terminal_outcome": "not_done"}
        elif agent_not_done:
            extra_quality = {**(extra_quality or {}), "agent_terminal_outcome": "not_done"}
        quality = {
            "transport": "api",
            "completion_observed": completion_observed,
            "export_completed": export_completed,
            "export_pending": export_pending,
            "output_link_count": len(outputs),
            "local_output_count": sum(1 for o in outputs if isinstance(o, dict) and o.get("local_path")),
            "risk": risk,
        }
        if self._analysis_receipts:
            quality["analysis_receipts"] = list(self._analysis_receipts)
        if extra_quality:
            quality.update({k: v for k, v in extra_quality.items() if v is not None})
        if export_completed:
            # Single choke point for every export path — measure once, here, so no caller
            # has to probe the file itself.
            export_qc = self._export_qc(outputs)
            if export_qc:
                quality["export_qc"] = export_qc
        self._finish_progress(status, str(quality.get("progress_summary") or ""), outputs)
        return {
            "status": status,
            "outputs": outputs,
            "questions": questions or [],
            "errors": errors,
            "quality": quality,
            "final_screenshot": final_screenshot,
        }

    @staticmethod
    def _error(exc: Exception) -> dict:
        if isinstance(exc, ApiTransportError):
            return {"code": exc.code, "message": exc.message, "details": exc.details}
        return {"code": "internal_error", "message": str(exc), "details": {"type": type(exc).__name__}}

    @staticmethod
    def _cancelled(job_id: str) -> bool:
        # Cooperative cancellation: every wait polls jobs.is_cancel_requested so /cut and the web
        # cancel actually stop the run rather than only marking the record.
        if not job_id:
            return False
        try:
            from . import jobs

            return bool(jobs.is_cancel_requested(job_id))
        except Exception:  # noqa: BLE001 — never let a cancel probe crash the run
            return False

    def _raise_if_cancelled(self, job_id: str) -> None:
        if self._cancelled(job_id):
            raise _JobCancelled()

    @staticmethod
    def _questions_summary(questions: list[dict]) -> str | None:
        for q in questions:
            if isinstance(q, dict) and q.get("question"):
                return str(q["question"])[:700]
        return None

    @staticmethod
    def _job_timeout(job: dict) -> float:
        # timeout_sec is a tool parameter, so the model picks it. Unclamped, a large
        # value outlives whatever wall the host puts on a blocking call, and the host
        # kills the call instead of the plugin answering — the one outcome that loses
        # the job_id needed to re-attach. The ceiling keeps the answer on our side.
        ceiling = _env_num("CASSETTE_MCP_MAX_BLOCKING_SEC", 1500.0, 60.0, getter=os.getenv)
        try:
            requested = float(job.get("timeout_sec") or 1800)
        except (TypeError, ValueError):
            requested = 1800.0
        return min(max(60.0, requested), ceiling)

    @staticmethod
    def _export_timeout(job: dict) -> float:
        # A reviewed-completion export gets its own budget (env override, else the job timeout),
        # so it never inherits an already-exhausted run deadline.
        raw = _env("CASSETTE_EXPORT_TIMEOUT_SEC")
        if raw:
            try:
                return max(60.0, float(raw))
            except ValueError:
                pass
        return ApiTransport._job_timeout(job)

    @staticmethod
    def _media_ready_timeout() -> float:
        # How long to wait for uploaded media to become fully ready. Agentic analysis and the
        # render source take real time for longer clips. Prefer CASSETTE_API_MEDIA_READY_TIMEOUT_SEC,
        # then the older CASSETTE_UPLOAD_TIMEOUT_SEC, then a default.
        raw = _env("CASSETTE_API_MEDIA_READY_TIMEOUT_SEC") or _env("CASSETTE_UPLOAD_TIMEOUT_SEC")
        try:
            return max(30.0, float(raw or "600"))
        except ValueError:
            return 600.0

    @staticmethod
    def _run_start_timeout() -> float:
        # How long a run may stay 'pending' before we declare the queue stalled. Generous by default
        # to tolerate cold starts; override with CASSETTE_API_RUN_START_TIMEOUT_SEC.
        return _env_num("CASSETTE_API_RUN_START_TIMEOUT_SEC", 120.0, 30.0)

    @staticmethod
    def _poll_interval() -> float:
        return _env_num("CASSETTE_API_POLL_INTERVAL_SEC", 3.0, 1.0)

    @staticmethod
    def _recursion_limit() -> int:
        return _env_num("CASSETTE_API_RECURSION_LIMIT", 344, 25, cast=int)

    @staticmethod
    def _http_timeout_for_upload(num_bytes: int) -> float:
        # Allow large uploads/downloads more time than ordinary JSON calls.
        return max(60.0, _http_timeout(), num_bytes / (256 * 1024))
