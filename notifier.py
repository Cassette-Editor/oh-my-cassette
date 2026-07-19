from __future__ import annotations

import asyncio
import base64
import re
import string
import os
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"succeeded", "failed", "needs_user", "timed_out", "cancelled"}


def _run_async_send(factory) -> dict:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: dict[str, Any] = {}
    error: BaseException | None = None

    def target() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(factory())
        except BaseException as exc:
            error = exc

    thread = threading.Thread(target=target, name="cassette-notifier", daemon=True)
    thread.start()
    thread.join()
    if error is not None:
        raise error
    return result


def _hermes_agent_root() -> Path:
    return Path(os.getenv("HERMES_AGENT_ROOT", "~/.hermes/hermes-agent")).expanduser()


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _hermes_dotenv_value(name: str) -> str:
    path = Path(os.getenv("HERMES_ENV_FILE", str(_hermes_home() / ".env"))).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    prefix = f"{name}="
    export_prefix = f"export {name}="
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(export_prefix):
            return _unquote_env_value(stripped[len(export_prefix) :])
        if stripped.startswith(prefix):
            return _unquote_env_value(stripped[len(prefix) :])
    return ""


def _runtime_env(name: str) -> str:
    return str(os.getenv(name, "") or _hermes_dotenv_value(name)).strip()


def _error_codes(job: dict) -> str:
    codes = [str(err.get("code") or "unknown") for err in job.get("errors", []) if isinstance(err, dict)]
    return ", ".join(codes) if codes else "none"


def _is_disabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _is_enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _send_original_zip_enabled() -> bool:
    return _is_enabled(os.getenv("CASSETTE_WEIXIN_SEND_ORIGINAL_ZIP"))


def _weixin_cdn_upload_attempts() -> int:
    raw = str(os.getenv("CASSETTE_WEIXIN_CDN_UPLOAD_ATTEMPTS", "3")).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _exported_media_paths(job: dict) -> list[str]:
    paths: list[str] = []
    for output in job.get("outputs") or []:
        if not isinstance(output, dict):
            continue
        path = str(output.get("local_path") or "")
        if path and Path(path).exists():
            paths.append(path)
    return paths


def _is_video_export(path: str) -> bool:
    return Path(path).suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"}


def _max_zip_bytes() -> int:
    raw = str(os.getenv("CASSETTE_WEIXIN_ORIGINAL_ZIP_MAX_BYTES", str(25 * 1024 * 1024))).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 25 * 1024 * 1024


def _telegram_video_max_bytes() -> int:
    # Telegram Bot API multipart uploads are capped at 50 MB for non-photo files.
    raw = str(os.getenv("CASSETTE_TELEGRAM_VIDEO_MAX_BYTES", "50000000")).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 50_000_000


def _telegram_preview_target_bytes() -> int:
    raw = str(os.getenv("CASSETTE_TELEGRAM_PREVIEW_TARGET_BYTES", "45000000")).strip()
    try:
        return max(1, min(int(raw), _telegram_video_max_bytes() - 1024))
    except ValueError:
        return min(45_000_000, _telegram_video_max_bytes() - 1024)


