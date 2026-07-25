"""Gateway-flow orchestration: chat media ingestion, slash commands, Hermes hooks.

Split out of ``tools.py``. This half drives the conversational gateway flow —
QQ/Telegram attachment ingestion, the ``/cassette`` and ``/cut`` commands, model
and language pickers, pending-edit state — and is used by the Hermes gateway and
by the web demo, which synthesizes the same events. ``tools.py`` keeps the tool
handlers that every host calls directly.

The dependency is one-way: ``gateway`` imports ``tools``, never the reverse.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import mimetypes
import os
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import browser, exact_bgm, jobs, manifest, notifier, security, transport
from . import tools as tools_mod
from .errors import CassetteError
from .security import redact_for_log, safe_hash_id
from .tools import (
    _cassette_language_for_session,
    _cassette_model_options,
    _cassette_model_preference_for_session,
    _cassette_run_job_tool_chain_guard,
    _clear_pending_edit,
    _default_cassette_language_for_platform,
    _fixed_flow_busy_message,
    _freetouse_request_json,
    _gateway_asset_count,
    _instruction_with_bgm,
    _is_active_job,
    _latest_active_job_for_session,
    _normalize_cassette_language,
    _normalize_platform_name,
    _normalize_thinking_level,
    _pending_edit_path,
    _platform_value,
    _save_cassette_model_preference,
    _save_pending_edit,
    _save_session_preferences,
    cassette_cancel_job,
    cassette_job_status,
)

_FREETOUSE_CATEGORY_CACHE: dict[str, Any] = {"loaded_at": 0.0, "categories": []}


def _mime_to_media_type(mime: str, path: str) -> str:
    value = (mime or "").lower()
    if value.startswith("video/"):
        return "video"
    if value.startswith("image/"):
        return "image"
    if value.startswith("audio/"):
        return "audio"
    suffix = Path(path).suffix.lower()
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _AUDIO_EXTENSIONS:
        return "audio"
    return "file"


_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp"}


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def _language_label(language: str, *, display_language: str | None = None) -> str:
    language = _normalize_cassette_language(language) or "zh"
    display = _normalize_cassette_language(display_language) or language
    if display == "en":
        return "English" if language == "en" else "Chinese"
    return "英文" if language == "en" else "中文"


def _qq_attachment_media_type(att: dict[str, Any]) -> str:
    content_type = str(att.get("content_type") or "").split(";", 1)[0].strip().lower()
    filename = str(att.get("filename") or "").strip().lower()
    suffix = Path(filename).suffix.lower()
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("audio/"):
        return "audio"
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _AUDIO_EXTENSIONS:
        return "audio"
    return ""


def _is_qq_media_attachment(att: dict[str, Any]) -> bool:
    return bool(_qq_attachment_media_type(att))


def _qq_attachment_url(att: dict[str, Any]) -> str:
    raw = str(att.get("url") or "").strip()
    if raw.startswith("//"):
        return f"https:{raw}"
    return raw


def _allowed_qq_attachment_hosts() -> set[str]:
    raw = os.getenv(
        "CASSETTE_QQ_ATTACHMENT_HOSTS",
        "multimedia.nt.qq.com.cn,gchat.qpic.cn,c2cpicdw.qpic.cn,grouptalk.c2c.qq.com",
    )
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _validate_qq_attachment_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in _allowed_qq_attachment_hosts():
        raise CassetteError(
            "qq_attachment_url_rejected",
            "QQ attachment URL host is not allowed for Cassette ingestion",
        )


def _qq_media_headers(gateway: Any, event: Any) -> dict[str, str]:
    adapter = _gateway_adapter(gateway, getattr(event, "source", None), fallback_to_default=False)
    headers = {"User-Agent": "Hermes-Cassette/1.0"}
    header_getter = getattr(adapter, "_qq_media_headers", None)
    if callable(header_getter):
        try:
            headers.update({str(k): str(v) for k, v in (header_getter() or {}).items() if v})
            return headers
        except Exception:
            pass
    token = str(getattr(adapter, "_access_token", "") or os.getenv("QQ_ACCESS_TOKEN", "")).strip()
    if token:
        headers["Authorization"] = f"QQBot {token}"
    return headers


def _cache_gateway_document_bytes(data: bytes, filename: str) -> str:
    try:
        from gateway.platforms.base import cache_document_from_bytes

        return cache_document_from_bytes(data, filename)
    except Exception:
        import uuid

        home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
        cache_dir = home / "cache" / "documents"
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename).name or "video.mp4"
        path = cache_dir / f"doc_{uuid.uuid4().hex[:12]}_{safe_name}"
        path.write_bytes(data)
        return str(path)


def _qq_attachment_mime(att: dict[str, Any], media_type: str) -> str:
    content_type = str(att.get("content_type") or "").split(";", 1)[0].strip().lower()
    if content_type and content_type != "file":
        return content_type
    guessed = mimetypes.guess_type(str(att.get("filename") or ""))[0]
    if guessed:
        return guessed
    if media_type == "video":
        return "video/mp4"
    if media_type == "audio":
        return "audio/mpeg"
    if media_type == "image":
        return "image/jpeg"
    return "application/octet-stream"


def _qq_attachment_cache_name(att: dict[str, Any], media_type: str, mime: str) -> str:
    suffix = Path(str(att.get("filename") or "")).suffix.lower()
    allowed = {
        "video": _VIDEO_EXTENSIONS,
        "audio": _AUDIO_EXTENSIONS,
        "image": _IMAGE_EXTENSIONS,
    }.get(media_type, set())
    if suffix not in allowed:
        suffix = mimetypes.guess_extension(mime) or {
            "video": ".mp4",
            "audio": ".mp3",
            "image": ".jpg",
        }.get(media_type, ".bin")
    return f"{media_type}{suffix}"


def _download_qq_attachment_to_cache(att: dict[str, Any], gateway: Any, event: Any) -> tuple[str, str]:
    url = _qq_attachment_url(att)
    if not url:
        raise CassetteError("qq_attachment_missing_url", "QQ attachment URL is missing")
    _validate_qq_attachment_url(url)
    media_type = _qq_attachment_media_type(att)
    if not media_type:
        raise CassetteError("qq_attachment_unsupported_type", "QQ attachment is not supported Cassette media")
    timeout = float(os.getenv("CASSETTE_QQ_ATTACHMENT_TIMEOUT_SEC", "120"))
    request = Request(url, headers=_qq_media_headers(gateway, event))
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read(security.get_max_bytes() + 1)
    except Exception as exc:
        raise CassetteError(
            "qq_attachment_download_failed",
            "Failed to download QQ attachment for Cassette ingestion",
            {"reason": type(exc).__name__},
        ) from exc
    if len(data) > security.get_max_bytes():
        raise CassetteError("file_too_large", "QQ attachment is larger than CASSETTE_MAX_BYTES")
    content_type = _qq_attachment_mime(att, media_type)
    cache_name = _qq_attachment_cache_name(att, media_type, content_type)
    # Match the Weixin gateway contract: downloaded media enters Hermes document cache;
    # cassette owns the final asset layout and any H.264 normalization.
    return _cache_gateway_document_bytes(data, cache_name), content_type


def _qq_raw_gateway_media(event: Any, gateway: Any) -> tuple[list[str], list[str], list[str]]:
    raw = getattr(event, "raw_message", None)
    attachments = raw.get("attachments") if isinstance(raw, dict) else None
    if not isinstance(attachments, list):
        return [], [], []
    media_paths: list[str] = []
    media_types: list[str] = []
    failures: list[str] = []
    for att in attachments:
        if not isinstance(att, dict) or not _is_qq_media_attachment(att):
            continue
        try:
            path, mime = _download_qq_attachment_to_cache(att, gateway, event)
            media_paths.append(path)
            media_types.append(mime)
        except CassetteError as exc:
            failures.append(exc.code)
        except Exception:
            failures.append("qq_attachment_download_failed")
    return media_paths, media_types, failures


def _telegram_attachment_media_type(att: dict[str, Any]) -> str:
    content_type = (
        str(att.get("content_type") or att.get("mime_type") or att.get("mime") or "").split(";", 1)[0].strip().lower()
    )
    filename = str(att.get("filename") or att.get("file_name") or att.get("name") or "").strip().lower()
    suffix = Path(filename).suffix.lower()
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("audio/"):
        return "audio"
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _AUDIO_EXTENSIONS:
        return "audio"
    return ""


def _telegram_attachment_mime(att: dict[str, Any], media_type: str, path_hint: str = "") -> str:
    content_type = (
        str(att.get("content_type") or att.get("mime_type") or att.get("mime") or "").split(";", 1)[0].strip().lower()
    )
    if content_type and content_type != "file":
        return content_type
    guessed = mimetypes.guess_type(str(att.get("filename") or att.get("file_name") or path_hint or ""))[0]
    if guessed:
        return guessed
    if media_type == "video":
        return "video/mp4"
    if media_type == "audio":
        return "audio/mpeg"
    if media_type == "image":
        return "image/jpeg"
    return "application/octet-stream"


def _telegram_local_path(att: dict[str, Any]) -> str:
    for key in ("local_path", "path", "cached_path", "file_path"):
        value = str(att.get(key) or "").strip()
        if value and not value.startswith(("http://", "https://")) and Path(value).expanduser().exists():
            return value
    return ""


def _telegram_attachment_url(att: dict[str, Any]) -> str:
    for key in ("url", "download_url", "file_url", "file_path"):
        value = str(att.get(key) or "").strip()
        if value.startswith("//"):
            return f"https:{value}"
        if value.startswith("https://"):
            return value
    return ""


def _allowed_telegram_attachment_hosts() -> set[str]:
    raw = os.getenv("CASSETTE_TELEGRAM_ATTACHMENT_HOSTS", "api.telegram.org,cdn.telegram.org")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _validate_telegram_attachment_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in _allowed_telegram_attachment_hosts():
        raise CassetteError(
            "telegram_attachment_url_rejected",
            "Telegram attachment URL host is not allowed for Cassette ingestion",
        )


def _telegram_attachment_cache_name(att: dict[str, Any], media_type: str, mime: str) -> str:
    suffix = Path(str(att.get("filename") or att.get("file_name") or "")).suffix.lower()
    allowed = {
        "video": _VIDEO_EXTENSIONS,
        "audio": _AUDIO_EXTENSIONS,
        "image": _IMAGE_EXTENSIONS,
    }.get(media_type, set())
    if suffix not in allowed:
        suffix = mimetypes.guess_extension(mime) or {
            "video": ".mp4",
            "audio": ".mp3",
            "image": ".jpg",
        }.get(media_type, ".bin")
    return f"telegram-{media_type}{suffix}"


def _download_telegram_attachment_to_cache(att: dict[str, Any]) -> tuple[str, str]:
    url = _telegram_attachment_url(att)
    if not url:
        raise CassetteError("telegram_attachment_missing_url", "Telegram attachment URL is missing")
    _validate_telegram_attachment_url(url)
    media_type = _telegram_attachment_media_type(att)
    if not media_type:
        raise CassetteError(
            "telegram_attachment_unsupported_type", "Telegram attachment is not supported Cassette media"
        )
    timeout = float(os.getenv("CASSETTE_TELEGRAM_ATTACHMENT_TIMEOUT_SEC", "120"))
    request = Request(url, headers={"User-Agent": "Hermes-Cassette/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read(security.get_max_bytes() + 1)
    except Exception as exc:
        raise CassetteError(
            "telegram_attachment_download_failed",
            "Failed to download Telegram attachment for Cassette ingestion",
            {"reason": type(exc).__name__},
        ) from exc
    if len(data) > security.get_max_bytes():
        raise CassetteError("file_too_large", "Telegram attachment is larger than CASSETTE_MAX_BYTES")
    content_type = _telegram_attachment_mime(att, media_type, url)
    return _cache_gateway_document_bytes(
        data, _telegram_attachment_cache_name(att, media_type, content_type)
    ), content_type


def _iter_telegram_attachment_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if _telegram_attachment_media_type(value) and (_telegram_local_path(value) or _telegram_attachment_url(value)):
            found.append(value)
        for key in (
            "attachments",
            "media",
            "photos",
            "photo",
            "videos",
            "video",
            "audio",
            "voice",
            "documents",
            "document",
        ):
            child = value.get(key)
            if child is not None and child is not value:
                found.extend(_iter_telegram_attachment_dicts(child))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iter_telegram_attachment_dicts(item))
    return found


def _telegram_raw_gateway_media(event: Any, gateway: Any) -> tuple[list[str], list[str], list[str]]:
    del gateway
    raw = getattr(event, "raw_message", None)
    attachments = _iter_telegram_attachment_dicts(raw)
    if not attachments:
        return [], [], []
    media_paths: list[str] = []
    media_types: list[str] = []
    failures: list[str] = []
    seen: set[str] = set()
    for att in attachments:
        try:
            local_path = _telegram_local_path(att)
            media_type = _telegram_attachment_media_type(att)
            if local_path:
                key = str(Path(local_path).expanduser())
                if key in seen:
                    continue
                seen.add(key)
                media_paths.append(local_path)
                media_types.append(_telegram_attachment_mime(att, media_type, local_path))
                continue
            path, mime = _download_telegram_attachment_to_cache(att)
            if path in seen:
                continue
            seen.add(path)
            media_paths.append(path)
            media_types.append(mime)
        except CassetteError as exc:
            failures.append(exc.code)
        except Exception:
            failures.append("telegram_attachment_download_failed")
    return media_paths, media_types, failures


def _is_gateway_media_placeholder_text(text: str) -> bool:
    normalized = (text or "").strip()
    if not normalized:
        return True
    if normalized.startswith("[Attachment:") and normalized.endswith("]"):
        return True
    if normalized.startswith("[Voice]"):
        return True
    value = normalized.casefold().strip()
    bracketed = value.strip("[]【】()（） ")
    media_words = {
        "视频",
        "图片",
        "照片",
        "语音",
        "音频",
        "文件",
        "附件",
        "video",
        "image",
        "photo",
        "voice",
        "audio",
        "file",
        "attachment",
    }
    if bracketed in media_words:
        return True
    if re.fullmatch(r"\[cq:(video|image|record|voice|file|audio)\b[^\]]*\]", value):
        return True
    if re.fullmatch(r"\[(video|image|photo|voice|audio|file|attachment)\b[^\]]*\]", value):
        return True
    if re.fullmatch(r"[【\[]?(视频|图片|照片|语音|音频|文件|附件)[】\]]?\s+.+", normalized):
        tail = re.sub(r"^[【\[]?(视频|图片|照片|语音|音频|文件|附件)[】\]]?\s+", "", normalized).strip()
        if Path(tail).suffix.lower() in (_VIDEO_EXTENSIONS | _IMAGE_EXTENSIONS | _AUDIO_EXTENSIONS):
            return True
    if (
        len(normalized) <= 260
        and "\n" not in normalized
        and Path(normalized).suffix.lower() in (_VIDEO_EXTENSIONS | _IMAGE_EXTENSIONS | _AUDIO_EXTENSIONS)
    ):
        return True
    return False


def _gateway_hermes_session_id(event: Any, session_store: Any = None) -> str:
    if session_store is None:
        return ""
    getter = getattr(session_store, "get_or_create_session", None)
    if not callable(getter):
        return ""
    try:
        entry = getter(getattr(event, "source", None))
    except Exception:
        return ""
    if isinstance(entry, dict):
        return str(entry.get("session_id") or "")
    return str(getattr(entry, "session_id", "") or "")


def _gateway_session_id(event: Any, session_store: Any = None) -> str:
    source = getattr(event, "source", None)
    platform = (
        getattr(getattr(source, "platform", None), "value", None) or getattr(source, "platform", None) or "gateway"
    )
    if _normalize_platform_name(platform) == "web":
        chat_id = str(getattr(source, "chat_id", "") or "").strip()
        if chat_id.startswith("web_"):
            return chat_id
    chat_hash = safe_hash_id(getattr(source, "chat_id", None))
    hermes_session_id = _gateway_hermes_session_id(event, session_store)
    if hermes_session_id:
        return f"gateway_media_{platform}_{chat_hash}_{safe_hash_id(hermes_session_id)}"
    return f"gateway_media_{platform}_{chat_hash}"


def _is_gateway_authorized(gateway: Any, event: Any) -> bool:
    source = getattr(event, "source", None)
    if source is None:
        return False
    checker = getattr(gateway, "_is_user_authorized", None)
    if not callable(checker):
        return True
    try:
        return bool(checker(source))
    except Exception:
        return False


def _gateway_assets(session_id: str) -> list[dict]:
    try:
        data = manifest.list_assets(session_id=session_id)
    except Exception:
        return []
    assets = data.get("manifest", {}).get("assets", [])
    return [asset for asset in assets if asset.get("exists", True)]


def _gateway_adapter(gateway: Any, source: Any, *, fallback_to_default: bool = True) -> Any:
    adapters = getattr(gateway, "adapters", None) if gateway is not None else None
    if isinstance(adapters, dict) and source is not None:
        platform = getattr(source, "platform", None)
        try:
            adapter = adapters.get(platform)
        except TypeError:
            adapter = None
        if adapter is not None:
            return adapter
        platform_value = _platform_value(source)
        for key, candidate in adapters.items():
            key_value = str(getattr(key, "value", key))
            candidate_value = str(
                getattr(getattr(candidate, "platform", None), "value", getattr(candidate, "platform", ""))
            )
            if platform_value in {key_value, candidate_value}:
                return candidate
    return getattr(gateway, "adapter", None) if fallback_to_default else None


def _send_gateway_fixed_reply(gateway: Any, event: Any, text: str) -> bool:
    source = getattr(event, "source", None)
    chat_id = getattr(source, "chat_id", None)
    if gateway is None or source is None or not chat_id:
        return False
    adapter = _gateway_adapter(gateway, source)
    sender = getattr(adapter, "send", None)
    if not callable(sender):
        return False
    try:
        thread_id = getattr(source, "thread_id", None)
        if thread_id is not None:
            try:
                result = sender(str(chat_id), text, metadata={"thread_id": str(thread_id)})
            except TypeError:
                result = sender(str(chat_id), text)
        else:
            result = sender(str(chat_id), text)
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
            else:
                loop.create_task(result)
        return True
    except Exception:
        return False


def _fixed_media_saved_message(
    saved_count: int, total_count: int, failures: list[str] | None = None, language: str = "zh"
) -> str:
    language = _normalize_cassette_language(language) or "zh"
    if language == "en":
        saved_word = "asset" if saved_count == 1 else "assets"
        total_word = "asset" if total_count == 1 else "assets"
        if total_count > saved_count:
            message = f"Saved {saved_count} new {saved_word}; this session now has {total_count} {total_word}. Send more media, or send an edit instruction and I will hand it to Cassette."
        else:
            message = f"Saved {saved_count} {saved_word}. Send more media, or send an edit instruction and I will hand it to Cassette."
        if failures:
            message += f" Some assets failed to save. Error code(s): {', '.join(sorted(set(failures)))}."
        return message
    if total_count > saved_count:
        message = f"已保存本次素材 {saved_count} 个，当前会话共 {total_count} 个素材。请继续发送素材，或发送剪辑指令后我会交给 Cassette 处理。"
    else:
        message = f"已保存素材 {saved_count} 个。请继续发送素材，或发送剪辑指令后我会交给 Cassette 处理。"
    if failures:
        message += f" 有部分素材保存失败，错误码：{', '.join(sorted(set(failures)))}。"
    return message


def _fixed_media_failed_message(failures: list[str], language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return f"Failed to save media. Error code(s): {', '.join(sorted(set(failures)))}. Please resend the media or check gateway media configuration."
    return f"素材保存失败，错误码：{', '.join(sorted(set(failures)))}。请重新发送素材或检查网关媒体配置。"


def _fixed_edit_command_missing_assets_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return "No usable assets yet. Send video, image, or audio assets first, then use /edit with an edit instruction to start Cassette."
    return "还没有可用素材。请先发送视频、图片或音频素材，再用 /edit 加剪辑指令触发 Cassette。"


def _fixed_edit_command_missing_instruction_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return "Please write the edit instruction after /edit, for example: /edit cut this into a 10-second short video with captions."
    return "请在 /edit 后面写剪辑指令，例如：/edit 剪成 10 秒短视频，加中文字幕。"


def _fixed_refine_command_missing_assets_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return "No usable assets yet. Send video, image, or audio assets first, then use /refine with an edit instruction to trigger prompt optimization."
    return "还没有可用素材。请先发送视频、图片或音频素材，再用 /refine 加剪辑指令触发 prompt 优化。"


def _fixed_refine_command_missing_instruction_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return "Please write the edit instruction after /refine, for example: /refine cut this into a 10-second short video with captions."
    return "请在 /refine 后面写需要优化的剪辑指令，例如：/refine 剪成 10 秒短视频，加中文字幕。"


def _fixed_music_command_missing_instruction_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return "Please write the BGM matching requirement after /music, for example: /music light travel feeling, suitable for drone footage."
    return "请在 /music 后面写 BGM 匹配需求，例如：/music 旅行感、轻快、适合航拍。"


def _cassette_unreachable_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return "Cannot connect to Cassette. Please check your network settings."
    return "无法连接 Cassette，请检查网络设置。"


def _fixed_cut_requested_message(active: bool, language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        if active:
            return "Requested a stop for the current Cassette operation. If the Cassette agent is still running, I will click the page stop button; the browser state will be preserved for retry or the next edit instruction."
        return "Cassette has no active edit operation right now. The browser state is preserved; send a retry or the next edit instruction to continue."
    if active:
        return "已请求停止当前 Cassette 操作。若 Cassette agent 仍在执行，我会触发页面停止按钮；浏览器状态会保留，等待你发送重试或下一步剪辑指令。"
    return "Cassette 当前没有正在运行的剪辑任务，已保持浏览器状态暂停。你可以发送重试或下一步剪辑指令继续。"


def _reject_busy_flow(
    session_id: str, gateway: Any, event: Any, language: str = "zh", reason: str = "cassette_flow_busy"
) -> dict:
    reply_sent = _send_gateway_fixed_reply(gateway, event, _fixed_flow_busy_message(language))
    return {
        "action": "skip",
        "reason": reason,
        "session_id": session_id,
        "reply_sent": reply_sent,
    }


def _latest_active_job(limit: int = 50) -> dict | None:
    try:
        recent = jobs.list_jobs(limit=limit)
    except Exception:
        return None
    for job in recent:
        if _is_active_job(job):
            return job
    return None


def _handle_gateway_cut_command(session_id: str, gateway: Any, event: Any, language: str = "zh") -> dict:
    pending_edit = _load_pending_edit(session_id)
    active_job = _latest_active_job_for_session(session_id, getattr(event, "source", None))
    if pending_edit and not active_job:
        _clear_pending_edit(session_id)
        reply_sent = _send_gateway_fixed_reply(gateway, event, _fixed_cut_requested_message(True, language))
        return {
            "action": "skip",
            "reason": "cassette_cut_requested",
            "session_id": session_id,
            "pending_state": str(pending_edit.get("state") or ""),
            "reply_sent": reply_sent,
        }
    if active_job:
        if pending_edit:
            _clear_pending_edit(session_id)
        job_id = str(active_job.get("job_id") or "")
        if job_id:
            try:
                jobs.request_cancel(job_id)
            except Exception:
                pass
        reply_sent = _send_gateway_fixed_reply(gateway, event, _fixed_cut_requested_message(True, language))
        return {
            "action": "skip",
            "reason": "cassette_cut_requested",
            "session_id": session_id,
            "job_id": job_id,
            "current_stage": active_job.get("current_stage") or "",
            "reply_sent": reply_sent,
        }
    reply_sent = _send_gateway_fixed_reply(gateway, event, _fixed_cut_requested_message(False, language))
    return {
        "action": "skip",
        "reason": "cassette_cut_no_active_job",
        "session_id": session_id,
        "reply_sent": reply_sent,
    }


def _cassette_connectivity_skip(gateway: Any, event: Any, language: str = "zh") -> dict | None:
    ping_setting = str(
        os.getenv("CASSETTE_PING_ON_GATEWAY_INSTRUCTION", "")
        or notifier._runtime_env("CASSETTE_PING_ON_GATEWAY_INSTRUCTION")
    ).lower()
    if ping_setting in {"0", "false", "no", "off"}:
        return None
    result = browser.check_cassette_connectivity()
    if result.get("ok"):
        return None
    reply_sent = _send_gateway_fixed_reply(gateway, event, _cassette_unreachable_message(language))
    return {
        "action": "skip",
        "reason": "cassette_unreachable",
        "error_code": str(result.get("code") or "cassette_unreachable"),
        "reply_sent": reply_sent,
    }


def _looks_like_asset_status_query(text: str) -> bool:
    parsed = _gateway_slash_command(text)
    return bool(parsed and parsed[0] == "check_assets")


def _asset_display_name(asset: dict) -> str:
    caption = str(asset.get("caption") or "").strip()
    if caption.startswith("[Attachment:") and caption.endswith("]"):
        return caption.removeprefix("[Attachment:").removesuffix("]").strip()
    if caption.startswith("[Voice]"):
        return "voice/audio"
    return str(asset.get("original_name") or asset.get("asset_id") or "asset").strip()


def _fixed_asset_status_message(session_id: str, language: str = "zh") -> str:
    assets = _gateway_assets(session_id)
    counts: dict[str, int] = {}
    for asset in assets:
        media_type = str(asset.get("media_type") or "file")
        counts[media_type] = counts.get(media_type, 0) + 1
    if _normalize_cassette_language(language) == "en":
        label_map = (
            ("video", "video"),
            ("image", "image"),
            ("audio", "audio"),
            ("file", "file"),
            ("unknown", "unknown"),
        )
        type_parts = [f"{label} {counts[key]}" for key, label in label_map if counts.get(key)]
        summary = ", ".join(type_parts) if type_parts else "no usable assets"
        names = [_asset_display_name(asset) for asset in assets[:12]]
        if len(assets) > 12:
            names.append(f"...and {len(assets) - 12} more")
        detail = "; ".join(names)
        message = f"This Cassette session has saved {len(assets)} asset(s) ({summary})."
        if detail:
            message += f" Saved: {detail}."
        message += (
            " If you expected more, resend the missing media or wait for the upload to finish before checking again."
        )
        return message
    type_parts = []
    for key, label in (("video", "视频"), ("image", "图片"), ("audio", "音频"), ("file", "文件"), ("unknown", "未知")):
        if counts.get(key):
            type_parts.append(f"{label} {counts[key]} 个")
    summary = "，".join(type_parts) if type_parts else "暂无可用素材"
    names = [_asset_display_name(asset) for asset in assets[:12]]
    if len(assets) > 12:
        names.append(f"...另有 {len(assets) - 12} 个")
    detail = "；".join(names)
    message = f"当前 Cassette 会话已保存素材 {len(assets)} 个（{summary}）。"
    if detail:
        message += f" 已保存：{detail}。"
    message += " 如果你预期数量更多，请重新发送缺失素材，或稍等上传完成再检查。"
    return message


def _obviously_not_edit_instruction(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return True
    delivery_complaint_terms = {
        "didn't receive",
        "did not receive",
        "haven't received",
        "have not received",
        "not received",
        "not receive",
        "没收到",
        "没有收到",
        "未收到",
        "没接收到",
    }
    if any(term in normalized for term in delivery_complaint_terms):
        return True
    non_edit_exact_terms = {
        "你好",
        "hello",
        "hi",
        "嗨",
        "在吗",
        "谢谢",
        "thanks",
        "thank you",
        "thx",
        "好的",
        "好吧",
        "ok",
        "okay",
        "嗯",
        "收到",
    }
    return normalized in non_edit_exact_terms or _looks_like_asset_status_query(normalized)


_CASSETTE_GATEWAY_COMMANDS = {"edit", "refine", "music", "cut", "check_assets", "cassette_model"}


def _gateway_slash_command(text: str) -> tuple[str, str] | None:
    match = re.match(
        r"^\s*[／/]([A-Za-z][A-Za-z0-9_-]*)(?:@[A-Za-z0-9_]+)?(?:\s+|[:：]\s*|$)(.*)\s*$", text or "", flags=re.DOTALL
    )
    if not match:
        return None
    return match.group(1).lower(), match.group(2).strip()


def _is_reserved_gateway_slash_command(text: str) -> bool:
    parsed = _gateway_slash_command(text)
    if parsed and parsed[0] == "cassette":
        args = parsed[1].split(None, 1)
        return not (args and args[0].lower() == "cut")
    return bool(parsed and parsed[0] not in _CASSETTE_GATEWAY_COMMANDS)


def _forced_command_instruction(text: str, command: str) -> str | None:
    parsed = _gateway_slash_command(text)
    if not parsed or parsed[0] != command:
        return None
    return parsed[1]


def _forced_edit_instruction(text: str) -> str | None:
    return _forced_command_instruction(text, "edit")


def _forced_refine_instruction(text: str) -> str | None:
    return _forced_command_instruction(text, "refine")


def _forced_music_instruction(text: str) -> str | None:
    return _forced_command_instruction(text, "music")


def _forced_check_assets_command(text: str) -> bool:
    parsed = _gateway_slash_command(text)
    return bool(parsed and parsed[0] == "check_assets")


def _forced_cassette_model_command(text: str) -> str | None:
    parsed = _gateway_slash_command(text)
    if not parsed or parsed[0] != "cassette_model":
        return None
    return parsed[1]


def _forced_cut_instruction(text: str) -> str | None:
    parsed = _gateway_slash_command(text)
    if not parsed:
        return None
    if parsed[0] == "cut":
        return parsed[1]
    if parsed[0] == "cassette":
        parts = parsed[1].split(None, 1)
        if parts and parts[0].lower() == "cut":
            return parts[1].strip() if len(parts) > 1 else ""
    return None


def _forced_cassette_language_command(text: str) -> str | None:
    parsed = _gateway_slash_command(text)
    if not parsed or parsed[0] != "cassette":
        return None
    parts = parsed[1].split(None, 1)
    if not parts or parts[0].lower() not in {"language", "lang", "语言"}:
        return None
    return parts[1].strip() if len(parts) > 1 else ""


def _gateway_language_status_message(language: str, platform: Any = None) -> str:
    display = _normalize_cassette_language(language) or _default_cassette_language_for_platform(platform)
    if display == "en":
        return f"Cassette language is currently {_language_label(display, display_language='en')}. Use /cassette language zh or /cassette language en to change it."
    return f"当前 Cassette 语言是{_language_label(display, display_language='zh')}。可用 /cassette language zh 或 /cassette language en 修改。"


def _gateway_language_set_message(language: str) -> str:
    if _normalize_cassette_language(language) == "en":
        return "Cassette language set to English for this gateway session. Telegram defaults to English; QQ defaults to Chinese."
    return "已将当前 gateway 会话的 Cassette 语言设置为中文。QQ 默认中文，Telegram 默认英文。"


def _gateway_language_invalid_message(language: str) -> str:
    if _normalize_cassette_language(language) == "en":
        return "Unsupported Cassette language. Use /cassette language zh or /cassette language en."
    return "不支持的 Cassette 语言。请使用 /cassette language zh 或 /cassette language en。"


def _handle_gateway_language_command(session_id: str, platform: Any, gateway: Any, event: Any, arg: str) -> dict:
    current_language = _cassette_language_for_session(session_id, platform)
    requested = (arg or "").strip()
    if not requested:
        reply_sent = _send_gateway_fixed_reply(
            gateway, event, _gateway_language_status_message(current_language, platform)
        )
        return {
            "action": "skip",
            "reason": "cassette_language_status",
            "session_id": session_id,
            "cassette_language": current_language,
            "reply_sent": reply_sent,
        }
    normalized = _normalize_cassette_language(requested)
    if not normalized:
        reply_sent = _send_gateway_fixed_reply(gateway, event, _gateway_language_invalid_message(current_language))
        return {
            "action": "skip",
            "reason": "cassette_language_invalid",
            "session_id": session_id,
            "cassette_language": current_language,
            "reply_sent": reply_sent,
        }
    _save_cassette_language_preference(session_id, normalized, platform)
    reply_sent = _send_gateway_fixed_reply(gateway, event, _gateway_language_set_message(normalized))
    return {
        "action": "skip",
        "reason": "cassette_language_set",
        "session_id": session_id,
        "cassette_language": normalized,
        "reply_sent": reply_sent,
    }


def _numbered_choice(text: str, max_value: int) -> int | None:
    normalized = (text or "").strip().lower().strip("。.!！ ")
    if not normalized or normalized.startswith("/"):
        return None
    mapping = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if normalized in mapping:
        value = mapping[normalized]
        return value if 1 <= value <= max_value else None
    match = re.fullmatch(r"(?:选|选择)?\s*(\d{1,2})\s*(?:号|项|个)?", normalized)
    if not match:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= max_value else None


def _cassette_model_choice_message(options: dict[str, Any], language: str = "zh") -> str:
    models = list(options.get("models") or [])
    if _normalize_cassette_language(language) == "en":
        lines = ["Choose the Cassette model for this session. Reply with the number:"]
        lines.extend(f"{index}. {model.get('label')}" for index, model in enumerate(models, start=1))
        lines.append("You can run /cassette_model again later to change it.")
        return "\n".join(lines)
    lines = ["请选择当前 Cassette 会话使用的模型，回复序号即可："]
    lines.extend(f"{index}. {model.get('label')}" for index, model in enumerate(models, start=1))
    lines.append("后续同一 session 不会重复询问；需要修改时发送 /cassette_model。")
    return "\n".join(lines)


def _cassette_thinking_choice_message(model: str, options: dict[str, Any], language: str = "zh") -> str:
    thinking = list(options.get("thinking_levels") or [])
    if _normalize_cassette_language(language) == "en":
        lines = [f"Model selected: {model}. Choose the thinking level, reply with the number:"]
        lines.extend(f"{index}. {item.get('label')}" for index, item in enumerate(thinking, start=1))
        return "\n".join(lines)
    lines = [f"已选择模型：{model}。请选择思考程度，回复序号即可："]
    lines.extend(f"{index}. {item.get('label')}" for index, item in enumerate(thinking, start=1))
    return "\n".join(lines)


def _cassette_model_set_message(model: str, thinking: str, language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return f"Cassette model set for this session: {model} · {thinking}."
    return f"已设置当前 Cassette 会话模型：{model} · 思考程度 {thinking}。"


def _request_cassette_model_choice(
    session_id: str,
    instruction: str,
    asset_count: int,
    gateway: Any,
    event: Any,
    *,
    language: str = "zh",
    resume_after_model: str = "command",
) -> dict:
    options = _cassette_model_options(language)
    pending_extra: dict[str, Any] = {
        "model_options": options.get("models") or [],
        "thinking_options": options.get("thinking_levels") or [],
        "model_options_source": options.get("source") or "",
        "language": _normalize_cassette_language(language) or "zh",
        "resume_after_model": resume_after_model,
    }
    _save_pending_edit(session_id, instruction, asset_count, "awaiting_model_choice", **pending_extra)
    reply_sent = _send_gateway_fixed_reply(gateway, event, _cassette_model_choice_message(options, language))
    return {
        "action": "skip",
        "reason": "cassette_model_choice_requested",
        "asset_count": asset_count,
        "session_id": session_id,
        "reply_sent": reply_sent,
        "model_options_source": options.get("source") or "",
    }


def _handle_pending_cassette_model_choice(
    session_id: str,
    pending_edit: dict,
    user_text: str,
    gateway: Any,
    event: Any,
    *,
    language: str = "zh",
) -> dict | None:
    state = str(pending_edit.get("state") or "")
    if state == "awaiting_model_choice":
        models = list(pending_edit.get("model_options") or [])
        choice = _numbered_choice(user_text, len(models))
        if choice is None:
            return _reject_busy_flow(session_id, gateway, event, language, "cassette_model_choice_busy_rejected")
        selected_model = str((models[choice - 1] or {}).get("label") or "").strip()
        _save_pending_edit(
            session_id,
            str(pending_edit.get("instruction") or ""),
            int(pending_edit.get("asset_count") or 0),
            "awaiting_model_thinking_choice",
            model_options=models,
            thinking_options=list(pending_edit.get("thinking_options") or []),
            selected_model=selected_model,
            resume_after_model=pending_edit.get("resume_after_model") or "edit",
            optimization_enabled=bool(pending_edit.get("optimization_enabled")),
            continue_after_match=bool(pending_edit.get("continue_after_match", True)),
            semantic_gate=bool(pending_edit.get("semantic_gate")),
            language=_normalize_cassette_language(language) or "zh",
        )
        reply_sent = _send_gateway_fixed_reply(
            gateway,
            event,
            _cassette_thinking_choice_message(
                selected_model,
                {"thinking_levels": list(pending_edit.get("thinking_options") or [])},
                language,
            ),
        )
        return {
            "action": "skip",
            "reason": "cassette_model_thinking_choice_requested",
            "asset_count": int(pending_edit.get("asset_count") or 0),
            "session_id": session_id,
            "reply_sent": reply_sent,
        }
    if state != "awaiting_model_thinking_choice":
        return None
    thinking_options = list(pending_edit.get("thinking_options") or [])
    choice = _numbered_choice(user_text, len(thinking_options))
    selected_model = str(pending_edit.get("selected_model") or "").strip()
    if choice is None:
        return _reject_busy_flow(session_id, gateway, event, language, "cassette_model_thinking_choice_busy_rejected")
    selected_thinking = str(
        (thinking_options[choice - 1] or {}).get("value") or (thinking_options[choice - 1] or {}).get("label") or ""
    ).strip()
    selected_thinking = _normalize_thinking_level(selected_thinking)
    resume_after_model = str(pending_edit.get("resume_after_model") or "command")
    _save_cassette_model_preference(
        session_id,
        selected_model,
        selected_thinking,
        source="gateway_command",
    )
    _clear_pending_edit(session_id)
    instruction = str(pending_edit.get("instruction") or "").strip()
    asset_count = int(pending_edit.get("asset_count") or 0)
    if resume_after_model == "refine" and instruction:
        return _rewrite_accepted_prompt_optimization(session_id, instruction, asset_count, language=language)
    if resume_after_model not in {"command", ""} and instruction:
        # Stale pre-0.4.1 funnel resume: route the pending instruction straight to the direct lane.
        return _rewrite_direct_original_instruction(
            session_id, instruction, asset_count, direct_reason="session_default", language=language
        )
    reply_sent = _send_gateway_fixed_reply(
        gateway, event, _cassette_model_set_message(selected_model, selected_thinking, language)
    )
    return {
        "action": "skip",
        "reason": "cassette_model_set",
        "session_id": session_id,
        "model_selection": {"model": selected_model, "thinking_level": selected_thinking},
        "reply_sent": reply_sent,
    }


def _looks_like_prompt_confirmation(text: str) -> bool:
    normalized = (text or "").strip().lower()
    normalized = normalized.strip("。.!！ ")
    confirmations = {
        "确认",
        "可以",
        "好的",
        "好",
        "行",
        "没问题",
        "就这样",
        "按这个来",
        "按这个做",
        "开始",
        "开始吧",
        "执行",
        "执行吧",
        "继续",
        "继续吧",
        "ok",
        "okay",
        "yes",
        "confirm",
        "approved",
        "go",
        "go ahead",
    }
    if normalized in confirmations:
        return True
    return any(
        term in normalized
        for term in ("确认执行", "确认开始", "按优化", "用这个方案", "按这个方案", "开始剪", "可以开始")
    )


def _looks_like_prompt_optimization_decline(text: str) -> bool:
    normalized = (text or "").strip().lower()
    normalized = normalized.strip("。.!！ ")
    if not normalized:
        return False
    decline_terms = (
        "否",
        "不用",
        "不要",
        "不需要",
        "不优化",
        "别优化",
        "跳过优化",
        "原样",
        "直接开始",
        "直接剪",
        "按原文",
        "按原始",
        "原指令",
        "原始指令",
        "无需优化",
        "no",
        "n",
        "skip",
        "skip optimization",
        "without optimization",
        "do not optimize",
        "don't optimize",
        "raw",
        "use original",
    )
    return any(term in normalized for term in decline_terms)


def _load_pending_edit(session_id: str) -> dict | None:
    path = _pending_edit_path(session_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    state = str(data.get("state") or "")
    if state in {"awaiting_optimization_choice", "awaiting_bgm_choice"}:
        # Pre-0.4.1 funnel states: the upfront asks are gone — clear so the next message
        # routes straight to the verbatim direct lane.
        _clear_pending_edit(session_id)
        return None
    if not str(data.get("instruction") or "").strip() and state not in {
        "awaiting_model_choice",
        "awaiting_model_thinking_choice",
    }:
        return None
    return data


def _save_cassette_language_preference(session_id: str, language: str, platform: Any = None) -> dict:
    normalized = _normalize_cassette_language(language)
    if not normalized:
        raise CassetteError("invalid_cassette_language", "Cassette language must be zh or en")
    return _save_session_preferences(
        session_id,
        cassette_language=normalized,
        cassette_language_source="gateway_command",
        cassette_language_default=_default_cassette_language_for_platform(platform),
    )


def _language_name_for_prompt(language: str) -> str:
    return "English" if _normalize_cassette_language(language) == "en" else "Chinese"


def _cassette_orchestration_guard() -> str:
    return (
        "Hermes must act only as a Cassette orchestration supervisor for this edit: "
        "do not inspect, describe, understand, extract frames from, probe, transcode for analysis, or otherwise analyze the media yourself; "
        "do not use terminal, ffprobe, ffmpeg, vision_analyze, local scripts, or non-Cassette tools to decide creative content. "
        "Relay the user's editing intent to Cassette and let Cassette analyze the uploaded media, choose matching visual treatment, captions, poems, music, and timing."
    )


def _default_prompt_optimization_guard() -> str:
    return (
        "Before starting Cassette, act as a professional editing brief optimizer. "
        "Do not call cassette_list_assets, cassette_make_prompt, or cassette_run_job yet. "
        "Rewrite the user's editing intent into a concise, production-ready Chinese editing brief for Cassette, while preserving every explicit user requirement exactly: do not change specified product, theme, wording, duration, aspect ratio, captions, music, style, ordering, exclusions, or constraints. "
        "Only add detail for unspecified aspects, choosing defaults that best support the user's stated intent: pacing, structure, shot usage, caption hierarchy, safe layout, transitions, color, sound, export goal, and quality checks. "
        "Do not inspect/analyze local media yourself and do not claim to know visual content not provided by the user. "
        "Send the optimized brief to the user for confirmation and ask them to reply '确认' to start Cassette, or send modifications. "
        "Do not start a Cassette job until the user confirms the optimized brief."
    )


def _prompt_optimization_doc_path() -> Path:
    override = os.getenv("CASSETTE_PROMPT_OPTIMIZER_DOC")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / "prompts" / "hermes-edit-brief-optimizer.md"


def _prompt_optimization_guard() -> str:
    try:
        doc = _prompt_optimization_doc_path().read_text(encoding="utf-8").strip()
    except OSError:
        return _default_prompt_optimization_guard()
    return doc or _default_prompt_optimization_guard()


def _confirmed_prompt_guard(language: str = "zh") -> str:
    return (
        "The user has confirmed the optimized editing brief you proposed in the immediately preceding conversation. "
        "Use that confirmed optimized brief as the message for cassette_run_job. "
        f"{_cassette_run_job_tool_chain_guard(language)} "
        "Do not use the short confirmation word itself as the editing instruction."
    )


def _direct_original_instruction_guard(reason: str = "declined", language: str = "zh") -> str:
    if reason == "session_default":
        return (
            "Use the user's edit instruction above exactly as the message for cassette_run_job; do not ask "
            "about prompt optimization, smart BGM, or model choice first — /refine, /music, and "
            "/cassette_model exist for users who want those. "
            f"{_cassette_run_job_tool_chain_guard(language)} "
            "Do not use any previous short denial/confirmation word as the editing instruction."
        )
    return (
        "The user declined prompt optimization. "
        "Use the original edit instruction above exactly as the message for cassette_run_job; if the plugin appended a smart-BGM requirement to that instruction, preserve that requirement too; do not optimize, rewrite, summarize, or reinterpret it first. "
        f"{_cassette_run_job_tool_chain_guard(language)} "
        "Do not use the short denial/confirmation word itself as the editing instruction."
    )


def _accepted_prompt_optimization_guard(language: str = "zh") -> str:
    return (
        "The user chose to use prompt optimization. "
        "Use the original edit instruction above as the source text to optimize. "
        f"{_prompt_optimization_guard()} "
        f"Write the optimized brief and confirmation question in {_language_name_for_prompt(language)}."
    )


def _rewrite_semantic_edit_instruction_judgment(
    session_id: str,
    instruction: str,
    asset_count: int,
    language: str = "zh",
) -> dict:
    text = (
        f"{instruction}\n\n"
        f"[Cassette gateway assets available: {asset_count} asset(s). "
        f"Use cassette session_id `{session_id}` only if this is an edit instruction. "
        "Hermes must semantically decide whether the user's message is a request to edit, transform, assemble, caption, style, add/remove/replace audio or visuals, export, or otherwise operate on the saved media. "
        "Do not rely on keyword matching. Vague follow-ups may be edit instructions when they naturally refer to the saved media or to the ongoing edit conversation. "
        "If this is not an edit instruction, answer the user normally and do not call Cassette tools. "
        f"If this is an edit instruction, {_direct_original_instruction_guard('session_default', language)} "
        f"{_cassette_orchestration_guard()} "
        "Do not ask the user to resend the already saved media.]"
    )
    return {"action": "rewrite", "text": text}


def _exact_bgm_enabled() -> bool:
    try:
        return exact_bgm.exact_bgm_enabled()
    except Exception:
        return False


def _exact_bgm_selection_reask_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return "Please reply with 1, 2, 3, 4, or 5: 1-3 select a song, 4 asks for another batch, 5 uses random matching. You can also send text to add BGM requirements and I will recommend a new batch."
    return "请回复 1、2、3、4 或 5：1-3 选择对应歌曲，4 换一批，5 随机匹配。也可以直接发送文字补充 BGM 需求，我会重新推荐一批。"


def _parse_exact_bgm_choice(text: str) -> int | None:
    normalized = (text or "").strip().lower().strip("。.!！ ")
    if not normalized or normalized.startswith("/"):
        return None
    mapping = {
        "1": 1,
        "2": 2,
        "3": 3,
        "4": 4,
        "5": 5,
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "第一首": 1,
        "第二首": 2,
        "第三首": 3,
        "换一批": 4,
        "重新推荐": 4,
        "再来一批": 4,
        "随机": 5,
        "随机匹配": 5,
    }
    if normalized in mapping:
        return mapping[normalized]
    match = re.fullmatch(r"(?:选|选择)?\s*([1-5])\s*(?:号|首)?", normalized)
    if match:
        return int(match.group(1))
    return None


def _request_exact_bgm_recommendations(
    session_id: str,
    instruction: str,
    asset_count: int,
    *,
    optimization_enabled: bool,
    continue_after_match: bool = True,
    recommendation_round: int = 1,
    language: str = "zh",
) -> dict:
    if not _exact_bgm_enabled():
        return _rewrite_smart_bgm_keyword_selection(
            session_id,
            instruction,
            asset_count,
            optimization_enabled=optimization_enabled,
            continue_after_match=continue_after_match,
            language=language,
        )
    _save_pending_edit(
        session_id,
        instruction,
        asset_count,
        "awaiting_exact_bgm_selection",
        optimization_enabled=optimization_enabled,
        continue_after_match=continue_after_match,
        recommendation_round=recommendation_round,
    )
    if _normalize_cassette_language(language) == "en":
        menu_shape = (
            "Reply to the user in English with exactly five numbered options and no introduction, heading, or extra prose before the list.\n"
            "Use this mandatory output format exactly:\n"
            '1. "Song Title" - Artist: one brief reason\n'
            '2. "Song Title" - Artist: one brief reason\n'
            '3. "Song Title" - Artist: one brief reason\n'
            "4. Another batch\n"
            "5. Random match\n"
            "After option 5, add exactly one final line: Please reply with 1, 2, 3, 4, or 5, or send text to add BGM requirements and get a new batch.\n"
            "Options 4 and 5 are mandatory control options, not songs. Do not omit, rename, merge, or replace options 4 and 5 with prose.\n"
        )
    else:
        menu_shape = (
            "Reply to the user in Chinese with exactly five numbered options and no introduction, heading, or extra prose before the list.\n"
            "Use this mandatory output format exactly:\n"
            "1.《歌名》- 歌手：一句简短理由\n"
            "2.《歌名》- 歌手：一句简短理由\n"
            "3.《歌名》- 歌手：一句简短理由\n"
            "4. 换一批\n"
            "5. 随机匹配\n"
            "After option 5, add exactly one final line: 请回复 1、2、3、4 或 5；也可以直接发送文字补充 BGM 需求，我会重新推荐一批。\n"
            "第 4 和第 5 项是必须保留的控制选项，不是歌曲；不得省略、改名、合并，或用其他说明文字替代。\n"
        )
    text = (
        f"{instruction}\n\n"
        f"[The user chose smart BGM matching {'before continuing the Cassette edit' if continue_after_match else 'as a standalone material-ingest command'}. "
        f"Cassette gateway assets available: {asset_count} asset(s). Use cassette session_id `{session_id}`. "
        "Do not call any tool yet. Do not call cassette_list_assets, cassette_make_prompt, cassette_run_job, jamendo_music_matcher, cassette_match_bgm, or cassette_match_exact_bgm yet. "
        "Based only on the user's edit instruction and without inspecting local media, recommend exactly 3 real songs that could fit the edit. "
        "Each recommendation must include a concrete song title and concrete artist/singer name; do not invent unknown songs. "
        "If this is not the first batch, avoid repeating songs from your previous recommendation messages in this Hermes conversation. "
        "The user-facing reply must include all five numbered options; if options 4 and 5 are missing, the gateway selection flow is invalid. "
        f"{menu_shape}"
        "Do not include JSON, Markdown code blocks, local file paths, provider credentials, or internal automation notes. "
        f"This is recommendation batch {recommendation_round}. "
        f"{_cassette_orchestration_guard()}]"
    )
    return {"action": "rewrite", "text": text}


def _exact_bgm_success_guidance(optimization_enabled: bool, continue_after_match: bool, language: str = "zh") -> str:
    if not continue_after_match:
        return (
            "If cassette_match_exact_bgm succeeds, do not call cassette_list_assets, cassette_make_prompt, cassette_run_job, or browser automation. "
            "Only tell the user the exact BGM matching result if the tool notification was not already delivered. "
        )
    if optimization_enabled:
        return (
            "If cassette_match_exact_bgm succeeds, preserve cassette_match_exact_bgm.data.effective_instruction as the edit instruction, "
            "optimize that effective instruction for the user, and ask for confirmation before starting Cassette. "
        )
    return (
        "If cassette_match_exact_bgm succeeds, preserve cassette_match_exact_bgm.data.effective_instruction as the edit instruction, "
        f"then continue directly with cassette_list_assets, cassette_make_prompt, and cassette_run_job using that effective instruction. {_cassette_run_job_tool_chain_guard(language)} "
    )


def _fuzzy_bgm_fallback_text(
    instruction: str,
    *,
    optimization_enabled: bool,
    continue_after_match: bool,
    freetouse_summary: str | None = None,
    language: str = "zh",
) -> str:
    summary = freetouse_summary if freetouse_summary is not None else _safe_freetouse_category_summary()
    if _jamendo_configured():
        return (
            "If the exact song search tool returns ok=false, fall back without asking the user again. "
            "First call jamendo_music_matcher with userQuery, searchTerms, fuzzyTags, excludeTerms, vocalInstrumental when clear, download=true, and session_id. "
            "Do not generate or pass raw Jamendo SearchPlan JSON. "
            "If Jamendo also returns ok=false after its internal 3-attempt search budget or reports missing/invalid credentials, fall back to Free To Use: choose exactly 3 Free To Use search queries from the category summary below and call cassette_match_bgm with session_id, instruction, search_queries, optimization_enabled="
            f'{str(bool(optimization_enabled)).lower()}, continue_after_match={str(bool(continue_after_match)).lower()}, fallback_from="exact_bgm", and fallback_reason set to the exact-song tool error code or concise failure reason. '
            f"Use the same success handling as the primary exact tool, including cassette_language='{_normalize_cassette_language(language) or 'zh'}'. Original instruction: {instruction}\n\n"
            "Free To Use fallback category summary:\n"
            f"{summary}\n"
        )
    return (
        "If the exact song search tool returns ok=false, fall back without asking the user again: choose exactly 3 Free To Use search queries from the category summary below and call cassette_match_bgm with session_id, instruction, search_queries, optimization_enabled="
        f'{str(bool(optimization_enabled)).lower()}, continue_after_match={str(bool(continue_after_match)).lower()}, fallback_from="exact_bgm", and fallback_reason set to the exact-song tool error code or concise failure reason. '
        f"Use the same success handling as the primary exact tool, including cassette_language='{_normalize_cassette_language(language) or 'zh'}'. Original instruction: {instruction}\n\n"
        "Free To Use fallback category summary:\n"
        f"{summary}\n"
    )


def _rewrite_exact_bgm_selection(
    session_id: str,
    instruction: str,
    asset_count: int,
    *,
    selected_index: int,
    optimization_enabled: bool,
    continue_after_match: bool,
    language: str = "zh",
) -> dict:
    text = (
        f"{instruction}\n\n"
        f"[The user selected smart BGM recommendation #{selected_index}. Cassette gateway assets available: {asset_count} asset(s). "
        f"Use cassette session_id `{session_id}`. "
        "Read ONLY the immediately previous assistant recommendation menu in this Hermes conversation. "
        f"Extract the exact song title and artist/singer from the exact numbered line `{selected_index}.` in that latest menu; do not use older recommendation batches, memory, or a different numbered line. "
        "If that exact numbered line is missing or you cannot unambiguously extract both title and artist from it, ask the user to reply with `song title - artist` and do not call any BGM or Cassette tool. "
        "When the selected line is unambiguous, make your next action a tool call to cassette_match_exact_bgm. "
        "Call cassette_match_exact_bgm with session_id, instruction, title, artist, optimization_enabled="
        f"{str(bool(optimization_enabled)).lower()}, continue_after_match={str(bool(continue_after_match)).lower()}, and download=true. "
        "Do not ask another BGM question, do not recommend a new song, and do not call Cassette tools before the BGM tool returns. "
        f"{_exact_bgm_success_guidance(optimization_enabled, continue_after_match, language)}"
        f"{_fuzzy_bgm_fallback_text(instruction, optimization_enabled=optimization_enabled, continue_after_match=continue_after_match, language=language)}"
        "The tools will send or provide the user-facing BGM result message; do not expose local paths, raw IDs, credentials, or worker commands. "
        f"{_cassette_orchestration_guard()}]"
    )
    return {"action": "rewrite", "text": text}


def _available_bgm_methods() -> list[str]:
    methods: list[str] = []
    if _exact_bgm_enabled():
        methods.append("exact_song")
    if _jamendo_configured():
        methods.append("jamendo")
    methods.append("freetouse")
    return methods


def _rewrite_random_bgm_provider_selection(
    session_id: str,
    instruction: str,
    asset_count: int,
    *,
    optimization_enabled: bool,
    continue_after_match: bool,
    language: str = "zh",
) -> dict:
    methods = _available_bgm_methods()
    primary = random.choice(methods)
    fallback_order = [method for method in methods if method != primary]
    summary = _safe_freetouse_category_summary()
    lines = [
        f"{instruction}\n\n",
        f"[The user selected random smart BGM matching. Cassette gateway assets available: {asset_count} asset(s). Use cassette session_id `{session_id}`. ",
        f"The plugin selected `{primary}` as the primary provider for this random attempt; fallback provider order is: {', '.join(fallback_order) or 'none'}. ",
        "Follow this provider order strictly and stop after the first successful BGM tool call. Do not ask the user another BGM question. ",
    ]
    if primary == "exact_song" or "exact_song" in fallback_order:
        lines.append(
            "For `exact_song`, Hermes must choose one concrete real song title plus concrete artist/singer that fits the edit instruction, then call cassette_match_exact_bgm with session_id, instruction, title, artist, optimization_enabled="
            f"{str(bool(optimization_enabled)).lower()}, continue_after_match={str(bool(continue_after_match)).lower()}, and download=true. "
            "Do not show the exact-song choice to the user in random mode before calling the tool. "
        )
    if primary == "jamendo" or "jamendo" in fallback_order:
        lines.append(
            "For `jamendo`, call jamendo_music_matcher with fixed-form userQuery, searchTerms, fuzzyTags, excludeTerms, optional vocalInstrumental, download=true, and session_id. "
            "Do not generate or pass raw Jamendo SearchPlan JSON. "
        )
    if primary == "freetouse" or "freetouse" in fallback_order:
        lines.append(
            "For `freetouse`, choose exactly 3 Free To Use music search queries from the category summary below and call cassette_match_bgm with session_id, instruction, search_queries, optimization_enabled="
            f"{str(bool(optimization_enabled)).lower()}, and continue_after_match={str(bool(continue_after_match)).lower()}. "
        )
    lines.append(
        f"{_exact_bgm_success_guidance(optimization_enabled, continue_after_match, language)}"
        f"For Jamendo or Free To Use success, use that tool's data.effective_instruction and hermes_next_step, preserving cassette_language='{_normalize_cassette_language(language) or 'zh'}'. "
        "If every provider fails, continue the edit without BGM and tell the user BGM matching failed without blocking the Cassette flow. "
        "Free To Use category summary:\n"
        f"{summary}\n"
        f"{_cassette_orchestration_guard()}]"
    )
    return {"action": "rewrite", "text": "".join(lines)}


def _safe_freetouse_category_summary() -> str:
    try:
        return _freetouse_category_summary()
    except Exception as exc:
        return f"- Free To Use category summary unavailable ({type(exc).__name__}); use concise English music category/mood terms inferred from the user instruction."


def _jamendo_configured() -> bool:
    # Read through the module: tools._disable_jamendo_bgm rebinds this at
    # runtime, and a from-import would freeze the value at import time.
    return tools_mod._JAMENDO_DISABLED_CODE is None and bool(
        str(os.getenv("JAMENDO_CLIENT_ID", "") or notifier._runtime_env("JAMENDO_CLIENT_ID")).strip()
    )


def _rewrite_smart_bgm_keyword_selection(
    session_id: str,
    instruction: str,
    asset_count: int,
    *,
    optimization_enabled: bool,
    continue_after_match: bool = True,
    language: str = "zh",
) -> dict:
    summary = _safe_freetouse_category_summary()
    if _jamendo_configured():
        return _rewrite_jamendo_first_bgm_selection(
            session_id,
            instruction,
            asset_count,
            optimization_enabled=optimization_enabled,
            continue_after_match=continue_after_match,
            freetouse_summary=summary,
            language=language,
        )
    if continue_after_match:
        after_match_guidance = (
            "After the tool returns, preserve cassette_match_bgm.data.effective_instruction as the edit instruction. "
            "If optimization_enabled is true, optimize that effective instruction for the user and ask for confirmation before starting Cassette. "
            f"If optimization_enabled is false, continue directly with cassette_list_assets, cassette_make_prompt, and cassette_run_job using that effective instruction. {_cassette_run_job_tool_chain_guard(language)} "
        )
    else:
        after_match_guidance = (
            "Call cassette_match_bgm with continue_after_match=false. "
            "After the tool returns, do not call cassette_list_assets, cassette_make_prompt, cassette_run_job, or any Cassette browser automation. "
            "Only tell the user the BGM matching result if the tool notification was not already delivered. "
        )
    text = (
        f"{instruction}\n\n"
        f"[The user chose smart BGM matching {'before continuing the Cassette edit' if continue_after_match else 'as a standalone material-ingest command'}. "
        f"Cassette gateway assets available: {asset_count} asset(s). Use cassette session_id `{session_id}`. "
        "Do not call cassette_list_assets, cassette_make_prompt, or cassette_run_job until after cassette_match_bgm returns. "
        "Choose exactly 3 Free To Use music search queries from the category summary below. "
        "Each query must be 1 to 4 lowercase English words, built from one Free To Use category name plus one or two related mood/tag words when useful. "
        "Keep the granularity medium: avoid one-word generic queries when the user intent is specific, and avoid long sentence-like queries. "
        "Do not use Chinese, local media analysis, file paths, or invented track titles. "
        "Then call cassette_match_bgm with session_id, instruction, search_queries, and optimization_enabled="
        f"{str(bool(optimization_enabled)).lower()}. "
        f"{after_match_guidance}"
        "The tool will send or provide the user-facing BGM result message; do not expose local paths or raw IDs.\n\n"
        "Free To Use category summary:\n"
        f"{summary}\n\n"
        f"{_cassette_orchestration_guard()}]"
    )
    return {"action": "rewrite", "text": text}


def _rewrite_jamendo_first_bgm_selection(
    session_id: str,
    instruction: str,
    asset_count: int,
    *,
    optimization_enabled: bool,
    continue_after_match: bool,
    freetouse_summary: str,
    language: str = "zh",
) -> dict:
    if continue_after_match:
        jamendo_success = (
            "If jamendo_music_matcher succeeds, preserve jamendo_music_matcher.data.effective_instruction as the edit instruction. "
            "If optimization_enabled is true, optimize that effective instruction for the user and ask for confirmation before starting Cassette. "
            f"If optimization_enabled is false, continue directly with cassette_list_assets, cassette_make_prompt, and cassette_run_job using that effective instruction. {_cassette_run_job_tool_chain_guard(language)} "
        )
        freetouse_success = (
            "If the Free To Use fallback succeeds, preserve cassette_match_bgm.data.effective_instruction as the edit instruction. "
            "If optimization_enabled is true, optimize that effective instruction for the user and ask for confirmation before starting Cassette. "
            f"If optimization_enabled is false, continue directly with cassette_list_assets, cassette_make_prompt, and cassette_run_job using that effective instruction. {_cassette_run_job_tool_chain_guard(language)} "
        )
    else:
        jamendo_success = (
            "If jamendo_music_matcher succeeds, do not call cassette_list_assets, cassette_make_prompt, cassette_run_job, or browser automation. "
            "Only tell the user the Jamendo BGM matching result if the tool notification was not already delivered. "
        )
        freetouse_success = (
            "If the Free To Use fallback succeeds, do not call cassette_list_assets, cassette_make_prompt, cassette_run_job, or browser automation. "
            "Only tell the user the BGM matching result if the tool notification was not already delivered. "
        )
    text = (
        f"{instruction}\n\n"
        f"[The user chose smart BGM matching {'before continuing the Cassette edit' if continue_after_match else 'as a standalone material-ingest command'}. "
        f"Cassette gateway assets available: {asset_count} asset(s). Use cassette session_id `{session_id}`. "
        "Jamendo credentials appear configured, so use Jamendo as the primary smart-BGM provider and Free To Use only as fallback. "
        "Do not call cassette_list_assets, cassette_make_prompt, or cassette_run_job until the BGM provider step finishes. "
        "Your next action must be a tool call, not a user-visible message: call jamendo_music_matcher with userQuery, searchTerms, fuzzyTags, excludeTerms, vocalInstrumental, download=true, session_id, and seed if the user provided one. "
        "Do not generate or pass a raw Jamendo SearchPlan JSON. Do not put any JSON in assistant content, Markdown, or a normal reply. "
        "Fill the fixed form only: searchTerms must be 1 to 5 short English Jamendo-friendly search phrases; fuzzyTags should be 0 to 8 English mood/genre/instrument words; excludeTerms should contain only clearly unwanted English words; vocalInstrumental must be vocal, instrumental, or omitted. "
        "Do not provide boost, order, type, duration, acousticElectric, speed, extraParams, or raw SearchPlan fields; the plugin will build safe Jamendo strategies itself. "
        "The plugin will search Jamendo across multiple result orders/boosts and has a 3-attempt zero-result budget before returning failure. "
        "If you cannot fill valid English searchTerms, skip Jamendo and use the Free To Use fallback below without showing the user any JSON. "
        f"{jamendo_success}"
        "If jamendo_music_matcher returns ok=false after its internal 3-attempt Jamendo search budget, or reports Jamendo missing configuration, credential/API validation failure, network failure, no eligible tracks, invalid fixed-form fields, or download failure, fall back to the existing Free To Use flow below without asking the user again. "
        "For the Free To Use fallback, choose exactly 3 Free To Use music search queries from the category summary. "
        "Each query must be 1 to 4 lowercase English words, built from one Free To Use category name plus one or two related mood/tag words when useful. "
        "Then call cassette_match_bgm with session_id, instruction, search_queries, optimization_enabled="
        f"{str(bool(optimization_enabled)).lower()}, and continue_after_match={str(bool(continue_after_match)).lower()}. "
        f"{freetouse_success}"
        "The tools will send or provide the user-facing BGM result message; do not expose local paths, raw IDs, Jamendo credentials, or worker commands.\n\n"
        "Free To Use fallback category summary:\n"
        f"{freetouse_summary}\n\n"
        f"{_cassette_orchestration_guard()}]"
    )
    return {"action": "rewrite", "text": text}


def _freetouse_categories() -> list[dict[str, Any]]:
    now = time.time()
    ttl = int(os.getenv("CASSETTE_FREETOUSE_CATEGORY_CACHE_SEC", "21600"))
    cached = _FREETOUSE_CATEGORY_CACHE.get("categories") or []
    loaded_at = float(_FREETOUSE_CATEGORY_CACHE.get("loaded_at") or 0.0)
    if cached and ttl > 0 and now - loaded_at < ttl:
        return list(cached)
    payload = _freetouse_request_json("/music/categories/all")
    data = payload.get("data") if isinstance(payload, dict) else payload
    categories = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    _FREETOUSE_CATEGORY_CACHE["categories"] = categories
    _FREETOUSE_CATEGORY_CACHE["loaded_at"] = now
    return categories


def _category_related_terms(description: str, limit: int = 5) -> list[str]:
    stopwords = {
        "with",
        "from",
        "your",
        "this",
        "that",
        "these",
        "those",
        "music",
        "tracks",
        "copyright",
        "free",
        "royalty",
        "videos",
        "video",
        "perfect",
        "project",
        "background",
        "content",
        "making",
        "create",
        "level",
        "next",
    }
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z-]{2,}", description or ""):
        value = token.lower().replace("-", " ")
        if value in stopwords or value in terms:
            continue
        terms.append(value)
        if len(terms) >= limit:
            break
    return terms


def _freetouse_category_summary(max_categories: int | None = None) -> str:
    categories = _freetouse_categories()
    categories = sorted(
        categories,
        key=lambda item: (
            str(item.get("type") or ""),
            -int(item.get("views") or 0),
            str(item.get("name") or ""),
        ),
    )
    if max_categories is None:
        max_categories = int(os.getenv("CASSETTE_FREETOUSE_CATEGORY_SUMMARY_LIMIT", "67"))
    lines: list[str] = []
    for item in categories[: max(1, max_categories)]:
        name = str(item.get("name") or "").strip()
        category_type = str(item.get("type") or "").strip()
        if not name:
            continue
        terms = ", ".join(_category_related_terms(str(item.get("description") or "")))
        if terms:
            lines.append(f"- {name} ({category_type}); related tags: {terms}")
        else:
            lines.append(f"- {name} ({category_type})")
    return "\n".join(lines)


def _bgm_note(result: dict[str, Any]) -> str:
    if result.get("status") == "downloaded":
        return (
            f"Smart BGM asset added: {result.get('artist') or 'unknown artist'} - "
            f"{result.get('title') or 'unknown title'} using query `{result.get('query') or ''}`. "
            "Use this uploaded audio asset as background music unless it conflicts with an explicit user requirement. "
        )
    code = result.get("code") or "bgm_skipped"
    return f"Smart BGM was skipped because {code}; continue the Cassette edit without blocking. "


def _rewrite_direct_original_instruction(
    session_id: str,
    instruction: str,
    asset_count: int,
    bgm_result: dict[str, Any] | None = None,
    *,
    direct_reason: str = "declined",
    language: str = "zh",
) -> dict:
    _clear_pending_edit(session_id)
    bgm_note = _bgm_note(bgm_result) if bgm_result else ""
    effective_instruction = _instruction_with_bgm(instruction, bgm_result, language=language)
    reason_label = (
        "Cassette prompt optimization skipped for this follow-up edit because the session already completed the first edit choices."
        if direct_reason == "session_default"
        else "Cassette prompt optimization declined."
    )
    text = (
        f"{effective_instruction}\n\n"
        f"[{reason_label} Cassette gateway assets available: {asset_count} asset(s). "
        f"{bgm_note}"
        f"Use cassette session_id `{session_id}` for this confirmed edit. "
        f"{_direct_original_instruction_guard(direct_reason, language)} "
        "Call cassette_run_job with wait=false for this gateway job so /cut can pause the active Cassette browser operation. "
        f"{_cassette_orchestration_guard()} "
        "Do not emit MEDIA tags or guess local export paths; cassette_run_job notification handles the stored gateway delivery target and reports any delivery failure.]"
    )
    return {"action": "rewrite", "text": text}


def _rewrite_accepted_prompt_optimization(
    session_id: str,
    instruction: str,
    asset_count: int,
    bgm_result: dict[str, Any] | None = None,
    language: str = "zh",
) -> dict:
    bgm_note = _bgm_note(bgm_result) if bgm_result else ""
    effective_instruction = _instruction_with_bgm(instruction, bgm_result, language=language)
    _save_pending_edit(session_id, effective_instruction, asset_count, "awaiting_optimized_brief_confirmation")
    text = (
        f"{effective_instruction}\n\n"
        f"[Cassette prompt optimization accepted. Cassette gateway assets available: {asset_count} asset(s). "
        f"{bgm_note}"
        f"Use cassette session_id `{session_id}` for this edit instruction. "
        f"{_accepted_prompt_optimization_guard(language)} "
        f"{_cassette_orchestration_guard()} "
        "Do not ask the user to resend the already saved media.]"
    )
    return {"action": "rewrite", "text": text}


def _start_gateway_edit_instruction(
    session_id: str,
    instruction: str,
    asset_count: int,
    gateway: Any,
    event: Any,
    *,
    force_refine: bool = False,
    language: str = "zh",
) -> dict | None:
    connectivity = _cassette_connectivity_skip(gateway, event, language)
    if connectivity:
        return connectivity
    if force_refine:
        return _rewrite_accepted_prompt_optimization(session_id, instruction, asset_count, language=language)
    return _rewrite_direct_original_instruction(
        session_id,
        instruction,
        asset_count,
        direct_reason="session_default",
        language=language,
    )


def ingest_gateway_media(event: Any = None, gateway: Any = None, **kwargs) -> dict | None:
    """Ingest authorized gateway media before the LLM turn sees the message.

    Hermes gateway adapters may expose media paths in event.media_urls without
    putting those paths into the user-visible prompt. This hook stores pure
    media messages as assets, then binds later text instructions to those assets
    for authorized users only, without exposing raw chat/user IDs.
    """
    if event is None:
        return None
    media_paths = list(getattr(event, "media_urls", None) or [])
    if gateway is not None and not _is_gateway_authorized(gateway, event):
        return None

    source = getattr(event, "source", None)
    platform = _platform_value(source)
    normalized_platform = _normalize_platform_name(platform)
    session_id = _gateway_session_id(event, kwargs.get("session_store"))
    cassette_language = _cassette_language_for_session(session_id, normalized_platform)
    user_text = (getattr(event, "text", None) or "").strip()
    forced_language = _forced_cassette_language_command(user_text)
    if forced_language is not None:
        return _handle_gateway_language_command(session_id, normalized_platform, gateway, event, forced_language)
    forced_model_command = _forced_cassette_model_command(user_text)
    if forced_model_command is not None:
        return _request_cassette_model_choice(
            session_id,
            "",
            _gateway_asset_count(session_id),
            gateway,
            event,
            language=cassette_language,
        )
    if _is_reserved_gateway_slash_command(user_text):
        return None
    forced_edit = _forced_edit_instruction(user_text)
    forced_refine = _forced_refine_instruction(user_text)
    forced_music = _forced_music_instruction(user_text)
    forced_check_assets = _forced_check_assets_command(user_text)
    forced_cut = _forced_cut_instruction(user_text)
    edit_text = forced_edit if forced_edit is not None else forced_refine if forced_refine is not None else user_text
    media_types = list(getattr(event, "media_types", None) or [])
    raw_media_failures: list[str] = []
    if forced_cut is not None:
        return _handle_gateway_cut_command(session_id, gateway, event, cassette_language)
    if not media_paths and normalized_platform == "qqbot":
        media_paths, media_types, raw_media_failures = _qq_raw_gateway_media(event, gateway)
    if not media_paths and normalized_platform == "telegram":
        media_paths, media_types, raw_media_failures = _telegram_raw_gateway_media(event, gateway)
    if not media_paths:
        if raw_media_failures:
            reply_sent = _send_gateway_fixed_reply(
                gateway, event, _fixed_media_failed_message(raw_media_failures, cassette_language)
            )
            return {
                "action": "skip",
                "reason": "cassette_media_ingest_failed",
                "errors": sorted(set(raw_media_failures)),
                "reply_sent": reply_sent,
            }
        if not user_text:
            return None
        asset_count = _gateway_asset_count(session_id)
        if forced_check_assets:
            reply_sent = _send_gateway_fixed_reply(
                gateway, event, _fixed_asset_status_message(session_id, cassette_language)
            )
            return {
                "action": "skip",
                "reason": "cassette_asset_status_reported",
                "asset_count": asset_count,
                "session_id": session_id,
                "reply_sent": reply_sent,
            }
        if _latest_active_job_for_session(session_id, source):
            return _reject_busy_flow(session_id, gateway, event, cassette_language, "cassette_active_job_busy_rejected")
        pending_edit = _load_pending_edit(session_id)
        if pending_edit and str(pending_edit.get("state") or "") in {
            "awaiting_model_choice",
            "awaiting_model_thinking_choice",
        }:
            return _handle_pending_cassette_model_choice(
                session_id,
                pending_edit,
                user_text,
                gateway,
                event,
                language=cassette_language,
            )
        if forced_music is not None:
            if not forced_music:
                reply_sent = _send_gateway_fixed_reply(
                    gateway, event, _fixed_music_command_missing_instruction_message(cassette_language)
                )
                return {
                    "action": "skip",
                    "reason": "cassette_music_command_missing_instruction",
                    "asset_count": asset_count,
                    "session_id": session_id,
                    "reply_sent": reply_sent,
                }
            return _request_exact_bgm_recommendations(
                session_id,
                forced_music,
                asset_count,
                optimization_enabled=False,
                continue_after_match=False,
                language=cassette_language,
            )
        if not asset_count:
            if forced_refine is not None:
                reply_sent = _send_gateway_fixed_reply(
                    gateway, event, _fixed_refine_command_missing_assets_message(cassette_language)
                )
                return {
                    "action": "skip",
                    "reason": "cassette_refine_command_missing_assets",
                    "session_id": session_id,
                    "reply_sent": reply_sent,
                }
            if forced_edit is not None:
                reply_sent = _send_gateway_fixed_reply(
                    gateway, event, _fixed_edit_command_missing_assets_message(cassette_language)
                )
                return {
                    "action": "skip",
                    "reason": "cassette_edit_command_missing_assets",
                    "session_id": session_id,
                    "reply_sent": reply_sent,
                }
            return None
        if forced_refine is not None:
            if not edit_text:
                reply_sent = _send_gateway_fixed_reply(
                    gateway, event, _fixed_refine_command_missing_instruction_message(cassette_language)
                )
                return {
                    "action": "skip",
                    "reason": "cassette_refine_command_missing_instruction",
                    "asset_count": asset_count,
                    "session_id": session_id,
                    "reply_sent": reply_sent,
                }
            return _start_gateway_edit_instruction(
                session_id,
                edit_text,
                asset_count,
                gateway,
                event,
                force_refine=True,
                language=cassette_language,
            )
        if forced_edit is not None:
            if not edit_text:
                reply_sent = _send_gateway_fixed_reply(
                    gateway, event, _fixed_edit_command_missing_instruction_message(cassette_language)
                )
                return {
                    "action": "skip",
                    "reason": "cassette_edit_command_missing_instruction",
                    "asset_count": asset_count,
                    "session_id": session_id,
                    "reply_sent": reply_sent,
                }
            return _start_gateway_edit_instruction(
                session_id, edit_text, asset_count, gateway, event, language=cassette_language
            )
        if _is_gateway_media_placeholder_text(user_text):
            reply_sent = _send_gateway_fixed_reply(
                gateway, event, _fixed_asset_status_message(session_id, cassette_language)
            )
            return {
                "action": "skip",
                "reason": "cassette_media_placeholder_ignored",
                "asset_count": asset_count,
                "session_id": session_id,
                "reply_sent": reply_sent,
            }
        if pending_edit and pending_edit.get("state") == "awaiting_exact_bgm_selection":
            pending_instruction = str(pending_edit.get("instruction") or "").strip()
            optimization_enabled = bool(pending_edit.get("optimization_enabled"))
            continue_after_match = bool(pending_edit.get("continue_after_match", True))
            choice = _parse_exact_bgm_choice(user_text)
            if choice in {1, 2, 3}:
                _clear_pending_edit(session_id)
                return _rewrite_exact_bgm_selection(
                    session_id,
                    pending_instruction,
                    asset_count,
                    selected_index=int(choice),
                    optimization_enabled=optimization_enabled,
                    continue_after_match=continue_after_match,
                    language=cassette_language,
                )
            if choice == 4:
                recommendation_round = int(pending_edit.get("recommendation_round") or 1) + 1
                return _request_exact_bgm_recommendations(
                    session_id,
                    pending_instruction,
                    asset_count,
                    optimization_enabled=optimization_enabled,
                    continue_after_match=continue_after_match,
                    recommendation_round=recommendation_round,
                    language=cassette_language,
                )
            if choice == 5:
                _clear_pending_edit(session_id)
                return _rewrite_random_bgm_provider_selection(
                    session_id,
                    pending_instruction,
                    asset_count,
                    optimization_enabled=optimization_enabled,
                    continue_after_match=continue_after_match,
                    language=cassette_language,
                )
            if user_text and not user_text.startswith("/"):
                recommendation_round = int(pending_edit.get("recommendation_round") or 1) + 1
                supplement = user_text.strip()
                if _normalize_cassette_language(cassette_language) == "en":
                    updated_instruction = f"{pending_instruction}\n\nAdditional BGM requirement from user: {supplement}"
                else:
                    updated_instruction = f"{pending_instruction}\n\n用户补充的 BGM 需求：{supplement}"
                return _request_exact_bgm_recommendations(
                    session_id,
                    updated_instruction,
                    asset_count,
                    optimization_enabled=optimization_enabled,
                    continue_after_match=continue_after_match,
                    recommendation_round=recommendation_round,
                    language=cassette_language,
                )
            reply_sent = _send_gateway_fixed_reply(
                gateway, event, _exact_bgm_selection_reask_message(cassette_language)
            )
            if reply_sent:
                return {
                    "action": "skip",
                    "reason": "cassette_exact_bgm_selection_reasked",
                    "asset_count": asset_count,
                    "session_id": session_id,
                    "reply_sent": True,
                }
            text = (
                f"{pending_instruction}\n\n"
                f"[Cassette exact smart BGM song selection is still required. Cassette gateway assets available: {asset_count} asset(s). "
                f"Use cassette session_id `{session_id}`. Ask exactly this question in {_language_name_for_prompt(cassette_language)}: {_exact_bgm_selection_reask_message(cassette_language)} "
                f"{_cassette_orchestration_guard()}]"
            )
            return {"action": "rewrite", "text": text}
        if pending_edit and pending_edit.get("state") == "awaiting_optimized_brief_confirmation":
            pending_instruction = str(pending_edit.get("instruction") or "").strip()
            if _looks_like_prompt_optimization_decline(user_text):
                connectivity = _cassette_connectivity_skip(gateway, event, cassette_language)
                if connectivity:
                    return connectivity
                return _rewrite_direct_original_instruction(
                    session_id, pending_instruction, asset_count, language=cassette_language
                )
            if _looks_like_prompt_confirmation(user_text):
                connectivity = _cassette_connectivity_skip(gateway, event, cassette_language)
                if connectivity:
                    return connectivity
                _clear_pending_edit(session_id)
                text = (
                    f"{user_text}\n\n"
                    f"[Cassette optimized prompt confirmed. Cassette gateway assets available: {asset_count} asset(s). "
                    f"Use cassette session_id `{session_id}` for this confirmed edit. "
                    f"{_confirmed_prompt_guard(cassette_language)} "
                    "Call cassette_run_job with wait=false for this gateway job so /cut can pause the active Cassette browser operation. "
                    f"{_cassette_orchestration_guard()} "
                    "Do not emit MEDIA tags or guess local export paths; cassette_run_job notification handles the stored gateway delivery target and reports any delivery failure.]"
                )
                return {"action": "rewrite", "text": text}
            return _reject_busy_flow(
                session_id, gateway, event, cassette_language, "cassette_optimized_brief_confirmation_busy_rejected"
            )
        if _looks_like_prompt_confirmation(user_text):
            connectivity = _cassette_connectivity_skip(gateway, event, cassette_language)
            if connectivity:
                return connectivity
            _clear_pending_edit(session_id)
            text = (
                f"{user_text}\n\n"
                f"[Cassette optimized prompt confirmed. Cassette gateway assets available: {asset_count} asset(s). "
                f"Use cassette session_id `{session_id}` for this confirmed edit. "
                f"{_confirmed_prompt_guard(cassette_language)} "
                "Call cassette_run_job with wait=false for this gateway job so /cut can pause the active Cassette browser operation. "
                f"{_cassette_orchestration_guard()} "
                "Do not emit MEDIA tags or guess local export paths; cassette_run_job notification handles the stored gateway delivery target and reports any delivery failure.]"
            )
            return {"action": "rewrite", "text": text}
        if _obviously_not_edit_instruction(user_text):
            return None
        return _rewrite_semantic_edit_instruction_judgment(session_id, user_text, asset_count, cassette_language)

    ingested: list[dict[str, Any]] = []
    failures: list[str] = list(raw_media_failures)
    for index, media_path in enumerate(media_paths):
        mime = media_types[index] if index < len(media_types) else ""
        try:
            data = manifest.ingest_asset(
                source_path=media_path,
                original_name=Path(media_path).name,
                media_type=_mime_to_media_type(mime, media_path),
                chat_id=getattr(source, "chat_id", None),
                user_id=getattr(source, "user_id", None),
                message_id=getattr(event, "message_id", None),
                chat_type=getattr(source, "chat_type", None),
                thread_id=getattr(source, "thread_id", None),
                caption=getattr(event, "text", None) or "",
                session_id=session_id,
                platform=str(platform),
            )
            ingested.append(data)
        except CassetteError as exc:
            failures.append(exc.code)
        except Exception:
            failures.append("internal_error")

    if not ingested:
        if failures:
            reply_sent = _send_gateway_fixed_reply(
                gateway, event, _fixed_media_failed_message(failures, cassette_language)
            )
            return {
                "action": "skip",
                "reason": "cassette_media_ingest_failed",
                "errors": sorted(set(failures)),
                "reply_sent": reply_sent,
            }
        return None

    asset_count = len(ingested)
    is_placeholder_text = _is_gateway_media_placeholder_text(user_text)
    if forced_check_assets:
        total_count = _gateway_asset_count(session_id)
        reply_sent = _send_gateway_fixed_reply(
            gateway, event, _fixed_asset_status_message(session_id, cassette_language)
        )
        return {
            "action": "skip",
            "reason": "cassette_asset_status_reported",
            "asset_count": total_count,
            "new_asset_count": asset_count,
            "session_id": session_id,
            "reply_sent": reply_sent,
            **({"warnings": sorted(set(failures))} if failures else {}),
        }
    if user_text and _latest_active_job_for_session(session_id, source):
        return _reject_busy_flow(session_id, gateway, event, cassette_language, "cassette_active_job_busy_rejected")
    if forced_music is not None:
        total_count = _gateway_asset_count(session_id)
        if not forced_music:
            reply_sent = _send_gateway_fixed_reply(
                gateway, event, _fixed_music_command_missing_instruction_message(cassette_language)
            )
            return {
                "action": "skip",
                "reason": "cassette_music_command_missing_instruction",
                "asset_count": asset_count,
                "total_asset_count": total_count,
                "session_id": session_id,
                "reply_sent": reply_sent,
                **({"warnings": sorted(set(failures))} if failures else {}),
            }
        result = _request_exact_bgm_recommendations(
            session_id,
            forced_music,
            total_count,
            optimization_enabled=False,
            continue_after_match=False,
            language=cassette_language,
        )
        if failures:
            result["warnings"] = sorted(set(failures))
        return result
    if forced_refine is not None and not edit_text:
        total_count = _gateway_asset_count(session_id)
        reply_sent = _send_gateway_fixed_reply(
            gateway, event, _fixed_refine_command_missing_instruction_message(cassette_language)
        )
        return {
            "action": "skip",
            "reason": "cassette_refine_command_missing_instruction",
            "asset_count": asset_count,
            "total_asset_count": total_count,
            "session_id": session_id,
            "reply_sent": reply_sent,
            **({"warnings": sorted(set(failures))} if failures else {}),
        }
    if forced_edit is not None and not edit_text:
        total_count = _gateway_asset_count(session_id)
        reply_sent = _send_gateway_fixed_reply(
            gateway, event, _fixed_edit_command_missing_instruction_message(cassette_language)
        )
        return {
            "action": "skip",
            "reason": "cassette_edit_command_missing_instruction",
            "asset_count": asset_count,
            "total_asset_count": total_count,
            "session_id": session_id,
            "reply_sent": reply_sent,
            **({"warnings": sorted(set(failures))} if failures else {}),
        }
    if is_placeholder_text:
        total_count = _gateway_asset_count(session_id)
        reply_sent = _send_gateway_fixed_reply(
            gateway,
            event,
            _fixed_media_saved_message(asset_count, total_count, failures, cassette_language),
        )
        return {
            "action": "skip",
            "reason": "cassette_media_saved_waiting_for_instruction",
            "asset_count": asset_count,
            "total_asset_count": total_count,
            "session_id": session_id,
            "reply_sent": reply_sent,
            **({"warnings": sorted(set(failures))} if failures else {}),
        }
    if forced_edit is None and forced_refine is None:
        total_count = _gateway_asset_count(session_id)
        if not _obviously_not_edit_instruction(edit_text):
            result = _rewrite_semantic_edit_instruction_judgment(session_id, edit_text, total_count, cassette_language)
            if failures:
                result["warnings"] = sorted(set(failures))
            return result
        reply_sent = _send_gateway_fixed_reply(
            gateway,
            event,
            _fixed_media_saved_message(asset_count, total_count, failures, cassette_language),
        )
        return {
            "action": "skip",
            "reason": "cassette_media_saved_waiting_for_instruction",
            "asset_count": asset_count,
            "total_asset_count": total_count,
            "session_id": session_id,
            "reply_sent": reply_sent,
            **({"warnings": sorted(set(failures))} if failures else {}),
        }
    result = _start_gateway_edit_instruction(
        session_id,
        edit_text,
        _gateway_asset_count(session_id),
        gateway,
        event,
        force_refine=forced_refine is not None,
        language=cassette_language,
    )
    if failures:
        if result.get("action") == "rewrite":
            result["text"] += f"\n[Some media failed safe ingestion with codes: {', '.join(sorted(set(failures)))}.]"
        else:
            result["warnings"] = sorted(set(failures))
    return result


def handle_cassette_command(raw_args: str = "") -> str:
    parts = (raw_args or "help").strip().split()
    cmd = parts[0] if parts else "help"
    if cmd == "help":
        return "/cassette help | status <job_id> | cancel <job_id> | cut [job_id] | language [zh|en] | recent [limit]"
    if cmd == "status" and len(parts) >= 2:
        return cassette_job_status({"job_id": parts[1]})
    if cmd == "cancel" and len(parts) >= 2:
        return cassette_cancel_job({"job_id": parts[1]})
    if cmd == "cut":
        return handle_cut_command(" ".join(parts[1:]))
    if cmd in {"language", "lang", "语言"}:
        requested = parts[1] if len(parts) > 1 else ""
        normalized = _normalize_cassette_language(requested)
        if requested and not normalized:
            return "Unsupported Cassette language. Use /cassette language zh or /cassette language en."
        if normalized:
            return (
                f"Use `/cassette language {normalized}` inside the QQ or Telegram conversation "
                "to set that gateway session's Cassette language. QQ defaults to zh; Telegram defaults to en."
            )
        return "Use `/cassette language zh` or `/cassette language en` inside a QQ or Telegram conversation."
    if cmd == "recent":
        limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
        return cassette_job_status({"limit": limit})
    return "Unknown /cassette command. Try: /cassette help"


def handle_cassette_model_command(raw_args: str = "") -> str:
    return "请在 QQ/Telegram gateway 对话里发送 /cassette_model 来为该会话选择 Cassette 模型和思考程度。"


def handle_cut_command(raw_args: str = "") -> str:
    parts = (raw_args or "").strip().split()
    job_id = parts[0] if parts and parts[0].startswith("cassette_") else ""
    active_job = None
    if job_id:
        try:
            active_job = jobs.load_job(job_id)
        except Exception:
            active_job = None
    if active_job is None:
        active_job = _latest_active_job()
        job_id = str((active_job or {}).get("job_id") or "")
    if job_id and active_job and _is_active_job(active_job):
        try:
            jobs.request_cancel(job_id)
        except Exception:
            return "Cassette 停止请求写入失败，请稍后重试或使用 /cassette status 查看任务状态。"
        return _fixed_cut_requested_message(True)
    return _fixed_cut_requested_message(False)


def close_cassette_browser_sessions(**kwargs) -> None:
    transport.get_transport().close_sessions()


def _compact_user_text(raw: str, max_chars: int = 700) -> str:
    text = " ".join(str(raw or "").split())
    text = redact_for_log(text)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def inject_cassette_context(**kwargs) -> str | None:
    del kwargs
    try:
        recent = jobs.list_jobs(limit=6)
    except Exception:
        return None
    if not recent:
        return None
    review_jobs = _completion_review_jobs(recent)
    if review_jobs:
        lines = [
            "Cassette completion review required. Hermes is the supervisor and must make the semantic completion decision from the latest Cassette reply, not from hard-coded keyword matching.",
        ]
        for job in review_jobs:
            lines.append(f"- job_id={job['job_id']} status={job['status']} latest_cassette_reply={job['summary']}")
        lines.extend(
            [
                'If the latest Cassette reply means the requested edit is complete enough to export, call cassette_review_completion with decision="export" and a concise reason.',
                'If Cassette says it is still editing or needs routine continuation — or the user asked for another change before export — call cassette_review_completion with decision="continue"; it starts a follow-up turn on the same session carrying message (or reason) verbatim.',
                'If Cassette asks for a material user choice or missing asset, call cassette_review_completion with decision="needs_user".',
                'If Cassette reports an unrecoverable failure, call cassette_review_completion with decision="failed".',
                "Do not expose local paths, raw IDs, prompts, or worker commands.",
            ]
        )
        return "\n".join(lines)
    summaries = [f"{j.get('job_id')}: {j.get('status')}" for j in recent]
    return "Recent Cassette jobs: " + "; ".join(summaries)


def _completion_review_jobs(recent: list[dict]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for job in recent:
        if job.get("status") != "needs_user":
            continue
        for question in job.get("questions") or []:
            if not isinstance(question, dict) or question.get("reason") != "completion_requires_hermes_review":
                continue
            quality = job.get("quality") if isinstance(job.get("quality"), dict) else {}
            summary = _compact_user_text(str(question.get("question") or quality.get("progress_summary") or ""), 700)
            result.append(
                {
                    "job_id": str(job.get("job_id") or ""),
                    "status": str(job.get("status") or ""),
                    "summary": summary,
                }
            )
            break
    return result[:3]


def log_cassette_tool_call(**kwargs) -> None:
    tool_name = str(kwargs.get("tool_name") or "")
    if not tool_name.startswith("cassette_"):
        return None
    try:
        log_dir = manifest.get_asset_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        result = kwargs.get("result")
        status = "unknown"
        if isinstance(result, str):
            try:
                status = "ok" if json.loads(result).get("ok") else "error"
            except Exception:
                status = "unparseable"
        line = f"{jobs.now_iso()} tool={redact_for_log(tool_name)} status={status}\n"
        with (log_dir / "cassette.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        return None


def _cassette_model_selection_completed(session_id: str) -> bool:
    return bool(_cassette_model_preference_for_session(session_id))


def _looks_like_prompt_optimization_accept(text: str) -> bool:
    normalized = (text or "").strip().lower()
    normalized = normalized.strip("。.!！ ")
    if not normalized:
        return False
    if _looks_like_prompt_optimization_decline(normalized):
        return False
    accept_terms = (
        "是",
        "用",
        "使用",
        "需要",
        "优化",
        "帮我优化",
        "先优化",
        "可以",
        "确认",
        "好的",
        "好",
        "yes",
        "y",
        "ok",
        "okay",
        "optimize",
        "use optimization",
    )
    return any(term in normalized for term in accept_terms)