def _video_metadata(file_path: str) -> dict[str, int | float]:
    ffprobe_bin = os.getenv("CASSETTE_FFPROBE_BIN", "ffprobe")
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration:format=duration",
        "-of",
        "json",
        file_path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        import json

        payload = json.loads(proc.stdout or "{}")
    except Exception:
        return {}
    stream = (payload.get("streams") or [{}])[0] if isinstance(payload.get("streams"), list) else {}
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}

    def int_value(value: Any) -> int | None:
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            return None
        return result if result > 0 else None

    def float_value(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result > 0 else None

    metadata: dict[str, int | float] = {}
    width = int_value(stream.get("width"))
    height = int_value(stream.get("height"))
    duration = float_value(stream.get("duration") or fmt.get("duration"))
    if width:
        metadata["width"] = width
    if height:
        metadata["height"] = height
    if duration:
        metadata["duration"] = duration
    return metadata


def _telegram_video_send_kwargs(file_path: str) -> dict[str, Any]:
    metadata = _video_metadata(file_path)
    kwargs: dict[str, Any] = {"supports_streaming": True}
    if metadata.get("width"):
        kwargs["width"] = int(metadata["width"])
    if metadata.get("height"):
        kwargs["height"] = int(metadata["height"])
    if metadata.get("duration"):
        kwargs["duration"] = max(1, int(round(float(metadata["duration"]))))
    return kwargs


def _telegram_export_reference(file_path: str) -> str:
    source = Path(file_path)
    try:
        marker = source.parts.index("exports")
        return "${CASSETTE_ASSET_ROOT}/" + "/".join(source.parts[marker:])
    except ValueError:
        return f"exports/{source.name}"


def _prepare_telegram_preview_video(file_path: str) -> Path:
    source = Path(file_path)
    if not source.exists():
        raise FileNotFoundError(str(source))
    max_bytes = _telegram_video_max_bytes()
    target_bytes = _telegram_preview_target_bytes()
    out_dir = source.parent / "telegram_delivery"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{source.stem}.telegram-preview.mp4"
    if dest.exists() and dest.stat().st_mtime >= source.stat().st_mtime and 0 < dest.stat().st_size <= max_bytes:
        return dest

    metadata = _video_metadata(str(source))
    duration = max(1.0, float(metadata.get("duration") or 60.0))
    ffmpeg_bin = os.getenv("CASSETTE_FFMPEG_BIN", "ffmpeg")
    scale_filters = [
        os.getenv("CASSETTE_TELEGRAM_PREVIEW_SCALE", "scale=-2:720,format=yuv420p"),
        "scale=-2:540,format=yuv420p",
        "scale=-2:360,format=yuv420p",
    ]
    audio_bitrates = ["96k", "80k", "64k"]
    target_factors = [1.0, 0.9, 0.8]

    last_detail = ""
    for index, scale_filter in enumerate(scale_filters):
        audio_kbps = int(audio_bitrates[min(index, len(audio_bitrates) - 1)].removesuffix("k"))
        target = int(target_bytes * target_factors[min(index, len(target_factors) - 1)])
        total_kbps = max(300, int((target * 8) / duration / 1000))
        video_kbps = max(220, total_kbps - audio_kbps - 64)
        fd, tmp_name = tempfile.mkstemp(prefix=".telegram.", suffix=".mp4", dir=str(out_dir))
        os.close(fd)
        tmp_path = Path(tmp_name)
        cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-map",
            "-0:d?",
            "-vf",
            scale_filter,
            "-c:v",
            "libx264",
            "-preset",
            os.getenv("CASSETTE_TELEGRAM_PREVIEW_PRESET", "veryfast"),
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{video_kbps}k",
            "-bufsize",
            f"{video_kbps * 2}k",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrates[min(index, len(audio_bitrates) - 1)],
            "-movflags",
            "+faststart",
            str(tmp_path),
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as exc:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise RuntimeError("ffmpeg_missing") from exc
        if proc.returncode == 0 and tmp_path.exists() and 0 < tmp_path.stat().st_size <= max_bytes:
            os.replace(tmp_path, dest)
            return dest
        last_detail = (proc.stderr or "").strip()[
            -300:
        ] or f"size={tmp_path.stat().st_size if tmp_path.exists() else 0}"
        try:
            tmp_path.unlink()
        except OSError:
            pass
    raise RuntimeError(f"telegram_preview_transcode_failed:{last_detail}")


def _zip_original_export(file_path: str) -> Path:
    source = Path(file_path)
    if not source.exists():
        raise FileNotFoundError(str(source))
    out_dir = source.parent / "weixin_delivery"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{source.stem}.original.zip"
    if zip_path.exists() and zip_path.stat().st_mtime >= source.stat().st_mtime and zip_path.stat().st_size > 0:
        return zip_path
    fd, tmp_name = tempfile.mkstemp(prefix=".original.", suffix=".zip", dir=str(out_dir))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(source, arcname=source.name)
        if tmp_path.stat().st_size > _max_zip_bytes():
            raise RuntimeError(f"original_zip_too_large:{tmp_path.stat().st_size}")
        os.replace(tmp_path, zip_path)
        return zip_path
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _delivery_profiles() -> list[dict[str, str | bool]]:
    if os.getenv("CASSETTE_WEIXIN_DELIVERY_SCALE"):
        return [
            {
                "name": "custom",
                "scale": os.getenv("CASSETTE_WEIXIN_DELIVERY_SCALE", "scale=-2:480,format=yuv420p"),
                "crf": os.getenv("CASSETTE_WEIXIN_DELIVERY_CRF", "32"),
                "audio_bitrate": os.getenv("CASSETTE_WEIXIN_DELIVERY_AUDIO_BITRATE", "64k"),
                "audio": not _is_disabled(os.getenv("CASSETTE_WEIXIN_DELIVERY_AUDIO", "1")),
            }
        ]
    return [
        {
            "name": "480p",
            "scale": "scale=-2:480,format=yuv420p",
            "crf": "32",
            "audio_bitrate": "64k",
            "audio": True,
        },
        {
            "name": "360p-silent",
            "scale": "scale=-2:360,format=yuv420p",
            "crf": "38",
            "audio_bitrate": "",
            "audio": False,
        },
    ]


def _prepare_weixin_video_delivery_file(file_path: str, profile: dict[str, str | bool] | None = None) -> Path:
    source = Path(file_path)
    if not source.exists():
        raise FileNotFoundError(str(source))
    profile = profile or _delivery_profiles()[0]
    profile_name = str(profile["name"])
    out_dir = source.parent / "weixin_delivery"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{source.stem}.weixin-{profile_name}.mp4"
    if dest.exists() and dest.stat().st_mtime >= source.stat().st_mtime and dest.stat().st_size > 0:
        return dest
    ffmpeg_bin = os.getenv("CASSETTE_FFMPEG_BIN", "ffmpeg")
    fd, tmp_name = tempfile.mkstemp(prefix=".weixin.", suffix=".mp4", dir=str(out_dir))
    os.close(fd)
    tmp_path = Path(tmp_name)
    scale_filter = str(profile["scale"])
    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
    ]
    if profile.get("audio"):
        cmd.extend(["-map", "0:a?"])
    else:
        cmd.append("-an")
    cmd.extend(
        [
            "-map",
            "-0:d?",
            "-vf",
            scale_filter,
            "-c:v",
            "libx264",
            "-profile:v",
            os.getenv("CASSETTE_WEIXIN_DELIVERY_PROFILE", "high"),
            "-level",
            os.getenv("CASSETTE_WEIXIN_DELIVERY_LEVEL", "3.1"),
            "-preset",
            os.getenv("CASSETTE_WEIXIN_DELIVERY_PRESET", "veryfast"),
            "-crf",
            str(profile["crf"]),
        ]
    )
    if profile.get("audio"):
        cmd.extend(["-c:a", "aac", "-b:a", str(profile["audio_bitrate"])])
    cmd.append(str(tmp_path))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise RuntimeError("ffmpeg_missing") from exc
    if proc.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size <= 0:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        detail = (proc.stderr or "").strip()[-300:]
        raise RuntimeError(f"weixin_delivery_transcode_failed:{detail}")
    os.replace(tmp_path, dest)
    return dest


def _openclaw_aes_key_for_api(hermes_aes_key_for_api: str) -> str:
    """Convert Hermes' historical base64(hex-key) shape to openclaw's base64(raw-key)."""
    try:
        decoded = base64.b64decode(hermes_aes_key_for_api, validate=True).decode("ascii")
    except Exception:
        return hermes_aes_key_for_api
    if len(decoded) != 32 or any(char not in string.hexdigits for char in decoded):
        return hermes_aes_key_for_api
    return base64.b64encode(bytes.fromhex(decoded)).decode("ascii")


def _patch_weixin_adapter_for_openclaw_media(adapter: Any) -> None:
    original_builder_method = adapter._outbound_media_builder

    def compat_builder_method(path: str, force_file_attachment: bool = False):
        media_type, item_builder = original_builder_method(path, force_file_attachment=force_file_attachment)

        def compat_item_builder(**kwargs):
            aes_key = kwargs.get("aes_key_for_api")
            if isinstance(aes_key, str):
                kwargs = {**kwargs, "aes_key_for_api": _openclaw_aes_key_for_api(aes_key)}
            return item_builder(**kwargs)

        return media_type, compat_item_builder

    adapter._outbound_media_builder = compat_builder_method


def _install_weixin_cdn_retry(weixin_module: Any):
    original_upload = weixin_module._upload_ciphertext
    if getattr(original_upload, "_cassette_retry_wrapper", False):
        return lambda: None

    async def retrying_upload_ciphertext(session, *, ciphertext: bytes, upload_url: str) -> str:
        attempts = _weixin_cdn_upload_attempts()
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await original_upload(session, ciphertext=ciphertext, upload_url=upload_url)
            except Exception as exc:
                if "HTTP 4" in str(exc):
                    raise
                last_exc = exc
                if attempt < attempts:
                    await asyncio.sleep(min(5, attempt * 2))
        if last_exc:
            raise last_exc
        raise RuntimeError("CDN upload failed")

    retrying_upload_ciphertext._cassette_retry_wrapper = True  # type: ignore[attr-defined]
    weixin_module._upload_ciphertext = retrying_upload_ciphertext

    def restore() -> None:
        weixin_module._upload_ciphertext = original_upload

    return restore


def _normalize_platform(platform: str | None) -> str:
    value = str(platform or "").strip().lower()
    if value in {"qq", "qqbot", "qq_bot"}:
        return "qqbot"
    if value in {"telegram", "tg", "telegram_bot", "telegrambot"}:
        return "telegram"
    if value in {"wechat", "weixin", "wx"}:
        return "weixin"
    if value in {"web", "browser", "web_demo", "webdemo"}:
        return "web"
    return value


def _platform_label(platform: str | None) -> str:
    normalized = _normalize_platform(platform)
    if normalized == "qqbot":
        return "QQ"
    if normalized == "weixin":
        return "微信"
    if normalized == "telegram":
        return "Telegram"
    if normalized == "web":
        return "网页"
    return "当前平台"


def _language_for_platform(platform: str | None, job: dict | None = None) -> str:
    raw = str((job or {}).get("cassette_language") or "").strip().lower()
    if raw in {"en", "english"}:
        return "en"
    if raw in {"zh", "chinese", "cn"}:
        return "zh"
    return "en" if _normalize_platform(platform) == "telegram" else "zh"


def _normalize_qq_chat_type(chat_type: str | None) -> str:
    value = str(chat_type or "").strip().lower()
    if value in {"group", "guild"}:
        return value
    return "c2c"


class _QQDirectRestTransport:
    closed = False


def _enable_qq_direct_rest_mode(adapter: Any) -> None:
    # Cassette notifications use QQ's REST send/upload APIs from a short-lived
    # adapter; no gateway WebSocket listener exists in this process.
    adapter._running = True
    adapter._ws = _QQDirectRestTransport()


def _is_export_only_progress_summary(summary: str) -> bool:
    value = str(summary or "").strip().lower()
    return value.startswith("export status:") or value in {
        "export queued",
        "export rendering",
        "render complete",
    }


def _clean_terminal_summary(summary: str) -> str:
    cleaned = " ".join(str(summary or "").split()).strip()
    for suffix in (" Copy", " 复制"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
    return cleaned


def _trim_generic_completion_prefix(summary: str) -> str:
    cleaned = _clean_terminal_summary(summary)
    prefixes = (
        "The edit is complete and ready to export.",
        "Edit complete and ready to export.",
        "Cassette edit completed and the exported video is ready.",
        "剪辑完成，可以导出。",
        "剪辑已完成，可以导出。",
    )
    lowered = cleaned.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix.lower()):
            trimmed = cleaned[len(prefix) :].strip()
            return trimmed or cleaned
    return cleaned


def _summary_bullets(summary: str, *, language: str) -> list[str]:
    cleaned = _trim_generic_completion_prefix(summary)
    if not cleaned:
        return []
    if language == "en":
        parts = re.split(r"(?<=[.!?])\s+", cleaned)
    else:
        parts = re.split(r"(?<=[。！？])\s*", cleaned)
    bullets = [part.strip(" -•\t") for part in parts if part.strip(" -•\t")]
    return bullets[:4] or [cleaned]


def _terminal_summary(job: dict) -> str:
    quality = job.get("quality") or {}
    for key in ("final_summary", "progress_summary"):
        summary = _clean_terminal_summary(str(quality.get(key) or ""))
        if summary:
            return summary
    progress_events = job.get("progress_events") or []
    fallback = ""
    for event in reversed(progress_events):
        if not isinstance(event, dict):
            continue
        summary = _clean_terminal_summary(str(event.get("summary") or ""))
        if not summary:
            continue
        if job.get("status") == "succeeded" and _is_export_only_progress_summary(summary):
            fallback = fallback or summary
            continue
        return summary
    return fallback


def _append_summary_block(lines: list[str], summary: str, *, language: str, status: str) -> None:
    if not summary:
        return
    label = "Edit Summary" if language == "en" and status == "succeeded" else "Progress"
    if language != "en":
        label = "剪辑摘要" if status == "succeeded" else "进度/页面总结"
    lines.extend(["", f"**{label}**"])
    for bullet in _summary_bullets(summary[:800], language=language):
        lines.append(f"- {bullet}")


def _append_delivery_block(lines: list[str], message: str, *, language: str) -> None:
    if not message:
        return
    lines.extend(["", "**Delivery**" if language == "en" else "**交付状态**", f"- {message}"])


def format_platform_final_message(job: dict, media_delivery: str | None = None, platform: str | None = None) -> str:
    platform = _normalize_platform(platform or (job.get("delivery") or {}).get("platform"))
    platform_label = _platform_label(platform)
    language = _language_for_platform(platform, job)
    status = job.get("status") or "unknown"
    quality = job.get("quality") or {}
    latest_progress = _terminal_summary(job)
    output_count = len(job.get("outputs") or job.get("output_links") or [])
    exported_paths = _exported_media_paths(job)
    export_pending = bool(quality.get("export_pending")) or (status == "succeeded" and output_count == 0)

    if language == "en":
        if status == "succeeded":
            headline = "Cassette edit completed and the exported video is ready."
        elif status == "needs_user":
            headline = "Cassette needs manual confirmation before it can continue."
        elif status == "timed_out":
            headline = "Cassette timed out before exposing a clear completion signal."
        elif status == "failed":
            headline = f"Cassette edit failed. Error code(s): {_error_codes(job)}."
        elif status == "cancelled":
            headline = "Cassette edit was paused."
        else:
            headline = f"Cassette job status: {status}."

        lines = [f"**{headline}**", f"Job: `{job.get('job_id', '')}`"]
        _append_summary_block(lines, latest_progress, language=language, status=status)
        if status == "succeeded":
            delivery_line = ""
            if exported_paths:
                if media_delivery == "sent":
                    delivery_line = "Exported video has been sent."
                elif media_delivery == "sent_telegram_preview":
                    delivery_line = (
                        "Telegram Bot API limits uploaded videos to 50 MB, so I sent a compressed preview video. "
                        f"The original Cassette export remains saved at `{_telegram_export_reference(exported_paths[0])}`."
                    )
                elif media_delivery == "failed":
                    delivery_line = f"The exported video was generated, but {platform_label} video delivery failed."
                else:
                    delivery_line = "Exported video was generated and is being sent."
            elif output_count:
                delivery_line = (
                    f"Detected {output_count} output link(s), but no local exported video file was confirmed."
                )
            elif export_pending:
                delivery_line = "No exported video file was detected yet."
            _append_delivery_block(lines, delivery_line, language=language)
        return "\n".join(lines).strip()

    if status == "succeeded":
        headline = "Cassette 剪辑任务已完成，导出视频已生成。"
    elif status == "needs_user":
        headline = "Cassette 剪辑任务需要人工确认后才能继续。"
    elif status == "timed_out":
        headline = "Cassette 剪辑任务超时，未检测到明确完成信号。"
    elif status == "failed":
        headline = f"Cassette 剪辑任务失败。错误码：{_error_codes(job)}。"
    elif status == "cancelled":
        headline = "Cassette 剪辑任务已取消。"
    else:
        headline = f"Cassette 剪辑任务状态：{status}。"

    lines = [f"**{headline}**", f"Job: `{job.get('job_id', '')}`"]
    _append_summary_block(lines, latest_progress, language=language, status=status)
    if status == "succeeded":
        delivery_line = ""
        if exported_paths:
            if media_delivery == "sent":
                delivery_line = "导出视频已发送。"
            elif media_delivery == "sent_telegram_preview":
                delivery_line = (
                    "Telegram Bot API 上传视频限制为 50 MB，因此已发送压缩后的预览视频。"
                    f"原始 Cassette 导出文件仍保存在 `{_telegram_export_reference(exported_paths[0])}`。"
                )
            elif media_delivery == "sent_compatible":
                delivery_line = "原始导出视频不被微信 CDN 接受，已转换为微信兼容 MP4 并发送。"
            elif media_delivery == "sent_preview_zip":
                delivery_line = (
                    "已发送低码率 MP4 预览；随后发送的 zip 文件包含原始大小导出视频，请解压后查看/保存原片。"
                )
            elif media_delivery == "sent_preview_zip_failed":
                delivery_line = "已发送低码率 MP4 预览；原始导出视频 zip 文件发送失败。"
            elif media_delivery == "sent_preview":
                delivery_line = "原始导出视频不被微信 CDN 接受，已发送低码率微信兼容 MP4 预览。"
            elif media_delivery == "failed":
                delivery_line = f"导出视频已生成，但{platform_label}视频发送失败。"
            else:
                delivery_line = "导出视频已生成，准备发送。"
        elif output_count:
            delivery_line = f"已检测到 {output_count} 个输出链接，但未能确认本地导出文件。"
        elif export_pending:
            delivery_line = "当前未检测到导出视频文件。"
        _append_delivery_block(lines, delivery_line, language=language)
    return "\n".join(lines).strip()


async def _send_weixin_text(chat_id: str, message: str) -> dict:
    from gateway.platforms.weixin import send_weixin_direct

    return await send_weixin_direct(
        extra={},
        token=_runtime_env("WEIXIN_TOKEN"),
        chat_id=str(chat_id),
        message=message,
        media_files=None,
    )


async def _send_weixin_image_attachment(chat_id: str, file_path: str, caption: str = "") -> dict:
    from gateway.platforms.weixin import send_weixin_direct

    result = await send_weixin_direct(
        extra={},
        token=_runtime_env("WEIXIN_TOKEN"),
        chat_id=str(chat_id),
        message=caption,
        media_files=[(file_path, False)],
    )
    if result.get("success"):
        return {"success": True, "message_id": result.get("message_id"), "mode": "native"}
    return {"success": False, "error": str(result.get("error") or "unknown")[:300]}


async def _weixin_send_with_adapter(call):
    try:
        from gateway.config import PlatformConfig
        from gateway.platforms import weixin as weixin_module
        from gateway.platforms.weixin import (
            ILINK_BASE_URL,
            WEIXIN_CDN_BASE_URL,
            ContextTokenStore,
            WeixinAdapter,
            _make_ssl_connector,
        )
        from hermes_constants import get_hermes_home
        import aiohttp
    except Exception as exc:
        return {"success": False, "error": f"weixin_import_failed:{type(exc).__name__}"}

    token = _runtime_env("WEIXIN_TOKEN")
    account_id = _runtime_env("WEIXIN_ACCOUNT_ID")
    if not token:
        return {"success": False, "error": "missing_weixin_token"}
    if not account_id:
        return {"success": False, "error": "missing_weixin_account_id"}

    base_url = str(_runtime_env("WEIXIN_BASE_URL") or ILINK_BASE_URL).strip().rstrip("/")
    cdn_base_url = str(_runtime_env("WEIXIN_CDN_BASE_URL") or WEIXIN_CDN_BASE_URL).strip().rstrip("/")
    token_store = ContextTokenStore(str(get_hermes_home()))
    token_store.restore(account_id)
    try:
        async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
            restore_retry = _install_weixin_cdn_retry(weixin_module)
            adapter = WeixinAdapter(
                PlatformConfig(
                    enabled=True,
                    token=token,
                    extra={
                        "account_id": account_id,
                        "base_url": base_url,
                        "cdn_base_url": cdn_base_url,
                    },
                )
            )
            adapter._send_session = session
            adapter._session = session
            adapter._token = token
            adapter._account_id = account_id
            adapter._base_url = base_url
            adapter._cdn_base_url = cdn_base_url
            adapter._token_store = token_store
            _patch_weixin_adapter_for_openclaw_media(adapter)
            try:
                return await call(adapter)
            finally:
                restore_retry()
    except Exception as exc:
        return {"success": False, "error": str(exc)[:300]}


async def _send_weixin_video_attachment(chat_id: str, file_path: str) -> dict:
    async def _call(adapter):
        try:
            message_id = await adapter._send_file(str(chat_id), file_path, "")
            return {"success": True, "message_id": message_id, "mode": "native"}
        except Exception as first_exc:
            if not _is_video_export(file_path) or _is_disabled(os.getenv("CASSETTE_WEIXIN_DELIVERY_TRANSCODE", "1")):
                return {"success": False, "error": str(first_exc)[:300]}
            try:
                errors = []
                for profile in _delivery_profiles():
                    try:
                        compatible_path = _prepare_weixin_video_delivery_file(file_path, profile)
                        message_id = await adapter._send_file(str(chat_id), str(compatible_path), "")
                        return {
                            "success": True,
                            "message_id": message_id,
                            "mode": "weixin_compatible_mp4",
                            "media_profile": profile["name"],
                            "fallback_error": str(first_exc)[:200],
                        }
                    except Exception as profile_exc:
                        errors.append(f"{profile['name']}:{str(profile_exc)[:80]}")
                raise RuntimeError("; ".join(errors))
            except Exception as fallback_exc:
                return {
                    "success": False,
                    "error": str(first_exc)[:200],
                    "fallback_error": str(fallback_exc)[:200],
                }

    return await _weixin_send_with_adapter(_call)


async def _qq_send_with_adapter(chat_id: str, chat_type: str | None, call):
    try:
        from gateway.config import PlatformConfig
        from gateway.platforms._http_client_limits import platform_httpx_limits
        from gateway.platforms.qqbot.adapter import QQAdapter
        import httpx
    except Exception as exc:
        return {"success": False, "error": f"qq_import_failed:{type(exc).__name__}"}

    app_id = _runtime_env("QQ_APP_ID")
    client_secret = _runtime_env("QQ_CLIENT_SECRET")
    if not app_id:
        return {"success": False, "error": "missing_qq_app_id"}
    if not client_secret:
        return {"success": False, "error": "missing_qq_client_secret"}

    adapter = QQAdapter(
        PlatformConfig(enabled=True, token="", extra={"app_id": app_id, "client_secret": client_secret})
    )
    adapter._chat_type_map[str(chat_id)] = _normalize_qq_chat_type(chat_type)
    _enable_qq_direct_rest_mode(adapter)
    adapter._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, limits=platform_httpx_limits())
    try:
        return await call(adapter)
    except Exception as exc:
        return {"success": False, "error": str(exc)[:300]}
    finally:
        if adapter._http_client:
            await adapter._http_client.aclose()


async def _send_qq_text(chat_id: str, message: str, chat_type: str | None = None) -> dict:
    async def _call(adapter):
        result = await adapter.send(str(chat_id), message)
        if result.success:
            return {"success": True, "message_id": result.message_id}
        return {"success": False, "error": str(result.error or "unknown")[:300]}

    return await _qq_send_with_adapter(chat_id, chat_type, _call)


async def _send_qq_video_attachment(chat_id: str, file_path: str, chat_type: str | None = None) -> dict:
    async def _call(adapter):
        result = await adapter.send_video(str(chat_id), file_path)
        if result.success:
            return {"success": True, "message_id": result.message_id, "mode": "native"}
        return {"success": False, "error": str(result.error or "unknown")[:300]}

    return await _qq_send_with_adapter(chat_id, chat_type, _call)


async def _send_qq_image_attachment(
    chat_id: str, file_path: str, caption: str = "", chat_type: str | None = None
) -> dict:
    async def _call(adapter):
        result = await adapter.send_image_file(str(chat_id), file_path, caption=caption)
        if result.success:
            return {"success": True, "message_id": result.message_id, "mode": "native"}
        return {"success": False, "error": str(result.error or "unknown")[:300]}

    return await _qq_send_with_adapter(chat_id, chat_type, _call)


def _telegram_thread_metadata(thread_id: str | None = None) -> dict[str, str] | None:
    value = str(thread_id or "").strip()
    return {"thread_id": value} if value else None


async def _telegram_send_with_adapter(call):
    try:
        from gateway.config import PlatformConfig
        from gateway.platforms.telegram import TelegramAdapter
        from telegram import Bot
        from telegram.request import HTTPXRequest
    except Exception as exc:
        return {"success": False, "error": f"telegram_import_failed:{type(exc).__name__}"}

    token = _runtime_env("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"success": False, "error": "missing_telegram_bot_token"}

    try:
        request = HTTPXRequest(
            connect_timeout=10.0,
            read_timeout=30.0,
            write_timeout=60.0,
            pool_timeout=8.0,
        )
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token=token, extra={}))
        adapter._bot = Bot(token=token, request=request)
        try:
            await adapter._bot.initialize()
        except Exception:
            pass
        try:
            result = await call(adapter)
        finally:
            try:
                await adapter._bot.shutdown()
            except Exception:
                pass
    except Exception as exc:
        return {"success": False, "error": str(exc)[:300]}
    if isinstance(result, dict):
        return result
    if getattr(result, "success", False):
        return {"success": True, "message_id": getattr(result, "message_id", None), "mode": "native"}
    return {"success": False, "error": str(getattr(result, "error", "") or "unknown")[:300]}


async def _send_telegram_text(chat_id: str, message: str, thread_id: str | None = None) -> dict:
    async def _call(adapter):
        return await adapter.send(str(chat_id), message, metadata=_telegram_thread_metadata(thread_id))

    return await _telegram_send_with_adapter(_call)


async def _send_telegram_image_attachment(
    chat_id: str, file_path: str, caption: str = "", thread_id: str | None = None
) -> dict:
    async def _call(adapter):
        return await adapter.send_image_file(
            str(chat_id),
            file_path,
            caption=caption,
            metadata=_telegram_thread_metadata(thread_id),
        )

    return await _telegram_send_with_adapter(_call)


async def _send_telegram_video_attachment(chat_id: str, file_path: str, thread_id: str | None = None) -> dict:
    async def _call(adapter):
        if not adapter._bot:
            return {"success": False, "error": "telegram_bot_not_connected"}
        kwargs = _telegram_video_send_kwargs(file_path)
        message_thread_id = None
        if str(thread_id or "").strip():
            try:
                message_thread_id = int(str(thread_id).strip())
            except ValueError:
                message_thread_id = None
        with open(file_path, "rb") as video_file:
            message = await adapter._bot.send_video(
                chat_id=int(chat_id),
                video=video_file,
                message_thread_id=message_thread_id,
                read_timeout=30,
                write_timeout=120,
                connect_timeout=10,
                pool_timeout=8,
                **kwargs,
            )
        return {
            "success": True,
            "message_id": str(getattr(message, "message_id", "")),
            "mode": "native",
            **{key: kwargs[key] for key in ("width", "height", "duration") if key in kwargs},
        }

    return await _telegram_send_with_adapter(_call)


async def _send_weixin_file_attachment(chat_id: str, file_path: str) -> dict:
    async def _call(adapter):
        message_id = await adapter._send_file(str(chat_id), file_path, "")
        return {"success": True, "message_id": message_id}

    return await _weixin_send_with_adapter(_call)


def format_progress_snapshot_message(job: dict, summary: str = "") -> str:
    del summary
    platform = _normalize_platform((job.get("delivery") or {}).get("platform"))
    language = _language_for_platform(platform, job)
    quality = job.get("quality") or {}
    stage = job.get("current_stage") or quality.get("current_stage") or "agent"
    stage_timings = job.get("stage_timings") or quality.get("stage_timings") or {}
    stage_data = stage_timings.get(stage) if isinstance(stage_timings, dict) else {}
    elapsed = stage_data.get("duration_sec") if isinstance(stage_data, dict) else None
    attempts = stage_data.get("attempts") if isinstance(stage_data, dict) else None
    if language == "en":
        lines = [
            "Cassette is working. Current page screenshot attached.",
            f"Job: {job.get('job_id', '')}",
            f"Stage: {stage}",
        ]
    else:
        lines = [
            "Cassette 正在执行剪辑任务，下面是当前页面状态截图。",
            f"Job: {job.get('job_id', '')}",
            f"阶段: {stage}",
        ]
    if elapsed is not None:
        lines.append(f"{'Stage elapsed' if language == 'en' else '阶段耗时'}: {elapsed}s")
    if attempts:
        lines.append(f"{'Attempts' if language == 'en' else '尝试次数'}: {attempts}")
    return "\n".join(line for line in lines if line)


def format_model_selection_message(model_selection: dict, language: str = "zh") -> str:
    model = str(model_selection.get("model") or "DeepSeek V4 Flash")
    thinking = str(model_selection.get("thinking_level") or "Low")
    if _language_for_platform(None, {"cassette_language": language}) == "en":
        return f"Cassette model selected: {model}; thinking level: {thinking}."
    thinking_label = {"Low": "低", "Medium": "中", "High": "高"}.get(thinking, thinking)
    return f"Cassette 已选择模型：{model}，思考程度：{thinking_label}。"


def _send_web_outbox(
    chat_id: str,
    message: str,
    *,
    reason: str = "text",
    attachment_path: str = "",
    attachment_type: str = "",
    job_id: str = "",
    extra: dict[str, Any] | None = None,
) -> dict:
    try:
        from web_demo import session_store as web_sessions
    except Exception as exc:
        return {"success": False, "error": f"web_outbox_import_failed:{type(exc).__name__}"}
    try:
        event = web_sessions.add_event(
            str(chat_id),
            role="assistant",
            text=message,
            kind=reason,
            attachment_path=attachment_path,
            attachment_type=attachment_type,
            job_id=job_id,
            extra=extra or {},
        )
        return {"success": True, "message_id": str(event.get("id") or "")}
    except Exception as exc:
        return {"success": False, "error": f"web_outbox_write_failed:{type(exc).__name__}"}


def notify_model_selection(job: dict) -> dict:
    delivery = job.get("delivery") or {}
    platform = _normalize_platform(delivery.get("platform"))
    if platform not in {"weixin", "qqbot", "telegram", "web"}:
        return {"status": "skipped", "reason": "unsupported_platform", "platform": platform or ""}
    chat_id = delivery.get("chat_id")
    if not chat_id:
        return {"status": "skipped", "reason": "missing_chat_id"}
    chat_type = delivery.get("chat_type")
    thread_id = delivery.get("thread_id")
    message = format_model_selection_message(job.get("model_selection") or {}, _language_for_platform(platform, job))
    if platform == "web":
        result = _send_web_outbox(str(chat_id), message, reason="model_selection", job_id=str(job.get("job_id") or ""))
        if result.get("success"):
            return {"status": "sent", "platform": platform, "message_id": result.get("message_id")}
        return {
            "status": "failed",
            "platform": platform,
            "code": "web_model_selection_send_failed",
            "error": str(result.get("error") or "unknown")[:200],
        }

    root = _hermes_agent_root()
    if root.exists() and str(root) not in sys.path:
        sys.path.insert(0, str(root))

    async def _send() -> dict:
        if platform == "qqbot":
            return await _send_qq_text(str(chat_id), message, str(chat_type or ""))
        if platform == "telegram":
            return await _send_telegram_text(str(chat_id), message, str(thread_id or ""))
        return await _send_weixin_text(str(chat_id), message)

    try:
        result = _run_async_send(_send)
    except Exception as exc:
        return {
            "status": "failed",
            "platform": platform,
            "code": f"{platform}_model_selection_send_failed",
            "error": type(exc).__name__,
        }
    if result.get("success"):
        return {"status": "sent", "platform": platform, "message_id": result.get("message_id")}
    return {
        "status": "failed",
        "platform": platform,
        "code": f"{platform}_model_selection_send_failed",
        "error": str(result.get("error") or "unknown")[:200],
    }


def notify_gateway_text(delivery: dict, message: str, reason: str = "text") -> dict:
    platform = _normalize_platform(delivery.get("platform"))
    if platform not in {"weixin", "qqbot", "telegram", "web"}:
        return {"status": "skipped", "reason": "unsupported_platform", "platform": platform or ""}
    chat_id = delivery.get("chat_id")
    if not chat_id:
        return {"status": "skipped", "reason": "missing_chat_id", "platform": platform}
    chat_type = delivery.get("chat_type")
    thread_id = delivery.get("thread_id")
    if platform == "web":
        result = _send_web_outbox(str(chat_id), message, reason=reason)
        if result.get("success"):
            return {"status": "sent", "platform": platform, "message_id": result.get("message_id"), "reason": reason}
        return {
            "status": "failed",
            "platform": platform,
            "code": f"web_{reason}_send_failed",
            "error": str(result.get("error") or "unknown")[:200],
        }

    root = _hermes_agent_root()
    if root.exists() and str(root) not in sys.path:
        sys.path.insert(0, str(root))

    async def _send() -> dict:
        if platform == "qqbot":
            return await _send_qq_text(str(chat_id), message, str(chat_type or ""))
        if platform == "telegram":
            return await _send_telegram_text(str(chat_id), message, str(thread_id or ""))
        return await _send_weixin_text(str(chat_id), message)

    try:
        result = _run_async_send(_send)
    except Exception as exc:
        return {
            "status": "failed",
            "platform": platform,
            "code": f"{platform}_{reason}_send_failed",
            "error": type(exc).__name__,
        }
    if result.get("success"):
        return {"status": "sent", "platform": platform, "message_id": result.get("message_id"), "reason": reason}
    return {
        "status": "failed",
        "platform": platform,
        "code": f"{platform}_{reason}_send_failed",
        "error": str(result.get("error") or "unknown")[:200],
    }


def notify_progress_snapshot(job: dict, screenshot_path: str, summary: str = "") -> dict:
    if not screenshot_path or not Path(screenshot_path).exists():
        return {"status": "skipped", "reason": "missing_screenshot"}
    delivery = job.get("delivery") or {}
    platform = _normalize_platform(delivery.get("platform"))
    if platform not in {"weixin", "qqbot", "telegram", "web"}:
        return {"status": "skipped", "reason": "unsupported_platform", "platform": platform or ""}
    chat_id = delivery.get("chat_id")
    if not chat_id:
        return {"status": "skipped", "reason": "missing_chat_id"}
    chat_type = delivery.get("chat_type")
    thread_id = delivery.get("thread_id")
    message = format_progress_snapshot_message(job, summary)
    if platform == "web":
        result = _send_web_outbox(
            str(chat_id),
            message,
            reason="progress_snapshot",
            attachment_path=screenshot_path,
            attachment_type="image",
            job_id=str(job.get("job_id") or ""),
        )
        if result.get("success"):
            return {
                "status": "sent",
                "platform": platform,
                "message_id": result.get("message_id"),
                "media_mode": "web_outbox",
            }
        return {
            "status": "failed",
            "platform": platform,
            "code": "web_progress_snapshot_failed",
            "error": str(result.get("error") or "unknown")[:200],
        }

    root = _hermes_agent_root()
    if root.exists() and str(root) not in sys.path:
        sys.path.insert(0, str(root))

    async def _send() -> dict:
        if platform == "qqbot":
            return await _send_qq_image_attachment(
                str(chat_id), screenshot_path, caption=message, chat_type=str(chat_type or "")
            )
        if platform == "telegram":
            return await _send_telegram_image_attachment(
                str(chat_id), screenshot_path, caption=message, thread_id=str(thread_id or "")
            )
        return await _send_weixin_image_attachment(str(chat_id), screenshot_path, caption=message)

    try:
        result = _run_async_send(_send)
    except Exception as exc:
        return {
            "status": "failed",
            "platform": platform,
            "code": f"{platform}_progress_snapshot_failed",
            "error": type(exc).__name__,
        }
    if result.get("success"):
        return {
            "status": "sent",
            "platform": platform,
            "message_id": result.get("message_id"),
            "media_mode": result.get("mode") or "native",
        }
    return {
        "status": "failed",
        "platform": platform,
        "code": f"{platform}_progress_snapshot_failed",
        "error": str(result.get("error") or "unknown")[:200],
    }


def notify_terminal_job(job: dict) -> dict:
    if job.get("status") not in TERMINAL_STATUSES:
        return {"status": "skipped", "reason": "non_terminal"}
    delivery = job.get("delivery") or {}
    platform = _normalize_platform(delivery.get("platform"))
    if platform not in {"weixin", "qqbot", "telegram", "web"}:
        return {"status": "skipped", "reason": "unsupported_platform", "platform": platform or ""}
    chat_id = delivery.get("chat_id")
    if not chat_id:
        return {"status": "skipped", "reason": "missing_chat_id"}
    chat_type = delivery.get("chat_type")
    thread_id = delivery.get("thread_id")
    if platform == "web":
        exported_paths = _exported_media_paths(job) if job.get("status") == "succeeded" else []
        message = format_platform_final_message(
            job, media_delivery="sent" if exported_paths else None, platform=platform
        )
        result = _send_web_outbox(
            str(chat_id),
            message,
            reason="terminal",
            attachment_path=exported_paths[0] if exported_paths else "",
            attachment_type="video" if exported_paths else "",
            job_id=str(job.get("job_id") or ""),
        )
        if result.get("success"):
            payload = {"status": "sent", "platform": platform, "message_id": result.get("message_id")}
            if exported_paths:
                payload["media_message_id"] = result.get("message_id")
                payload["media_mode"] = "web_download"
            return payload
        return {
            "status": "failed",
            "platform": platform,
            "code": "web_send_failed",
            "error": str(result.get("error") or "unknown")[:200],
        }

    root = _hermes_agent_root()
    if root.exists() and str(root) not in sys.path:
        sys.path.insert(0, str(root))

    async def _send() -> dict:
        exported_paths = _exported_media_paths(job) if job.get("status") == "succeeded" else []
        if exported_paths:
            original_path = exported_paths[0]
            telegram_preview_info: dict[str, Any] = {}
            delivery_path = original_path
            media_result: dict[str, Any] | None = None
            if platform == "telegram":
                try:
                    original_size = Path(original_path).stat().st_size
                except OSError:
                    original_size = 0
                if original_size > _telegram_video_max_bytes():
                    try:
                        preview_path = _prepare_telegram_preview_video(original_path)
                        delivery_path = str(preview_path)
                        telegram_preview_info = {
                            "original_too_large": True,
                            "original_size_bytes": original_size,
                            "preview_path": str(preview_path),
                            "original_reference": _telegram_export_reference(original_path),
                        }
                    except Exception as exc:
                        media_result = {
                            "success": False,
                            "error": f"telegram_preview_prepare_failed:{type(exc).__name__}",
                        }
            if media_result is None:
                if platform == "qqbot":
                    media_result = await _send_qq_video_attachment(str(chat_id), delivery_path, str(chat_type or ""))
                elif platform == "telegram":
                    media_result = await _send_telegram_video_attachment(
                        str(chat_id), delivery_path, str(thread_id or "")
                    )
                else:
                    media_result = await _send_weixin_video_attachment(str(chat_id), delivery_path)
            if not media_result.get("success"):
                message = format_platform_final_message(job, media_delivery="failed", platform=platform)
                if platform == "qqbot":
                    text_result = await _send_qq_text(str(chat_id), message, str(chat_type or ""))
                elif platform == "telegram":
                    text_result = await _send_telegram_text(str(chat_id), message, str(thread_id or ""))
                else:
                    text_result = await _send_weixin_text(str(chat_id), message)
                if text_result.get("success"):
                    return {
                        "success": False,
                        "partial": True,
                        "code": f"{platform}_video_send_failed",
                        "error": str(media_result.get("error") or "unknown")[:200],
                        "text_message_id": text_result.get("message_id"),
                    }
                return {
                    "success": False,
                    "code": f"{platform}_send_failed",
                    "error": str(text_result.get("error") or media_result.get("error") or "unknown")[:200],
                }
            zip_result = None
            if (
                platform == "weixin"
                and media_result.get("mode") == "weixin_compatible_mp4"
                and _send_original_zip_enabled()
            ):
                try:
                    zip_path = _zip_original_export(exported_paths[0])
                    zip_result = await _send_weixin_file_attachment(str(chat_id), str(zip_path))
                except Exception as exc:
                    zip_result = {"success": False, "error": str(exc)[:200]}
            if platform == "weixin" and media_result.get("mode") == "weixin_compatible_mp4":
                if zip_result and zip_result.get("success"):
                    delivery_status = "sent_preview_zip"
                elif zip_result:
                    delivery_status = "sent_preview_zip_failed"
                else:
                    delivery_status = "sent_preview"
            elif platform == "telegram" and telegram_preview_info:
                delivery_status = "sent_telegram_preview"
            else:
                delivery_status = "sent"
            message = format_platform_final_message(job, media_delivery=delivery_status, platform=platform)
            if platform == "qqbot":
                text_result = await _send_qq_text(str(chat_id), message, str(chat_type or ""))
            elif platform == "telegram":
                text_result = await _send_telegram_text(str(chat_id), message, str(thread_id or ""))
            else:
                text_result = await _send_weixin_text(str(chat_id), message)
            if not text_result.get("success"):
                return {
                    "success": False,
                    "partial": True,
                    "code": f"{platform}_text_send_failed",
                    "error": str(text_result.get("error") or "unknown")[:200],
                    "media_message_id": media_result.get("message_id"),
                    "media_mode": media_result.get("mode") or "native",
                    **({"original_too_large": True} if telegram_preview_info else {}),
                    **(
                        {"original_size_bytes": telegram_preview_info.get("original_size_bytes")}
                        if telegram_preview_info
                        else {}
                    ),
                    **(
                        {"zip_message_id": zip_result.get("message_id")}
                        if zip_result and zip_result.get("success")
                        else {}
                    ),
                }
            return {
                "success": True,
                "message_id": text_result.get("message_id"),
                "media_message_id": media_result.get("message_id"),
                "media_mode": media_result.get("mode") or "native",
                **({"original_too_large": True} if telegram_preview_info else {}),
                **(
                    {"original_size_bytes": telegram_preview_info.get("original_size_bytes")}
                    if telegram_preview_info
                    else {}
                ),
                **(
                    {"preview_size_bytes": Path(str(telegram_preview_info.get("preview_path"))).stat().st_size}
                    if telegram_preview_info and Path(str(telegram_preview_info.get("preview_path"))).exists()
                    else {}
                ),
                **(
                    {"original_reference": telegram_preview_info.get("original_reference")}
                    if telegram_preview_info
                    else {}
                ),
                **(
                    {"zip_message_id": zip_result.get("message_id")} if zip_result and zip_result.get("success") else {}
                ),
                **({"zip_error": zip_result.get("error")} if zip_result and not zip_result.get("success") else {}),
            }
        message = format_platform_final_message(job, platform=platform)
        if platform == "qqbot":
            return await _send_qq_text(str(chat_id), message, str(chat_type or ""))
        if platform == "telegram":
            return await _send_telegram_text(str(chat_id), message, str(thread_id or ""))
        return await _send_weixin_text(str(chat_id), message)

    try:
        result = _run_async_send(_send)
    except Exception as exc:
        return {
            "status": "failed",
            "platform": platform,
            "code": f"{platform}_send_failed",
            "error": type(exc).__name__,
        }
    if result.get("success"):
        payload = {"status": "sent", "platform": platform, "message_id": result.get("message_id")}
        if result.get("media_message_id"):
            payload["media_message_id"] = result.get("media_message_id")
        if result.get("media_mode"):
            payload["media_mode"] = result.get("media_mode")
        if result.get("zip_message_id"):
            payload["zip_message_id"] = result.get("zip_message_id")
        if result.get("zip_error"):
            payload["zip_error"] = str(result.get("zip_error"))[:200]
        if result.get("original_too_large"):
            payload["original_too_large"] = True
        if result.get("original_size_bytes"):
            payload["original_size_bytes"] = result.get("original_size_bytes")
        if result.get("preview_size_bytes"):
            payload["preview_size_bytes"] = result.get("preview_size_bytes")
        if result.get("original_reference"):
            payload["original_reference"] = result.get("original_reference")
        return payload
    if result.get("partial"):
        return {
            "status": "partial",
            "platform": platform,
            "code": result.get("code") or f"{platform}_partial_send_failed",
            "error": str(result.get("error") or "unknown")[:200],
            **({"message_id": result.get("text_message_id")} if result.get("text_message_id") else {}),
            **({"media_message_id": result.get("media_message_id")} if result.get("media_message_id") else {}),
            **({"media_mode": result.get("media_mode")} if result.get("media_mode") else {}),
        }
    return {
        "status": "failed",
        "platform": platform,
        "code": result.get("code") or f"{platform}_send_failed",
        "error": str(result.get("error") or "unknown")[:200],
    }
