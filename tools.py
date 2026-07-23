from __future__ import annotations

import asyncio
import functools
import inspect
import json
import mimetypes
import os
import random
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from . import browser, exact_bgm, jamendo, jobs, manifest, notifier, prompt as prompt_mod, security, transport
from .errors import CassetteError
from .security import redact_for_log, safe_hash_id

_FREETOUSE_CATEGORY_CACHE: dict[str, Any] = {"loaded_at": 0.0, "categories": []}
_JAMENDO_DISABLED_CODE: str | None = None
_JAMENDO_AUTH_ERROR_HINTS = ("client_id", "credential", "credentials", "auth", "unauthorized", "forbidden")
_GATEWAY_JOB_EXECUTOR: ThreadPoolExecutor | None = None
_GATEWAY_JOB_EXECUTOR_LOCK = threading.Lock()


def ok(data: dict | None = None, warnings: list | None = None, job_id: str | None = None) -> str:
    payload: dict[str, Any] = {"ok": True, "data": data or {}, "warnings": warnings or []}
    if job_id:
        payload["job_id"] = job_id
    return json.dumps(payload, ensure_ascii=False)


def err(
    tool: str,
    code: str,
    message: str,
    details: dict | None = None,
    recoverable: bool = True,
) -> str:
    payload: dict[str, Any] = {
        "ok": False,
        "tool": tool,
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "recoverable": recoverable,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _safe_call(tool_name: str, fn, args: dict, **kwargs) -> str:
    try:
        return fn(args or {}, **kwargs)
    except CassetteError as exc:
        return err(tool_name, exc.code, str(exc), exc.details, exc.recoverable)
    except Exception as exc:
        # Coded transport errors (ApiTransportError and friends) keep their code so hosts can
        # react to e.g. validation_failed / stale_timeline instead of an opaque internal_error.
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            return err(tool_name, code, str(exc), {"type": type(exc).__name__}, True)
        return err(tool_name, "internal_error", str(exc), {"type": type(exc).__name__}, True)


def safe_tool(fn):
    """Wrap a tool entrypoint so raised exceptions become the standard err() envelope."""
    tool_name = fn.__name__

    @functools.wraps(fn)
    def wrapper(args: dict, **kwargs) -> str:
        return _safe_call(tool_name, fn, args, **kwargs)

    return wrapper


@safe_tool
def cassette_ingest_media(a: dict, **kw) -> str:
    data = manifest.ingest_asset(
        source_path=a.get("source_path"),
        original_name=a.get("original_name"),
        media_type=a.get("media_type"),
        chat_id=a.get("chat_id"),
        user_id=a.get("user_id"),
        message_id=a.get("message_id"),
        chat_type=a.get("chat_type"),
        thread_id=a.get("thread_id"),
        platform=a.get("platform"),
        caption=a.get("caption"),
        session_id=a.get("session_id"),
        task_id=kw.get("task_id"),
    )
    return ok(_scrub_ingest_data(data))


@safe_tool
def cassette_list_assets(a: dict, **kw) -> str:
    return ok(_scrub_list_assets(manifest.list_assets(a.get("session_id"), a.get("chat_id"), kw.get("task_id"))))


@safe_tool
def cassette_make_prompt(a: dict, **kw) -> str:
    instruction = (a.get("instruction") or "").strip()
    if not instruction:
        raise CassetteError("missing_required_arg", "instruction is required")
    listed = manifest.list_assets(a.get("session_id"), a.get("chat_id"), kw.get("task_id"))
    session_manifest = listed["manifest"]
    if a.get("requires_assets", True) and not session_manifest.get("assets"):
        raise CassetteError("missing_critical_assets", "No media assets are available for this Cassette edit")
    data = prompt_mod.build_cassette_prompt(
        instruction,
        session_manifest,
        {
            "output_format": a.get("output_format"),
            "duration": a.get("duration"),
            "style": a.get("style"),
            "cassette_language": _normalize_cassette_language(a.get("cassette_language") or a.get("language")),
            "constraints": a.get("constraints") or {},
        },
        runtime_host=str(kw.get("runtime_host") or "hermes"),
    )
    return ok(data)


@safe_tool
def cassette_answer_question(a: dict, **kw) -> str:
    job_id = str(a.get("job_id") or "").strip()
    response = str(a.get("response") or "").strip()
    if job_id or response:
        if not job_id or not response:
            raise CassetteError("missing_required_arg", "resume mode requires both job_id and response")
        job = jobs.load_job(job_id)
        quality = job.get("quality") if isinstance(job.get("quality"), dict) else {}
        if job.get("status") != "needs_user" or quality.get("completion_review_required"):
            raise CassetteError(
                "invalid_transition",
                "cassette_answer_question can resume only a user-input-paused job; use cassette_review_completion for completion review",
                {"job_id": job_id, "status": job.get("status") or ""},
            )
        if kw.get("runtime_host") == "mcp":
            if transport.selected_transport() == transport.TRANSPORT_BROWSER:
                from . import browser

                if not browser.has_live_browser_session_threaded(job):
                    result = transport.get_transport().resume(job, response)
                    job = jobs.merge_persisted_runtime_fields(job)
                    job.update(result)
                    job["status"] = result.get("status", "failed")
                    job["finished_at"] = jobs.now_iso()
                    job.pop("resume_request", None)
                    job.pop("continuation", None)
                    jobs.save_job(job)
                    return ok({"job": _scrub_job(job), "background": False}, job_id=job_id)
                job["status"] = "running"
                job["started_at"] = job.get("started_at") or jobs.now_iso()
                job["finished_at"] = None
                job["worker_kind"] = "thread"
                job["resume_request"] = {"response": response}
                jobs.save_job(job)
                _gateway_job_executor().submit(
                    _finish_background_cassette_job,
                    job_id,
                    "resume",
                    response,
                )
                return ok({"job": _scrub_job(job), "background": True}, job_id=job_id)
            job = jobs.start_worker(job_id, action="resume", response=response)
            return ok({"job": _scrub_job(job), "background": True}, job_id=job_id)
        result = transport.get_transport().resume(job, response)
        job = jobs.merge_persisted_runtime_fields(job)
        job.update(result)
        job["status"] = result.get("status", "failed")
        job["finished_at"] = jobs.now_iso()
        job.pop("resume_request", None)
        if job["status"] != "needs_user":
            job.pop("continuation", None)
        jobs.save_job(job)
        return ok({"job": _scrub_job(job)}, job_id=job_id)
    question = (a.get("question") or "").strip()
    if not question:
        raise CassetteError("missing_required_arg", "question is required")
    context = a.get("context") or {}
    context.update({"instruction": a.get("instruction"), "asset_count": a.get("asset_count")})
    return ok(prompt_mod.classify_cassette_question(question, context))


@safe_tool
def cassette_match_bgm(a: dict, **kw) -> str:
    session_id = str(a.get("session_id") or kw.get("task_id") or "").strip()
    instruction = str(a.get("instruction") or "").strip()
    raw_queries = a.get("search_queries") or []
    if isinstance(raw_queries, str):
        raw_queries = [raw_queries]
    if not session_id:
        raise CassetteError("missing_required_arg", "session_id is required")
    if not instruction:
        raise CassetteError("missing_required_arg", "instruction is required")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise CassetteError("missing_required_arg", "search_queries is required")
    search_queries = [_sanitize_bgm_query(str(query)) for query in raw_queries]
    search_queries = [query for query in search_queries if query]
    if not search_queries:
        raise CassetteError("invalid_bgm_search_query", "At least one valid Free To Use search query is required")
    continue_after_match = a.get("continue_after_match", True)
    continue_after_match = True if continue_after_match is None else bool(continue_after_match)
    optimization_enabled = bool(a.get("optimization_enabled"))
    fallback_from = str(a.get("fallback_from") or "").strip()
    fallback_reason = str(a.get("fallback_reason") or "").strip()
    session_hash = _debug_session_hash(session_id)
    started = time.monotonic()
    _log_cassette_debug_event(
        "bgm_freetouse_search_started",
        session_hash=session_hash,
        search_queries=search_queries[:3],
        continue_after_match=continue_after_match,
        optimization_enabled=optimization_enabled,
        fallback_from=fallback_from,
        fallback_reason=fallback_reason,
    )
    language = _cassette_language_for_session(session_id)
    try:
        result = dict(_match_and_download_smart_bgm(session_id, instruction, search_queries))
        if fallback_from:
            result["fallback_from"] = fallback_from
            if fallback_reason:
                result["fallback_reason"] = fallback_reason
        effective_instruction = (
            _instruction_with_bgm(instruction, result, language=language) if continue_after_match else instruction
        )
        if continue_after_match:
            if optimization_enabled:
                _save_pending_edit(
                    session_id,
                    effective_instruction,
                    _gateway_asset_count(session_id),
                    "awaiting_optimized_brief_confirmation",
                    optimization_enabled=True,
                )
            else:
                _clear_pending_edit(session_id)
        user_message = _smart_bgm_status_message(result, continue_after_match=continue_after_match, language=language)
        notification = _notify_smart_bgm_result(session_id, user_message)
        data = {
            "status": result.get("status") or "skipped",
            "code": result.get("code") or "",
            "search_queries": search_queries[:3],
            "selected": {
                "artist": result.get("artist") or "",
                "title": result.get("title") or "",
                "query": result.get("query") or "",
                "source_rank": result.get("source_rank") or "",
                "track_id": result.get("track_id") or "",
            },
            "asset_count": _gateway_asset_count(session_id),
            "effective_instruction": effective_instruction,
            "user_message": user_message,
            "notification": notification,
            "fallback": {
                "from": fallback_from,
                "reason": fallback_reason,
            }
            if fallback_from
            else {},
            "hermes_next_step": _bgm_next_step_guidance(
                optimization_enabled,
                continue_after_match=continue_after_match,
                language=language,
            ),
        }
        _log_cassette_debug_event(
            "bgm_freetouse_search_done",
            session_hash=session_hash,
            status=data["status"],
            code=data["code"],
            search_queries=search_queries[:3],
            attempted_queries=result.get("queries") or search_queries[:3],
            zero_result_queries=result.get("zero_result_queries") or [],
            selected=data["selected"],
            notification_status=notification.get("status") if isinstance(notification, dict) else "",
            fallback_from=fallback_from,
            fallback_reason=fallback_reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return ok(data)
    except CassetteError as exc:
        _log_cassette_debug_event(
            "bgm_freetouse_search_failed",
            session_hash=session_hash,
            code=exc.code,
            message=str(exc),
            recoverable=exc.recoverable,
            details=exc.details,
            search_queries=search_queries[:3],
            fallback_from=fallback_from,
            fallback_reason=fallback_reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise
    except Exception as exc:
        _log_cassette_debug_event(
            "bgm_freetouse_search_failed",
            session_hash=session_hash,
            code="internal_error",
            error_type=type(exc).__name__,
            message=str(exc),
            search_queries=search_queries[:3],
            fallback_from=fallback_from,
            fallback_reason=fallback_reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise


@safe_tool
def cassette_match_exact_bgm(a: dict, **kw) -> str:
    session_id = str(a.get("session_id") or kw.get("task_id") or "").strip()
    instruction = str(a.get("instruction") or "").strip()
    title = str(a.get("title") or a.get("songTitle") or a.get("song_title") or "").strip()
    artist = str(a.get("artist") or a.get("singer") or "").strip()
    title, artist = _normalize_exact_bgm_request(title, artist)
    if not session_id:
        raise CassetteError("missing_required_arg", "session_id is required")
    if not instruction:
        raise CassetteError("missing_required_arg", "instruction is required")
    if not title:
        raise CassetteError("missing_required_arg", "title is required")
    continue_after_match = a.get("continue_after_match", True)
    continue_after_match = True if continue_after_match is None else bool(continue_after_match)
    download = bool(a.get("download", True))
    optimization_enabled = bool(a.get("optimization_enabled"))
    session_hash = _debug_session_hash(session_id)
    started = time.monotonic()
    _log_cassette_debug_event(
        "bgm_exact_search_started",
        session_hash=session_hash,
        title=title,
        artist=artist,
        download=download,
        continue_after_match=continue_after_match,
        optimization_enabled=optimization_enabled,
    )
    language = _cassette_language_for_session(session_id)
    try:
        result = exact_bgm.match_exact_bgm(
            session_id=session_id,
            instruction=instruction,
            title=title,
            artist=artist,
            download=download,
        )
        effective_instruction = (
            _instruction_with_bgm(instruction, result, language=language) if continue_after_match else instruction
        )
        if continue_after_match and download and result.get("status") == "downloaded":
            if optimization_enabled:
                _save_pending_edit(
                    session_id,
                    effective_instruction,
                    _gateway_asset_count(session_id),
                    "awaiting_optimized_brief_confirmation",
                    optimization_enabled=True,
                )
            else:
                _clear_pending_edit(session_id)
        user_message = _smart_bgm_status_message(result, continue_after_match=continue_after_match, language=language)
        notification = (
            _notify_smart_bgm_result(session_id, user_message)
            if download
            else {"status": "skipped", "reason": "download_false"}
        )
        data = {
            "status": result.get("status") or "matched",
            "provider": result.get("provider") or "musicsquare_exact",
            "selected": {
                "artist": result.get("artist") or "",
                "title": result.get("title") or "",
                "query": result.get("query") or "",
                "source": result.get("source") or "",
                "track_id": result.get("track_id") or "",
            },
            "asset_count": _gateway_asset_count(session_id),
            "effective_instruction": effective_instruction,
            "user_message": user_message,
            "notification": notification,
            "metadata_path": result.get("metadata_path") or "",
            "attempts": result.get("attempts") or [],
            "hermes_next_step": _bgm_next_step_guidance(
                optimization_enabled,
                continue_after_match=continue_after_match,
                language=language,
            ),
        }
        if not download:
            data["eligibleCandidates"] = result.get("eligibleCandidates") or []
            data["candidateCount"] = result.get("candidateCount") or 0
        _log_cassette_debug_event(
            "bgm_exact_search_done",
            session_hash=session_hash,
            status=data["status"],
            provider=data["provider"],
            title=title,
            artist=artist,
            selected=data["selected"],
            candidate_count=result.get("candidateCount") or 0,
            attempts=_summarize_exact_bgm_attempts(result.get("attempts") or []),
            notification_status=notification.get("status") if isinstance(notification, dict) else "",
            metadata_saved=bool(result.get("metadata_path")),
            download=download,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return ok(data)
    except CassetteError as exc:
        _log_cassette_debug_event(
            "bgm_exact_search_failed",
            session_hash=session_hash,
            code=exc.code,
            message=str(exc),
            recoverable=exc.recoverable,
            title=title,
            artist=artist,
            attempts=_summarize_exact_bgm_attempts(exc.details.get("attempts") or []),
            details=exc.details,
            download=download,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise
    except Exception as exc:
        _log_cassette_debug_event(
            "bgm_exact_search_failed",
            session_hash=session_hash,
            code="internal_error",
            error_type=type(exc).__name__,
            message=str(exc),
            title=title,
            artist=artist,
            download=download,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise


def _normalize_exact_bgm_request(title: str, artist: str = "") -> tuple[str, str]:
    cleaned_title = re.sub(r"^\s*\d+\s*[.．、)]\s*", "", str(title or "").strip())
    cleaned_artist = str(artist or "").strip()
    if not cleaned_artist:
        parsed = _split_exact_bgm_menu_line(cleaned_title)
        if parsed:
            cleaned_title, cleaned_artist = parsed
        else:
            cleaned_title = _trim_exact_bgm_reason(cleaned_title)
    else:
        cleaned_title = _trim_exact_bgm_reason(cleaned_title)
        cleaned_artist = _trim_exact_bgm_reason(cleaned_artist)
    return _strip_exact_bgm_wrappers(cleaned_title), _strip_exact_bgm_wrappers(cleaned_artist)


def _split_exact_bgm_menu_line(value: str) -> tuple[str, str] | None:
    text = _trim_exact_bgm_reason(value)
    wrapped = r"[《「『“\"'].*?[》」』”\"']"
    patterns = [
        rf"^\s*(?P<title>{wrapped})\s*[-–—]\s*(?P<artist>.+?)\s*$",
        r"^\s*(?P<title>.+?)\s+[-–—]\s+(?P<artist>.+?)\s*$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text)
        if match:
            return match.group("title").strip(), _trim_exact_bgm_reason(match.group("artist"))
    return None


def _trim_exact_bgm_reason(value: str) -> str:
    return re.split(r"\s*[：:]\s*", str(value or "").strip(), maxsplit=1)[0].strip()


def _strip_exact_bgm_wrappers(value: str) -> str:
    text = str(value or "").strip()
    pairs = [("《", "》"), ("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("'", "'")]
    changed = True
    while changed and len(text) >= 2:
        changed = False
        for left, right in pairs:
            if text.startswith(left) and text.endswith(right):
                text = text[len(left) : len(text) - len(right)].strip()
                changed = True
                break
    return text


@safe_tool
def jamendo_music_matcher(a: dict, **kw) -> str:
    user_query = str(a.get("userQuery") or a.get("user_query") or a.get("query") or "").strip()
    if not user_query:
        raise CassetteError("missing_required_arg", "userQuery is required")
    planner = jamendo.HermesJamendoPlanner()
    plan_payload = a.get("searchPlan") or a.get("search_plan") or a.get("hermesJson") or a.get("hermes_json")
    repair_payload = a.get("repairJson") or a.get("repair_json")
    if plan_payload is None:
        plan = jamendo.build_search_plan_from_form(
            user_query=user_query,
            search_terms=_string_list_arg(a.get("searchTerms") or a.get("search_terms") or a.get("terms")),
            fuzzy_tags=_string_list_arg(a.get("fuzzyTags") or a.get("fuzzy_tags")),
            exclude_terms=_string_list_arg(a.get("excludeTerms") or a.get("exclude_terms")),
            vocalinstrumental=a.get("vocalInstrumental") or a.get("vocalinstrumental"),
            limit=_optional_int_arg(a.get("limit"), "limit"),
        )
    else:
        plan = planner.build_search_plan(user_query, plan_payload, repair_payload)
    download = bool(a.get("download", True))
    seed = _optional_int_arg(a.get("seed"), "seed")
    limit_override = _optional_int_arg(a.get("limitOverride", a.get("limit_override")), "limitOverride")
    output_dir_value = a.get("outputDir") or a.get("output_dir")
    output_dir = Path(str(output_dir_value)).expanduser() if output_dir_value else None
    try:
        result = jamendo.match_jamendo_music(
            user_query=user_query,
            search_plan=plan,
            download=download,
            seed=seed,
            limit_override=limit_override,
            output_dir=output_dir,
            session_id=str(a.get("session_id") or kw.get("task_id") or "").strip() or None,
        )
    except CassetteError as exc:
        if _jamendo_error_disables_provider(exc):
            _disable_jamendo_bgm(exc.code)
        raise
    return ok(result)


def _optional_int_arg(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CassetteError("invalid_argument", f"{name} must be an integer") from exc


def _string_list_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [item.strip() for item in re.split(r"[,;|，；\n]+", text) if item.strip()]


def _disable_jamendo_bgm(code: str) -> None:
    global _JAMENDO_DISABLED_CODE
    _JAMENDO_DISABLED_CODE = code


def _jamendo_error_disables_provider(exc: CassetteError) -> bool:
    if exc.code == "jamendo_http_error":
        status = exc.details.get("status")
        try:
            return int(status) in {401, 403}
        except (TypeError, ValueError):
            return False
    if exc.code != "jamendo_api_error":
        return False
    text = " ".join(str(value).lower() for value in [str(exc), *exc.details.values()])
    return any(hint in text for hint in _JAMENDO_AUTH_ERROR_HINTS)


def _scrub_ingest_data(data: dict) -> dict:
    scrubbed = dict(data or {})
    scrubbed.pop("saved_path", None)
    scrubbed.pop("manifest_path", None)
    return scrubbed


def _scrub_list_assets(data: dict) -> dict:
    manifest_data = dict((data or {}).get("manifest") or {})
    assets = []
    for asset in manifest_data.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        cleaned = {key: value for key, value in asset.items() if key not in {"saved_path"}}
        if asset.get("saved_path"):
            cleaned["stored"] = True
        assets.append(cleaned)
    manifest_data["assets"] = assets
    delivery = dict(manifest_data.pop("delivery", {}) or {})
    if delivery:
        manifest_data["delivery"] = {
            "platform": delivery.get("platform") or "",
            "chat_hash": safe_hash_id(delivery.get("chat_id")),
            "user_hash": safe_hash_id(delivery.get("user_id")),
            "chat_type": delivery.get("chat_type") or "",
            "has_raw_target": bool(delivery.get("chat_id")),
            "has_thread": bool(delivery.get("thread_id")),
        }
    return {"manifest": manifest_data}


def _asset_paths_for_session(
    session_id: str | None, chat_id: str | None, task_id: str | None
) -> tuple[str, list[str], dict]:
    listed = manifest.list_assets(session_id, chat_id, task_id)
    session_manifest = listed["manifest"]
    return (
        session_manifest.get("session_hash", ""),
        [a["saved_path"] for a in session_manifest.get("assets", []) if a.get("exists") and a.get("saved_path")],
        dict(session_manifest.get("delivery") or {}),
    )


def _normalize_thinking_level(value: str | None, text: str = "") -> str:
    explicit = (value or "").strip().lower()
    if explicit in {"high", "deep", "高", "高思考", "深度", "重度"}:
        return "High"
    if explicit in {"low", "light", "低", "低思考", "轻度"}:
        return "Low"
    if explicit in {"medium", "balanced", "中", "中等", "中度"}:
        return "Medium"
    haystack = f"{explicit}\n{text}".lower()
    if any(
        token in haystack
        for token in (
            "thinking level high",
            "high thinking",
            "deep reasoning",
            "思考程度 高",
            "思考程度高",
            "高思考",
            "深度思考",
        )
    ):
        return "High"
    if any(
        token in haystack
        for token in (
            "thinking level low",
            "low thinking",
            "light reasoning",
            "思考程度 低",
            "思考程度低",
            "低思考",
            "轻度思考",
        )
    ):
        return "Low"
    if any(
        token in haystack
        for token in (
            "thinking level medium",
            "medium thinking",
            "balanced reasoning",
            "思考程度 中",
            "思考程度中",
            "中等思考",
        )
    ):
        return "Medium"
    return os.getenv("CASSETTE_DEFAULT_THINKING_LEVEL", "Low")


def _cassette_model_selection(args: dict, delivery: dict | None = None) -> dict:
    session_id = str(args.get("session_id") or "").strip()
    platform = _normalize_platform_name((delivery or {}).get("platform"))
    if session_id and platform in {"qqbot", "weixin", "telegram", "web"}:
        preference = _cassette_model_preference_for_session(session_id)
        if preference:
            return {**preference, "source": "session_preference"}
    text = "\n".join(str(args.get(key) or "") for key in ("chat_message", "cassette_message", "instruction", "prompt"))
    model = (args.get("cassette_model") or args.get("model") or "").strip()
    thinking_level = _normalize_thinking_level(args.get("thinking_level"), text)
    return {
        "model": model,
        "thinking_level": thinking_level,
        "source": "user_or_default"
        if args.get("cassette_model") or args.get("model") or args.get("thinking_level")
        else "default",
    }


def _gateway_background_jobs_enabled() -> bool:
    return os.getenv("CASSETTE_GATEWAY_BACKGROUND_JOBS", "true").lower() not in {"0", "false", "no", "off"}


def _gateway_background_next_step() -> str:
    return (
        "Gateway Cassette job is running in the plugin background. Tell the user the Cassette job has started, "
        "then stop this Hermes turn. Do not call cassette_job_status repeatedly; the plugin sends progress "
        "screenshots and terminal notifications through the stored gateway delivery target. Only call "
        "cassette_job_status if the user explicitly asks for status."
    )


def _status_poll_min_interval_sec() -> int:
    try:
        return max(60, int(os.getenv("CASSETTE_STATUS_POLL_MIN_INTERVAL_SEC", "180")))
    except ValueError:
        return 180


def _is_gateway_background_job(job: dict) -> bool:
    quality = job.get("quality") or {}
    return bool(quality.get("gateway_background_job"))


def _gateway_job_executor() -> ThreadPoolExecutor:
    global _GATEWAY_JOB_EXECUTOR
    with _GATEWAY_JOB_EXECUTOR_LOCK:
        if _GATEWAY_JOB_EXECUTOR is None:
            _GATEWAY_JOB_EXECUTOR = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="cassette-gateway-job",
            )
        return _GATEWAY_JOB_EXECUTOR


def _should_run_gateway_job_in_background(args: dict, delivery: dict | None) -> bool:
    if not _gateway_background_jobs_enabled():
        return False
    platform = _normalize_platform_name((delivery or {}).get("platform"))
    if platform not in {"qqbot", "weixin", "telegram", "web"}:
        return False
    return bool((delivery or {}).get("chat_id"))


def _finish_background_cassette_job(job_id: str, action: str = "run", response: str = "") -> None:
    try:
        if jobs.is_cancel_requested(job_id):
            job = jobs.update_job(job_id, status="cancelled", finished_at=jobs.now_iso())
            job["notification"] = notifier.notify_terminal_job(job)
            jobs.save_job(job)
            return
        job = jobs.update_job(
            job_id,
            status="running",
            started_at=jobs.now_iso(),
            finished_at=None,
            worker_kind="thread",
        )
        active_transport = transport.get_transport()
        result = active_transport.resume(job, response) if action == "resume" else active_transport.run_job(job)
        job = jobs.merge_persisted_runtime_fields(job)
        job.update(result)
        job["status"] = result.get("status", "failed")
        job["finished_at"] = jobs.now_iso()
        job.pop("resume_request", None)
        if job["status"] != "needs_user":
            job.pop("continuation", None)
        jobs.save_job(job)
        job["notification"] = notifier.notify_terminal_job(job)
        jobs.save_job(job)
    except Exception as exc:
        try:
            job = jobs.update_job(
                job_id,
                status="failed",
                errors=[{"code": "internal_error", "message": str(exc), "details": {"type": type(exc).__name__}}],
                finished_at=jobs.now_iso(),
            )
            job["notification"] = notifier.notify_terminal_job(job)
            jobs.save_job(job)
        except Exception:
            pass


def _start_inprocess_cassette_job(job: dict, *, runtime_host: str = "gateway") -> dict:
    job["status"] = "running"
    job["started_at"] = job.get("started_at") or jobs.now_iso()
    job["worker_kind"] = "thread"
    quality = dict(job.get("quality") or {})
    if runtime_host == "mcp":
        quality["mcp_background_job"] = True
    else:
        quality["gateway_background_job"] = True
    quality["interruptible_by_cut"] = True
    job["quality"] = quality
    jobs.save_job(job)
    _gateway_job_executor().submit(_finish_background_cassette_job, job["job_id"])
    return job


@safe_tool
def cassette_run_job(a: dict, **kw) -> str:
    prompt_text = (a.get("prompt") or "").strip()
    chat_message = (a.get("chat_message") or a.get("cassette_message") or "").strip()
    if not prompt_text and chat_message:
        prompt_text = chat_message
    if not prompt_text:
        raise CassetteError("missing_required_arg", "prompt is required")
    raw_session_id = str(a.get("session_id") or kw.get("task_id") or "").strip()
    session_hash, asset_paths, delivery = _asset_paths_for_session(
        a.get("session_id"), a.get("chat_id"), kw.get("task_id")
    )
    if raw_session_id:
        active_job = _latest_active_job_for_session(raw_session_id)
        if active_job:
            raise CassetteError(
                "cassette_job_already_running",
                _fixed_flow_busy_message(_cassette_language_for_run(a, delivery)),
                {"job_id": active_job.get("job_id") or "", "status": active_job.get("status") or ""},
                True,
            )
    job = jobs.create_job(
        session_hash=session_hash,
        prompt=prompt_text,
        instruction=a.get("instruction"),
        asset_paths=asset_paths,
        options={
            "chat_message": chat_message,
            "url": a.get("url"),
            "timeout_sec": a.get("timeout_sec"),
            "selectors": a.get("selectors") or {},
            "delivery": delivery,
            "cassette_session_id": a.get("session_id"),
            "model_selection": _cassette_model_selection(a, delivery),
            "cassette_language": _cassette_language_for_run(a, delivery),
        },
    )
    if _should_run_gateway_job_in_background(a, delivery):
        job = _start_inprocess_cassette_job(job)
        scrubbed = _scrub_job(job)
        return ok(
            {
                "job": scrubbed,
                "background": True,
                "hermes_next_step": _gateway_background_next_step(),
            },
            job_id=job["job_id"],
        )
    if (
        kw.get("runtime_host") == "mcp"
        and a.get("wait", False) is False
        and transport.selected_transport() == transport.TRANSPORT_BROWSER
    ):
        job = _start_inprocess_cassette_job(job, runtime_host="mcp")
        return ok(
            {"job": _scrub_job(job), "background": True},
            job_id=job["job_id"],
        )
    if a.get("wait", True) is False:
        job = jobs.start_worker(job["job_id"])
        scrubbed = _scrub_job(job)
        return ok({"job": scrubbed}, job_id=job["job_id"])

    job["status"] = "running"
    job["started_at"] = job.get("started_at") or jobs.now_iso()
    jobs.save_job(job)
    result = transport.get_transport().run_job(job)
    job = jobs.merge_persisted_runtime_fields(job)
    job.update(result)
    job["status"] = result.get("status", "failed")
    job["finished_at"] = jobs.now_iso()
    jobs.save_job(job)
    job["notification"] = notifier.notify_terminal_job(job)
    jobs.save_job(job)
    return ok({"job": _scrub_job(job)}, job_id=job["job_id"])


def _scrub_job(job: dict) -> dict:
    scrubbed = dict(job)
    scrubbed.pop("prompt", None)
    scrubbed.pop("asset_paths", None)
    scrubbed.pop("worker_command", None)
    scrubbed.pop("continuation", None)
    scrubbed.pop("resume_request", None)
    scrubbed["outputs"] = _scrub_outputs(scrubbed.get("outputs") or [])
    if scrubbed.get("model_selection_notification"):
        notification = dict(scrubbed["model_selection_notification"])
        notification.pop("error", None)
        scrubbed["model_selection_notification"] = notification
    delivery = scrubbed.pop("delivery", None) or {}
    if delivery:
        scrubbed["delivery"] = {
            "platform": delivery.get("platform") or "",
            "chat_hash": safe_hash_id(delivery.get("chat_id")),
            "user_hash": safe_hash_id(delivery.get("user_id")),
            "has_raw_target": bool(delivery.get("chat_id")),
            "has_thread": bool(delivery.get("thread_id")),
        }
    scrubbed["report"] = _job_report(scrubbed)
    return scrubbed


def _scrub_outputs(outputs: list) -> list:
    scrubbed_outputs: list[dict] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        cleaned = {k: v for k, v in output.items() if k != "local_path"}
        if output.get("local_path"):
            cleaned["downloaded"] = True
            cleaned["filename"] = Path(str(output.get("local_path"))).name
        scrubbed_outputs.append(cleaned)
    return scrubbed_outputs


def _job_report(job: dict) -> dict:
    status = job.get("status") or "unknown"
    quality = job.get("quality") or {}
    is_gateway_background = _is_gateway_background_job(job)
    progress_events = job.get("progress_events") or []
    latest_progress = progress_events[-1] if progress_events else {}
    latest_progress_summary = latest_progress.get("summary") or quality.get("progress_summary", "")
    output_count = len(job.get("outputs") or job.get("output_links") or [])
    export_pending = bool(quality.get("export_pending")) or (status == "succeeded" and output_count == 0)
    if status == "succeeded" and export_pending:
        user_summary = "Cassette chat panel indicates the edit is complete, but no exported video was recorded."
    elif status == "succeeded":
        user_summary = "Cassette edit completed and exported output is available."
    elif status == "timed_out":
        user_summary = "Cassette did not expose a completion signal before timeout. Check the latest progress summary before treating this as failure."
    elif status == "needs_user":
        user_summary = "Cassette needs user input before it can continue."
    elif status == "cancelled":
        user_summary = "Cassette job was paused by request; browser state is preserved for retry or follow-up editing instructions."
    elif status == "running" and is_gateway_background:
        user_summary = (
            "Cassette is running in the background. The plugin will send progress screenshots and the final gateway "
            "notification; do not poll repeatedly unless the user explicitly asks for status."
        )
    elif status == "running":
        user_summary = "Cassette is still working."
    elif status == "failed":
        codes = ", ".join(e.get("code", "unknown") for e in job.get("errors", [])) or "unknown"
        user_summary = f"Cassette job failed with error code(s): {codes}."
    else:
        user_summary = f"Cassette job status is {status}."
    report = {
        "status": status,
        "user_summary": user_summary,
        "latest_progress": latest_progress_summary,
        "output_count": output_count,
        "export_pending": export_pending,
        "current_stage": job.get("current_stage") or quality.get("current_stage") or "",
        "stage_timings": job.get("stage_timings") or quality.get("stage_timings") or {},
        "progress_snapshot_count": len(job.get("progress_snapshot_notifications") or []),
        "model_selection": job.get("model_selection") or {},
    }
    if is_gateway_background and status == "running":
        report["background"] = True
        report["next_check_after_sec"] = _status_poll_min_interval_sec()
        report["polling_advice"] = _gateway_background_next_step()
    return report


@safe_tool
def cassette_job_status(a: dict, **kw) -> str:
    if a.get("job_id"):
        job = jobs.load_job(a["job_id"])
        data = {"job": _scrub_job(job)}
        if _is_gateway_background_job(job) and _is_active_job(job):
            data["background"] = True
            data["hermes_next_step"] = _gateway_background_next_step()
        return ok(data, job_id=a["job_id"])
    sess_hash = None
    if a.get("session_id"):
        sess_hash = manifest.resolve_session_hash(a.get("session_id"), None, kw.get("task_id"))
    return ok({"jobs": jobs.list_jobs(sess_hash, int(a.get("limit") or 10))})


@safe_tool
def cassette_review_completion(a: dict, **kw) -> str:
    del kw
    job_id = str(a.get("job_id") or "").strip()
    if not job_id:
        raise CassetteError("missing_required_arg", "job_id is required")
    decision = str(a.get("decision") or "").strip().lower()
    if decision not in {"export", "continue", "needs_user", "failed"}:
        raise CassetteError("missing_required_arg", "decision must be one of export, continue, needs_user, or failed")
    reason = redact_for_log(str(a.get("reason") or "").strip())
    summary = redact_for_log(str(a.get("summary") or "").strip())
    if not reason:
        raise CassetteError("missing_required_arg", "reason is required")
    job = jobs.load_job(job_id)
    review = {"decision": decision, "reason": reason, "summary": summary}
    if decision == "export":
        result = transport.get_transport().export(job, review)
        job = jobs.merge_persisted_runtime_fields(job)
        job.update(result)
        job["status"] = result.get("status", "failed")
        job["finished_at"] = jobs.now_iso()
        jobs.save_job(job)
        job["notification"] = notifier.notify_terminal_job(job)
        jobs.save_job(job)
        return ok({"job": _scrub_job(job), "completion_review": review}, job_id=job_id)

    questions = list(job.get("questions") or [])
    errors = list(job.get("errors") or [])
    quality = dict(job.get("quality") or {})
    quality["completion_review"] = review
    quality["completion_source"] = "hermes_completion_review"
    quality["progress_summary"] = summary or reason
    if decision == "failed":
        errors.append(
            {
                "code": "hermes_completion_review_failed",
                "message": "Hermes judged that Cassette did not complete the edit.",
                "details": {"reason": reason},
            }
        )
        job.update({"status": "failed", "errors": errors, "quality": quality, "finished_at": jobs.now_iso()})
    else:
        questions.append(
            {
                "question": summary or reason,
                "requires_user": decision == "needs_user",
                "reason": f"hermes_completion_review_{decision}",
                "answer": reason,
            }
        )
        job.update({"status": "needs_user", "questions": questions, "quality": quality, "finished_at": jobs.now_iso()})
    jobs.save_job(job)
    job["notification"] = notifier.notify_terminal_job(job)
    jobs.save_job(job)
    return ok({"job": _scrub_job(job), "completion_review": review}, job_id=job_id)


@safe_tool
def cassette_cancel_job(a: dict, **kw) -> str:
    if not a.get("job_id"):
        raise CassetteError("missing_required_arg", "job_id is required")
    job = jobs.request_cancel(a["job_id"])
    return ok({"job_id": job["job_id"], "status": job["status"]}, job_id=job["job_id"])


def _previews_dir(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:120] or "session"
    return Path(os.getenv("CASSETTE_ASSET_ROOT", str(manifest.get_asset_root()))) / "previews" / safe


def build_contact_sheet(document: dict, session_id: str) -> str | None:
    """Tile stored clip poster thumbnails (base64 data URIs on the document) into one jpeg.

    Zero server render: the posters were captured at upload time. Returns None when there is
    nothing to tile or ffmpeg is unavailable — the CTL text always stands on its own."""
    import base64
    import shutil
    import subprocess

    from . import timeline as timeline_mod

    try:
        clips = [
            c
            for c in timeline_mod.clips_in_timeline_order(document)
            if isinstance(c.get("thumbnail"), str) and c["thumbnail"].startswith("data:image")
        ][:8]
        if not clips:
            return None
        ffmpeg = os.getenv("CASSETTE_FFMPEG_BIN", "ffmpeg")
        if not shutil.which(ffmpeg):
            return None
        out_dir = _previews_dir(session_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"sheet-v{document.get('version', 0)}.jpg"
        if target.exists() and target.stat().st_size:
            return str(target)  # per-version sheets are immutable
        with tempfile.TemporaryDirectory(dir=str(out_dir)) as tmp:
            for index, clip in enumerate(clips):
                _, _, b64 = str(clip["thumbnail"]).partition(",")
                (Path(tmp) / f"in_{index:02d}.jpg").write_bytes(base64.b64decode(b64 or "", validate=False))
            cols = min(4, len(clips))
            rows = -(-len(clips) // cols)
            subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-y",
                    "-framerate",
                    "1",
                    "-i",
                    str(Path(tmp) / "in_%02d.jpg"),
                    "-vf",
                    (
                        "scale=320:180:force_original_aspect_ratio=decrease,"
                        "pad=320:180:(ow-iw)/2:(oh-ih)/2:color=0x07080b,"
                        f"tile={cols}x{rows}:padding=4:color=0x07080b"
                    ),
                    "-frames:v",
                    "1",
                    str(target),
                ],
                capture_output=True,
                timeout=30,
            )
        return str(target) if target.exists() and target.stat().st_size else None
    except Exception:  # noqa: BLE001 — the sheet is an enhancement, never a failure mode
        return None


# Curated no-LLM direct-edit surface: public editor tools whose inputs are self-contained
# (no media resolution, no generation). The server statically validates every input against
# TOOL_INPUT_JSON_SCHEMAS, so a wrong payload returns a precise validation message.
_DIRECT_EDIT_TOOLS = {
    "timeline_trim": "trim/retime a clip",
    "timeline_arrange": "move/reorder clips",
    "timeline_deleteClips": "delete clips",
    "timeline_addTextClip": "add a text/title clip",
    "timeline_text": "edit text content",
    "timeline_textStyle": "style a text clip",
    "timeline_textLayout": "lay out a text clip",
    "timeline_properties": "clip properties (volume/speed/opacity/...)",
    "timeline_filter": "apply/adjust filters",
    "timeline_effect": "apply/adjust effects",
    "timeline_keyframe": "keyframes",
    "timeline_audio": "audio operations",
    "timeline_track": "track operations",
    "timeline_transition": "transitions",
}
_UNDO_TOOL = "set-operation-history-cursor"


def _direct_edit_enabled() -> bool:
    return str(os.getenv("CASSETTE_DIRECT_EDIT", "") or "").strip().lower() in {"1", "true", "yes", "on"}


@safe_tool
def cassette_edit(a: dict, **kw) -> str:
    """Surgical no-LLM timeline edit through the manual-editor command lane (flagged)."""
    if not _direct_edit_enabled():
        raise CassetteError(
            "direct_edit_disabled",
            "Direct edits are disabled. Set CASSETTE_DIRECT_EDIT=1 to enable cassette_edit.",
            recoverable=False,
        )
    session_id = str(a.get("session_id") or "").strip()
    tool_name = str(a.get("tool_name") or "").strip()
    if not session_id or not tool_name:
        raise CassetteError("missing_required_arg", "session_id and tool_name are required")

    # One session, one writer: refuse while a run (or an unanswered question) holds the session.
    # ponytail: advisory client-side guard; a server-side session lease only if two hosts ever
    # share one session concurrently.
    active = [
        j
        for j in jobs.list_jobs(None, limit=20)
        if j.get("cassette_session_id") == session_id
        and j.get("status") in {"queued", "running", "needs_user", "cancel_requested"}
    ]
    if active:
        raise CassetteError(
            "job_active",
            f"Job {active[0]['job_id']} ({active[0]['status']}) holds this session; wait for it or cancel it first.",
        )

    from . import api_transport as api_mod
    from . import timeline as timeline_mod

    transport = api_mod.ApiTransport()
    before = transport.get_project_document(session_id)
    current_version = int(before.get("version") or 0)
    expected_version = a.get("expected_version")
    if expected_version is not None and int(expected_version) != current_version:
        raise CassetteError(
            "stale_timeline",
            f"The project moved to v{current_version} since you last read it. Re-read and retry.",
            details={"ctl": timeline_mod.render_ctl(before)},
        )

    import uuid as _uuid

    if tool_name in {_UNDO_TOOL, "undo"}:
        cursor_raw = (a.get("input") or {}).get("cursorSequence") if isinstance(a.get("input"), dict) else None
        if cursor_raw is None:
            raise CassetteError("missing_required_arg", "undo requires input.cursorSequence")
        command: dict[str, Any] = {"type": _UNDO_TOOL, "cursorSequence": int(cursor_raw)}
        envelope = {"commandId": str(_uuid.uuid4()), "source": "agent", "command": command}
    else:
        if tool_name not in _DIRECT_EDIT_TOOLS:
            raise CassetteError(
                "unknown_tool",
                f"'{tool_name}' is not a direct-edit tool.",
                details={"tools": _DIRECT_EDIT_TOOLS, "undo": _UNDO_TOOL},
            )
        if not isinstance(a.get("input"), dict):
            raise CassetteError("missing_required_arg", 'input is required and always shaped {"payload": {...}}')
        command = {"type": "agent-tool", "toolName": tool_name, "input": a["input"]}
        envelope = {
            "commandId": str(_uuid.uuid4()),
            "source": "agent",
            "toolName": tool_name,
            "command": command,
        }

    event = transport.post_project_command(session_id, envelope)
    after = event.get("document") if isinstance(event.get("document"), dict) else None
    data: dict[str, Any] = {
        "version_before": event.get("versionBefore", current_version),
        "version_after": event.get("versionAfter"),
    }
    if after:
        data["delta"] = timeline_mod.render_delta(before, after)
        data["ctl"] = timeline_mod.render_ctl(after)
    return ok(data)


@safe_tool
def cassette_timeline(a: dict, **kw) -> str:
    """Read the live project timeline as a bounded text digest (+ optional contact sheet)."""
    session_id = str(a.get("session_id") or "").strip()
    if not session_id:
        raise CassetteError("missing_required_arg", "session_id is required")
    from . import api_transport as api_mod
    from . import timeline as timeline_mod

    document = api_mod.ApiTransport().get_project_document(session_id)
    profile = str(a.get("profile") or "").strip().lower()
    detail = str(a.get("detail") or "").strip() or None
    if profile == "gateway":
        ctl = timeline_mod.render_ctl_gateway(document)
    else:
        ctl = timeline_mod.render_ctl(document, detail=detail)
    data: dict[str, Any] = {
        "ctl": ctl,
        "version": document.get("version", 0),
        "duration_sec": round(timeline_mod.total_duration_seconds(document), 1),
        "clip_count": len(timeline_mod.clips_in_timeline_order(document)),
    }
    if a.get("contact_sheet"):
        sheet = build_contact_sheet(document, session_id)
        if sheet:
            data["contact_sheet_path"] = sheet
        else:
            data["contact_sheet_path"] = None
            data["contact_sheet_note"] = "no clip posters available yet (or ffmpeg missing)"
    return ok(data)


def check_playwright() -> bool:
    # Transport-readiness gate: under the browser transport this checks Playwright;
    # under the API transport it checks that the API base URL + credentials are configured.
    return transport.get_transport().check_available()


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


def _platform_value(source: Any) -> str:
    return str(
        getattr(getattr(source, "platform", None), "value", None) or getattr(source, "platform", None) or "gateway"
    )


def _normalize_platform_name(platform: Any) -> str:
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


def _normalize_cassette_language(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = raw.replace("_", "-")
    if raw in {"zh", "zh-cn", "cn", "chinese", "mandarin", "中文", "汉语", "简体中文"}:
        return "zh"
    if raw in {"en", "en-us", "en-gb", "english", "英文", "英语"}:
        return "en"
    return ""


def _default_cassette_language_for_platform(platform: Any) -> str:
    return "en" if _normalize_platform_name(platform) == "telegram" else "zh"


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


def _gateway_asset_count(session_id: str) -> int:
    try:
        data = manifest.list_assets(session_id=session_id)
    except CassetteError:
        return 0
    except Exception:
        return 0
    assets = data.get("manifest", {}).get("assets", [])
    return len([asset for asset in assets if asset.get("exists", True)])


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


def _fixed_flow_busy_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return "Use /cut to stop the current flow or edit job before starting a new edit task."
    return "请使用/cut命令终止当前流程或剪辑任务后再尝试开始新的剪辑任务"


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


_ACTIVE_JOB_STATUSES = {"queued", "running", "cancel_requested"}


def _is_active_job(job: dict) -> bool:
    return str(job.get("status") or "") in _ACTIVE_JOB_STATUSES


def _gateway_session_chat_prefix(session_id: str) -> str:
    value = str(session_id or "").strip()
    if not value:
        return ""
    match = re.match(r"^(gateway_media_[^_]+_[0-9a-f]{16})(?:_[0-9a-f]{16})?$", value)
    if match:
        return match.group(1)
    return value


def _job_matches_gateway_session(job: dict, session_id: str, session_hash: str | None = None) -> bool:
    if session_hash and str(job.get("session_hash") or "") == session_hash:
        return True
    cassette_session_id = str(job.get("cassette_session_id") or "")
    if cassette_session_id and cassette_session_id == str(session_id or ""):
        return True
    chat_prefix = _gateway_session_chat_prefix(session_id)
    return bool(
        chat_prefix
        and cassette_session_id
        and (cassette_session_id == chat_prefix or cassette_session_id.startswith(f"{chat_prefix}_"))
    )


def _iter_recent_raw_jobs(limit: int = 50) -> list[dict]:
    try:
        recent = jobs.list_jobs(limit=limit)
    except Exception:
        return []
    raw_jobs: list[dict] = []
    for item in recent:
        job_id = str(item.get("job_id") or "")
        if not job_id:
            continue
        try:
            raw_jobs.append(jobs.load_job(job_id))
        except Exception:
            continue
    return raw_jobs


def _job_matches_gateway_delivery(job: dict, source: Any) -> bool:
    delivery = job.get("delivery") or {}
    if not isinstance(delivery, dict) or source is None:
        return False
    delivery_platform = _normalize_platform_name(delivery.get("platform"))
    source_platform = _normalize_platform_name(_platform_value(source))
    if delivery_platform and source_platform and delivery_platform != source_platform:
        return False
    source_chat = getattr(source, "chat_id", None)
    delivery_chat = delivery.get("chat_id")
    if source_chat is None or delivery_chat is None:
        return False
    if str(source_chat) != str(delivery_chat):
        return False
    source_thread = getattr(source, "thread_id", None)
    delivery_thread = delivery.get("thread_id")
    if source_thread and delivery_thread and str(source_thread) != str(delivery_thread):
        return False
    return True


def _latest_active_job_for_session(session_id: str, source: Any = None) -> dict | None:
    sess_hash = None
    try:
        sess_hash = manifest.resolve_session_hash(session_id=session_id)
        recent = jobs.list_jobs(sess_hash, limit=20)
    except Exception:
        recent = []
    for job in recent:
        if _is_active_job(job):
            return job
    try:
        fallback_recent = jobs.list_jobs(limit=50)
    except Exception:
        fallback_recent = []
    for job in fallback_recent:
        if _is_active_job(job) and _job_matches_gateway_session(job, session_id, sess_hash):
            return job
    if source is not None:
        for job in _iter_recent_raw_jobs(limit=50):
            if _is_active_job(job) and _job_matches_gateway_delivery(job, source):
                cleaned = dict(job)
                cleaned.pop("prompt", None)
                cleaned.pop("asset_paths", None)
                cleaned.pop("worker_command", None)
                cleaned.pop("delivery", None)
                return cleaned
    return None


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


def _cassette_model_preference_for_session(session_id: str) -> dict[str, str]:
    prefs = _load_session_preferences(session_id)
    model = str(prefs.get("cassette_model") or "").strip()
    thinking = _normalize_thinking_level(str(prefs.get("cassette_thinking_level") or ""))
    if not model or not prefs.get("cassette_model_selection_completed"):
        return {}
    return {"model": model, "thinking_level": thinking or "Low"}


def _cassette_model_selection_completed(session_id: str) -> bool:
    return bool(_cassette_model_preference_for_session(session_id))


def _gateway_model_choice_enabled() -> bool:
    return os.getenv("CASSETTE_GATEWAY_MODEL_CHOICE_ENABLED", "true").lower() not in {"0", "false", "no", "off"}


def _save_cassette_model_preference(session_id: str, model: str, thinking_level: str, *, source: str) -> dict:
    model = str(model or "").strip()
    thinking = _normalize_thinking_level(thinking_level)
    if not model:
        raise CassetteError("invalid_cassette_model", "Cassette model is required")
    return _save_session_preferences(
        session_id,
        cassette_model=model,
        cassette_thinking_level=thinking,
        cassette_model_selection_completed=True,
        cassette_model_selection_source=source,
    )


def _cassette_model_options(language: str = "zh") -> dict[str, Any]:
    options = browser.fetch_cassette_model_options(language=language)
    if not options.get("models"):
        raise CassetteError(
            "cassette_model_options_empty",
            "Cassette model options were not available from the Cassette page.",
            {"source": options.get("source") or "cassette_agent_page"},
        )
    return options


def _model_options_error_details(exc: Exception) -> dict[str, str]:
    code = getattr(exc, "code", None) or type(exc).__name__
    message = redact_for_log(str(exc) or code)
    return {"code": str(code), "message": message}


def _cassette_model_options_unavailable_message(language: str = "zh", error: dict[str, str] | None = None) -> str:
    detail = ""
    if error:
        code = error.get("code") or "unknown"
        message = error.get("message") or ""
        detail = f" ({code}: {message})" if message and message != code else f" ({code})"
    if _normalize_cassette_language(language) == "en":
        return f"Cassette model options are unavailable because the Cassette page could not provide them{detail}. Please retry after Cassette is reachable and logged in."
    return f"暂时无法从 Cassette 页面获取模型列表{detail}。请确认 Cassette 可访问且已登录后重试。"


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
    resume_after_model: str = "edit",
    optimization_enabled: bool | None = None,
) -> dict:
    try:
        options = _cassette_model_options(language)
    except Exception as exc:
        error_details = _model_options_error_details(exc)
        reply_sent = _send_gateway_fixed_reply(
            gateway, event, _cassette_model_options_unavailable_message(language, error_details)
        )
        return {
            "action": "skip",
            "reason": "cassette_model_options_unavailable",
            "asset_count": asset_count,
            "session_id": session_id,
            "reply_sent": reply_sent,
            "error": error_details,
        }
    pending_extra: dict[str, Any] = {
        "model_options": options.get("models") or [],
        "thinking_options": options.get("thinking_levels") or [],
        "model_options_source": options.get("source") or "",
        "language": _normalize_cassette_language(language) or "zh",
        "resume_after_model": resume_after_model,
    }
    if optimization_enabled is not None:
        pending_extra["optimization_enabled"] = bool(optimization_enabled)
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
    resume_after_model = str(pending_edit.get("resume_after_model") or "edit")
    _save_cassette_model_preference(
        session_id,
        selected_model,
        selected_thinking,
        source="gateway_command" if resume_after_model == "command" else "initial_edit",
    )
    if resume_after_model == "command":
        _clear_pending_edit(session_id)
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
    instruction = str(pending_edit.get("instruction") or "").strip()
    asset_count = int(pending_edit.get("asset_count") or 0)
    if resume_after_model == "semantic_edit":
        return _request_prompt_optimization_choice(session_id, instruction, asset_count, gateway, event, language)
    if resume_after_model == "semantic_optimization_choice":
        return _request_smart_bgm_choice(
            session_id,
            instruction,
            asset_count,
            gateway,
            event,
            optimization_enabled=bool(pending_edit.get("optimization_enabled")),
            language=language,
        )
    if resume_after_model == "refine":
        _mark_initial_edit_choices_completed(
            session_id,
            optimization_enabled=True,
            smart_bgm_enabled=False,
            source="refine_command",
        )
        return _rewrite_accepted_prompt_optimization(session_id, instruction, asset_count, language=language)
    if _initial_edit_choices_completed(session_id):
        return _rewrite_direct_original_instruction(
            session_id, instruction, asset_count, direct_reason="session_default", language=language
        )
    return _request_prompt_optimization_choice(session_id, instruction, asset_count, gateway, event, language)


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


def _looks_like_bgm_decline(text: str) -> bool:
    normalized = (text or "").strip().lower().strip("。.!！ ")
    if not normalized:
        return False
    decline_terms = (
        "否",
        "不用",
        "不要",
        "不需要",
        "不加",
        "不用bgm",
        "不要bgm",
        "不需要bgm",
        "不用配乐",
        "不要配乐",
        "不需要配乐",
        "跳过",
        "跳过bgm",
        "直接继续",
        "直接开始",
        "no",
        "n",
        "skip",
        "without bgm",
        "no bgm",
        "no music",
        "do not add music",
    )
    return any(term in normalized for term in decline_terms)


def _looks_like_bgm_accept(text: str) -> bool:
    normalized = (text or "").strip().lower().strip("。.!！ ")
    if not normalized:
        return False
    if _looks_like_bgm_decline(normalized):
        return False
    exact_accept_terms = {
        "是",
        "要",
        "需要",
        "使用",
        "加",
        "加上",
        "可以",
        "好的",
        "好",
        "yes",
        "y",
        "ok",
        "okay",
    }
    if normalized in exact_accept_terms:
        return True
    accept_phrases = (
        "bgm",
        "配乐",
        "背景音乐",
        "智能配乐",
        "智能匹配",
        "匹配一首",
        "匹配音乐",
        "帮我配",
        "帮我匹配",
        "use music",
        "use bgm",
        "add music",
        "add bgm",
        "background music",
    )
    return any(term in normalized for term in accept_phrases)


def _pending_edit_path(session_id: str) -> Path:
    sess_hash = manifest.resolve_session_hash(session_id=session_id)
    return manifest.get_session_dir(sess_hash) / "pending_edit.json"


def _session_preferences_path(session_id: str) -> Path:
    sess_hash = manifest.resolve_session_hash(session_id=session_id)
    return manifest.get_session_dir(sess_hash) / "session_preferences.json"


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".pending-edit.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _save_pending_edit(
    session_id: str,
    instruction: str,
    asset_count: int,
    state: str = "awaiting_optimization_choice",
    **extra: Any,
) -> dict:
    data = {
        "state": state,
        "instruction": instruction,
        "asset_count": asset_count,
        "updated_at": jobs.now_iso(),
    }
    data.update(extra)
    _write_json_atomic(_pending_edit_path(session_id), data)
    return data


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
    if not str(data.get("instruction") or "").strip() and state not in {
        "awaiting_model_choice",
        "awaiting_model_thinking_choice",
    }:
        return None
    return data


def _clear_pending_edit(session_id: str) -> None:
    try:
        _pending_edit_path(session_id).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _load_session_preferences(session_id: str) -> dict:
    path = _session_preferences_path(session_id)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_session_preferences(session_id: str, **updates: Any) -> dict:
    data = _load_session_preferences(session_id)
    data.update(updates)
    data["updated_at"] = jobs.now_iso()
    _write_json_atomic(_session_preferences_path(session_id), data)
    return data


def _manifest_delivery_platform_for_session(session_id: str) -> str:
    try:
        listed = manifest.list_assets(session_id=session_id)
        delivery = dict((listed.get("manifest") or {}).get("delivery") or {})
    except Exception:
        return ""
    return _normalize_platform_name(delivery.get("platform"))


def _cassette_language_for_session(session_id: str, platform: Any = None) -> str:
    configured = _normalize_cassette_language(_load_session_preferences(session_id).get("cassette_language"))
    if configured:
        return configured
    effective_platform = platform or _manifest_delivery_platform_for_session(session_id)
    return _default_cassette_language_for_platform(effective_platform)


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


def _cassette_language_for_run(args: dict, delivery: dict | None = None) -> str:
    explicit = _normalize_cassette_language(args.get("cassette_language") or args.get("language"))
    if explicit:
        return explicit
    session_id = str(args.get("session_id") or "").strip()
    platform = (delivery or {}).get("platform")
    if session_id:
        return _cassette_language_for_session(session_id, platform)
    return _default_cassette_language_for_platform(platform)


def _initial_edit_choices_completed(session_id: str) -> bool:
    return bool(_load_session_preferences(session_id).get("initial_edit_choices_completed"))


def _mark_initial_edit_choices_completed(
    session_id: str,
    *,
    optimization_enabled: bool,
    smart_bgm_enabled: bool,
    source: str,
) -> None:
    _save_session_preferences(
        session_id,
        initial_edit_choices_completed=True,
        prompt_optimization_enabled=bool(optimization_enabled),
        smart_bgm_enabled=bool(smart_bgm_enabled),
        initial_choice_source=source,
    )


def _prompt_optimization_choice_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return (
            "Would you like me to optimize your edit instruction into a more professional edit instruction first? "
            'Reply "optimize/yes" and I will optimize it and ask for confirmation; reply "no/no optimization" and I will send your original instruction directly to Cassette.'
        )
    return (
        "是否需要我先把你的剪辑指令优化成更专业的剪辑指令？"
        "回复“优化/是”我会先优化并发给你确认；回复“不优化/否”我会按原指令直接交给 Cassette 开始处理。"
    )


def _smart_bgm_choice_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return (
            "Would you like smart BGM matching based on the edit instruction? "
            'Reply "yes/need BGM" and I will recommend 3 songs for you to choose from; reply "no/no BGM" and I will continue the workflow.'
        )
    return (
        "是否需要我根据剪辑指令智能匹配一首 BGM？"
        "回复“需要/是”我会先推荐 3 首歌供你选择；回复“不需要/否”我会继续后续流程。"
    )


def _language_name_for_prompt(language: str) -> str:
    return "English" if _normalize_cassette_language(language) == "en" else "Chinese"


def _prompt_optimization_choice_guard(language: str = "zh") -> str:
    return (
        "Before starting Cassette, ask the user whether to use the prompt optimization feature. "
        "Do not optimize the edit brief yet. Do not call cassette_list_assets, cassette_make_prompt, or cassette_run_job yet. "
        f"Ask exactly this question in {_language_name_for_prompt(language)}: "
        f"{_prompt_optimization_choice_message(language)} "
        "If the user replies negatively, the plugin will provide the original instruction for direct Cassette execution. "
        "If the user replies affirmatively, the plugin will ask you to optimize the original instruction."
    )


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
    return Path(__file__).resolve().parent / "prompts" / "hermes-edit-brief-optimizer.md"


def _prompt_optimization_guard() -> str:
    try:
        doc = _prompt_optimization_doc_path().read_text(encoding="utf-8").strip()
    except OSError:
        return _default_prompt_optimization_guard()
    return doc or _default_prompt_optimization_guard()


def _cassette_run_job_tool_chain_guard(language: str = "zh") -> str:
    language = _normalize_cassette_language(language) or "zh"
    return (
        "Use the normal Cassette job path: call cassette_list_assets, then cassette_make_prompt, then cassette_run_job. "
        f"When calling cassette_make_prompt and cassette_run_job, pass cassette_language='{language}'. "
        "When calling cassette_run_job, pass prompt=cassette_make_prompt.data.prompt and chat_message=cassette_make_prompt.data.chat_message, with the same cassette session_id. "
        "For QQ, Telegram, or Weixin gateway sessions, call cassette_run_job with wait=false so gateway commands such as /cut remain responsive while the plugin sends progress and final notifications through the stored delivery target. "
        "After cassette_run_job returns a gateway background job, tell the user the Cassette job has started and end the turn; do not call cassette_job_status repeatedly unless the user explicitly asks for status. "
        "Do not call cassette_run_job until both fields are available. "
        "This must use the same model-selection notice, progress screenshot notifications, terminal status reporting, export, and gateway delivery behavior as every other Cassette job."
    )


def _confirmed_prompt_guard(language: str = "zh") -> str:
    return (
        "The user has confirmed the optimized editing brief you proposed in the immediately preceding conversation. "
        "Use that confirmed optimized brief as the instruction for cassette_make_prompt. "
        f"{_cassette_run_job_tool_chain_guard(language)} "
        "Do not use the short confirmation word itself as the editing instruction."
    )


def _direct_original_instruction_guard(reason: str = "declined", language: str = "zh") -> str:
    if reason == "session_default":
        return (
            "This is a follow-up edit in the same Hermes session after the initial edit choices were completed. "
            "Use the original edit instruction above exactly as the instruction for cassette_make_prompt; do not ask again about prompt optimization or smart BGM, and do not optimize, rewrite, summarize, or reinterpret it first. "
            f"{_cassette_run_job_tool_chain_guard(language)} "
            "Do not use any previous short denial/confirmation word as the editing instruction."
        )
    return (
        "The user declined prompt optimization. "
        "Use the original edit instruction above exactly as the instruction for cassette_make_prompt; if the plugin appended a smart-BGM requirement to that instruction, preserve that requirement too; do not optimize, rewrite, summarize, or reinterpret it first. "
        "Skipping prompt optimization changes only the instruction source; it is not a lightweight/direct browser path. "
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


def _request_prompt_optimization_choice(
    session_id: str, instruction: str, asset_count: int, gateway: Any, event: Any, language: str = "zh"
) -> dict:
    _save_pending_edit(session_id, instruction, asset_count, "awaiting_optimization_choice")
    reply_sent = _send_gateway_fixed_reply(gateway, event, _prompt_optimization_choice_message(language))
    if reply_sent:
        return {
            "action": "skip",
            "reason": "cassette_prompt_optimization_choice_requested",
            "asset_count": asset_count,
            "session_id": session_id,
            "reply_sent": True,
        }
    text = (
        f"{instruction}\n\n"
        f"[Cassette gateway assets available: {asset_count} asset(s). "
        f"Use cassette session_id `{session_id}` for this edit instruction after the user chooses whether to optimize. "
        f"{_prompt_optimization_choice_guard(language)} "
        f"{_cassette_orchestration_guard()} "
        "Do not ask the user to resend the already saved media.]"
    )
    return {"action": "rewrite", "text": text}


def _rewrite_semantic_edit_instruction_judgment(
    session_id: str,
    instruction: str,
    asset_count: int,
    language: str = "zh",
) -> dict:
    if _initial_edit_choices_completed(session_id):
        text = (
            f"{instruction}\n\n"
            f"[Cassette gateway assets available: {asset_count} asset(s). "
            f"Use cassette session_id `{session_id}` only if this is a follow-up edit instruction. "
            "Hermes must semantically decide whether the user's message is a request to edit, transform, assemble, caption, style, add/remove/replace audio or visuals, export, or otherwise operate on the saved media. "
            "Do not rely on keyword matching. Vague follow-ups may be edit instructions when they naturally refer to the saved media. "
            "If this is not an edit instruction, answer the user normally and do not call Cassette tools. "
            f"If this is an edit instruction, {_direct_original_instruction_guard('session_default', language)} "
            f"{_cassette_orchestration_guard()} "
            "Do not ask the user to resend the already saved media.]"
        )
        return {"action": "rewrite", "text": text}
    if _gateway_model_choice_enabled() and not _cassette_model_selection_completed(session_id):
        try:
            options = _cassette_model_options(language)
        except Exception as exc:
            unavailable_message = _cassette_model_options_unavailable_message(
                language, _model_options_error_details(exc)
            )
            text = (
                f"{instruction}\n\n"
                f"[Cassette gateway assets available: {asset_count} asset(s). "
                f"Use cassette session_id `{session_id}` only if this is an edit instruction. "
                "Hermes must semantically decide whether the user's message is a request to edit, transform, assemble, caption, style, add/remove/replace audio or visuals, export, or otherwise operate on the saved media. "
                "Do not rely on keyword matching. Vague follow-ups may be edit instructions when they naturally refer to the saved media. "
                "If this is not an edit instruction, answer the user normally and do not call Cassette tools. "
                f"If this is an edit instruction, tell the user exactly this in {_language_name_for_prompt(language)}: {unavailable_message} "
                "Do not call cassette_list_assets, cassette_make_prompt, cassette_run_job, BGM tools, or browser automation. "
                "Do not ask about prompt optimization yet because model selection must happen first. "
                f"{_cassette_orchestration_guard()} "
                "Do not ask the user to resend the already saved media.]"
            )
            return {"action": "rewrite", "text": text}
        _save_pending_edit(
            session_id,
            instruction,
            asset_count,
            "awaiting_model_choice",
            model_options=options.get("models") or [],
            thinking_options=options.get("thinking_levels") or [],
            model_options_source=options.get("source") or "",
            language=_normalize_cassette_language(language) or "zh",
            resume_after_model="semantic_edit",
            semantic_gate=True,
        )
        text = (
            f"{instruction}\n\n"
            f"[Cassette gateway assets available: {asset_count} asset(s). "
            f"Use cassette session_id `{session_id}` only if this is an edit instruction. "
            "Hermes must semantically decide whether the user's message is a request to edit, transform, assemble, caption, style, add/remove/replace audio or visuals, export, or otherwise operate on the saved media. "
            "Do not rely on keyword matching. Vague follow-ups may be edit instructions when they naturally refer to the saved media. "
            "If this is not an edit instruction, answer the user normally and do not call Cassette tools. "
            f"If this is an edit instruction, ask exactly this model selection message in {_language_name_for_prompt(language)} before asking about prompt optimization:\n{_cassette_model_choice_message(options, language)} "
            "Do not call cassette_list_assets, cassette_make_prompt, cassette_run_job, BGM tools, or browser automation. "
            "Do not ask about prompt optimization yet. Wait for the user's model number first. "
            f"{_cassette_orchestration_guard()} "
            "Do not ask the user to resend the already saved media.]"
        )
        return {"action": "rewrite", "text": text}
    _save_pending_edit(
        session_id,
        instruction,
        asset_count,
        "awaiting_optimization_choice",
        semantic_gate=True,
    )
    text = (
        f"{instruction}\n\n"
        f"[Cassette gateway assets available: {asset_count} asset(s). "
        f"Use cassette session_id `{session_id}` only if this is an edit instruction. "
        "Hermes must semantically decide whether the user's message is a request to edit, transform, assemble, caption, style, add/remove/replace audio or visuals, export, or otherwise operate on the saved media. "
        "Do not rely on keyword matching. Vague follow-ups may be edit instructions when they naturally refer to the saved media. "
        "If this is not an edit instruction, answer the user normally and do not call Cassette tools. "
        f"If this is an edit instruction, ask exactly this question in {_language_name_for_prompt(language)}: {_prompt_optimization_choice_message(language)} "
        "Do not call cassette_list_assets, cassette_make_prompt, cassette_run_job, BGM tools, or browser automation yet; wait for the user's optimization choice. "
        f"{_cassette_orchestration_guard()} "
        "Do not ask the user to resend the already saved media.]"
    )
    return {"action": "rewrite", "text": text}


def _request_smart_bgm_choice(
    session_id: str,
    instruction: str,
    asset_count: int,
    gateway: Any,
    event: Any,
    *,
    optimization_enabled: bool,
    language: str = "zh",
) -> dict:
    _save_pending_edit(
        session_id,
        instruction,
        asset_count,
        "awaiting_bgm_choice",
        optimization_enabled=optimization_enabled,
    )
    reply_sent = _send_gateway_fixed_reply(gateway, event, _smart_bgm_choice_message(language))
    if reply_sent:
        return {
            "action": "skip",
            "reason": "cassette_smart_bgm_choice_requested",
            "asset_count": asset_count,
            "session_id": session_id,
            "reply_sent": True,
        }
    text = (
        f"{instruction}\n\n"
        f"[Cassette smart BGM choice is required before continuing. Cassette gateway assets available: {asset_count} asset(s). "
        f"Use cassette session_id `{session_id}` after the user chooses whether to use smart BGM. "
        f"Ask exactly this question in {_language_name_for_prompt(language)}: {_smart_bgm_choice_message(language)} "
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
    return _JAMENDO_DISABLED_CODE is None and bool(
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


def _sanitize_bgm_query(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", (value or "").lower())
    tokens: list[str] = []
    for token in normalized.split():
        if len(token) < 3:
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= 4:
            break
    return " ".join(tokens)[:80].strip()


def _freetouse_api_base() -> str:
    return os.getenv("CASSETTE_FREETOUSE_API_BASE", "https://api.freetouse.com/v3").rstrip("/")


def _freetouse_data_base() -> str:
    return os.getenv("CASSETTE_FREETOUSE_DATA_BASE", "https://data.freetouse.com").rstrip("/")


def _freetouse_request_json(path: str, params: dict[str, Any] | None = None) -> Any:
    query = f"?{urlencode(params)}" if params else ""
    url = f"{_freetouse_api_base()}{path}{query}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "oh-my-cassette/1.0"})
    with urlopen(request, timeout=float(os.getenv("CASSETTE_FREETOUSE_TIMEOUT_SEC", "20"))) as response:
        return json.load(response)


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


def _freetouse_search_tracks(query: str, limit: int = 10, order: str = "random") -> list[dict[str, Any]]:
    allowed_order = {"similarity", "release_date", "views", "plays", "downloads", "staff_order", "random"}
    order = order if order in allowed_order else "random"
    bounded_limit = max(1, min(limit, 25))
    params = {
        "query": query,
        "limit": bounded_limit,
        "order": order,
        "sort": "desc",
    }
    payload = _freetouse_request_json("/music/tracks/search", params)
    if isinstance(payload, dict):
        data = payload.get("data") or []
    elif isinstance(payload, list):
        data = payload
    else:
        data = []
    return [track for track in data if isinstance(track, dict)][:bounded_limit]


def _track_artist_title(track: dict[str, Any]) -> tuple[str, str]:
    title = str(track.get("title") or "Free To Use BGM").strip() or "Free To Use BGM"
    artists = track.get("artists") or []
    artist = "Free To Use"
    if artists and isinstance(artists, list):
        first = artists[0]
        if isinstance(first, (list, tuple)) and len(first) >= 2 and isinstance(first[1], dict):
            artist = str(first[1].get("name") or artist).strip() or artist
        elif isinstance(first, dict):
            artist = str(first.get("name") or artist).strip() or artist
    return artist, title


def _safe_music_filename(artist: str, title: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", f"{artist} - {title}").strip(" ._")
    if not stem:
        stem = "freetouse-bgm"
    return f"{stem[:120]}.mp3"


def _download_freetouse_track(session_id: str, track: dict[str, Any], query: str) -> dict[str, Any]:
    track_id = str(track.get("id") or "").strip()
    if not track_id:
        raise CassetteError("bgm_track_missing_id", "Free To Use search result did not include a track id")
    artist, title = _track_artist_title(track)
    filename = _safe_music_filename(artist, title)
    sess_hash = manifest.resolve_session_hash(session_id=session_id)
    media_dir = manifest.get_session_dir(sess_hash) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".bgm.", suffix=".mp3", dir=str(media_dir))
    os.close(fd)
    tmp_path = Path(tmp_name)
    url = f"{_freetouse_data_base()}/music/tracks/{quote(track_id)}/file/mp3"
    max_bytes = int(os.getenv("CASSETTE_FREETOUSE_MAX_BYTES", str(60 * 1024 * 1024)))
    try:
        request = Request(url, headers={"User-Agent": "oh-my-cassette/1.0"})
        written = 0
        with urlopen(request, timeout=float(os.getenv("CASSETTE_FREETOUSE_TIMEOUT_SEC", "20"))) as response:
            content_type = str(response.headers.get("content-type") or "").lower()
            if content_type and "audio" not in content_type and "octet-stream" not in content_type:
                raise CassetteError("bgm_download_unexpected_content_type", "Free To Use returned a non-audio response")
            with tmp_path.open("wb") as fh:
                while True:
                    chunk = response.read(1024 * 128)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise CassetteError(
                            "bgm_download_too_large", "Free To Use BGM download exceeded the configured size limit"
                        )
                    fh.write(chunk)
        if written <= 0:
            raise CassetteError("bgm_download_empty", "Free To Use BGM download was empty")
        digest = security.sha256_file(tmp_path)
        final_path = media_dir / f"{digest}.mp3"
        os.replace(tmp_path, final_path)
        data = manifest.ingest_internal_asset(
            str(final_path),
            session_id=session_id,
            original_name=filename,
            media_type="audio",
            caption=f"Smart BGM matched from Free To Use: {artist} - {title}. Search query: {query}.",
            metadata={
                "source": "freetouse",
                "track_id": track_id,
                "artist": artist,
                "title": title,
                "query": query,
                "license_note": "Free To Use API track; review freetouse.com/license for usage terms.",
            },
        )
        return {
            "status": "downloaded",
            "asset_id": data.get("asset_id"),
            "track_id": track_id,
            "artist": artist,
            "title": title,
            "query": query,
            "source_rank": track.get("_cassette_source_rank") or "",
        }
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _search_staff_and_popular_tracks(query: str) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_rank, order in (("staff_picks", "staff_order"), ("popular", "downloads")):
        for track in _freetouse_search_tracks(query, limit=5, order=order)[:5]:
            track_id = str(track.get("id") or "")
            if not track_id or track_id in seen or track.get("is_premium") is True:
                continue
            seen.add(track_id)
            combined.append({**track, "_cassette_source_rank": source_rank})
    return combined


def _match_and_download_smart_bgm(session_id: str, instruction: str, search_queries: list[str]) -> dict[str, Any]:
    del instruction
    if os.getenv("CASSETTE_SMART_BGM_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
        return {"status": "skipped", "code": "bgm_disabled"}
    queries = []
    for query in search_queries:
        cleaned = _sanitize_bgm_query(query)
        if cleaned and cleaned not in queries:
            queries.append(cleaned)
    attempted: list[str] = []
    zero_result_queries: list[str] = []
    try:
        for query in queries[:3]:
            attempted.append(query)
            tracks = _search_staff_and_popular_tracks(query)
            if not tracks:
                zero_result_queries.append(query)
                continue
            result = _download_freetouse_track(session_id, random.choice(tracks), query)
            result["queries"] = list(attempted)
            result["zero_result_queries"] = list(zero_result_queries)
            return result
        return {
            "status": "skipped",
            "code": "bgm_no_search_results",
            "queries": attempted,
            "zero_result_queries": zero_result_queries,
        }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, CassetteError) as exc:
        code = getattr(exc, "code", None)
        error_code = code if isinstance(code, str) else "bgm_match_failed"
        if isinstance(code, int):
            error_code = "bgm_api_limited" if code == 429 else "bgm_api_http_error"
        return {
            "status": "skipped",
            "code": error_code,
            "queries": attempted,
            "error_type": type(exc).__name__,
        }
    except Exception as exc:
        return {
            "status": "skipped",
            "code": "bgm_match_failed",
            "queries": attempted,
            "error_type": type(exc).__name__,
        }


def _bgm_note(result: dict[str, Any]) -> str:
    if result.get("status") == "downloaded":
        return (
            f"Smart BGM asset added: {result.get('artist') or 'unknown artist'} - "
            f"{result.get('title') or 'unknown title'} using query `{result.get('query') or ''}`. "
            "Use this uploaded audio asset as background music unless it conflicts with an explicit user requirement. "
        )
    code = result.get("code") or "bgm_skipped"
    return f"Smart BGM was skipped because {code}; continue the Cassette edit without blocking. "


def _instruction_with_bgm(instruction: str, bgm_result: dict[str, Any] | None, language: str = "zh") -> str:
    if not bgm_result or bgm_result.get("status") != "downloaded":
        return instruction
    artist = str(bgm_result.get("artist") or "Free To Use").strip() or "Free To Use"
    title = str(bgm_result.get("title") or "matched BGM").strip() or "matched BGM"
    if _normalize_cassette_language(language) == "en":
        return (
            f"{instruction}\n\n"
            f'Please use the uploaded smart-matched BGM asset "{artist} - {title}" as background music. '
            "Automatically fit its start/end timing, volume, fades, and balance with original audio to the video rhythm. "
            "If the user gave a more explicit music requirement, preserve that explicit requirement first."
        )
    return (
        f"{instruction}\n\n"
        f"请添加已上传的智能匹配 BGM「{artist} - {title}」作为背景音乐，"
        "根据视频节奏自动调整起止、音量、淡入淡出和与原声的平衡；"
        "如果用户原指令中有更明确的音乐要求，以用户明确要求优先。"
    )


def _bgm_fallback_notice(result: dict[str, Any], *, english: bool) -> str:
    fallback_from = str(result.get("fallback_from") or "").strip().lower()
    if fallback_from in {"exact_bgm", "exact_song", "cassette_match_exact_bgm"}:
        if english:
            return "The exact song match did not succeed, so I switched to fallback smart BGM matching. "
        return "精确歌曲匹配未成功，已切换到备用智能 BGM 匹配。"
    if fallback_from:
        if english:
            return "The primary BGM provider did not succeed, so I switched to fallback smart BGM matching. "
        return "首选 BGM 匹配未成功，已切换到备用智能 BGM 匹配。"
    return ""


def _smart_bgm_status_message(
    result: dict[str, Any], *, continue_after_match: bool = True, language: str = "zh"
) -> str:
    english = _normalize_cassette_language(language) == "en"
    fallback_notice = _bgm_fallback_notice(result, english=english)
    if result.get("status") == "downloaded":
        artist = result.get("artist") or ("unknown artist" if english else "未知艺术家")
        title = result.get("title") or ("unknown track" if english else "未知曲目")
        query = result.get("query") or ""
        if english:
            if continue_after_match:
                return f"{fallback_notice}Smart BGM matched: {artist} - {title}. Search keywords: {query}. I will continue the edit flow."
            return f"{fallback_notice}Smart BGM matched: {artist} - {title}. Search keywords: {query}. Added as a new audio asset for this session."
        if continue_after_match:
            return f"{fallback_notice}已智能匹配 BGM：{artist} - {title}。搜索关键词：{query}。我会继续后续剪辑流程。"
        return f"{fallback_notice}已智能匹配 BGM：{artist} - {title}。搜索关键词：{query}。已添加为当前会话的新音频素材，后续剪辑时会一并上传。"
    code = result.get("code") or "bgm_match_failed"
    queries = ", ".join(str(item) for item in result.get("queries") or [] if item)
    if english:
        suffix = f"Tried keywords: {queries}. " if queries else ""
        if continue_after_match:
            return f"{fallback_notice}Smart BGM matching did not succeed ({code}), so it will not block the edit flow. {suffix}I will continue the edit flow."
        return f"{fallback_notice}Smart BGM matching did not succeed ({code}). {suffix}No edit job was started; you can keep sending assets or send an edit instruction."
    suffix = f"已尝试关键词：{queries}。" if queries else ""
    if continue_after_match:
        return f"{fallback_notice}智能 BGM 匹配未成功（{code}），不会阻断剪辑流程。{suffix}我会继续后续剪辑流程。"
    return (
        f"{fallback_notice}智能 BGM 匹配未成功（{code}）。{suffix}不会执行额外剪辑操作；你可以继续发送素材或剪辑指令。"
    )


def _notify_smart_bgm_result(session_id: str, message: str) -> dict[str, Any]:
    try:
        listed = manifest.list_assets(session_id=session_id)
        delivery = dict((listed.get("manifest") or {}).get("delivery") or {})
    except Exception:
        delivery = {}
    if not delivery:
        return {"status": "skipped", "reason": "missing_delivery"}
    try:
        return notifier.notify_gateway_text(delivery, message, reason="smart_bgm")
    except Exception as exc:
        return {"status": "failed", "code": "smart_bgm_notify_failed", "error": type(exc).__name__}


def _bgm_next_step_guidance(
    optimization_enabled: bool, *, continue_after_match: bool = True, language: str = "zh"
) -> str:
    if not continue_after_match:
        return (
            "Standalone smart BGM command is complete. Do not call cassette_list_assets, "
            "cassette_make_prompt, cassette_run_job, or browser automation; only report the saved BGM material status."
        )
    if optimization_enabled:
        return (
            "Use effective_instruction as the source text for prompt optimization. "
            "Send the optimized brief to the user for confirmation and do not start Cassette until confirmed."
        )
    return (
        "Use effective_instruction directly with cassette_list_assets, cassette_make_prompt, and cassette_run_job. "
        f"{_cassette_run_job_tool_chain_guard(language)} "
        "Do not ask for another confirmation before starting Cassette."
    )


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
    if _gateway_model_choice_enabled() and not _cassette_model_selection_completed(session_id):
        return _request_cassette_model_choice(
            session_id,
            instruction,
            asset_count,
            gateway,
            event,
            language=language,
            resume_after_model="refine" if force_refine else "edit",
        )
    if force_refine:
        _mark_initial_edit_choices_completed(
            session_id,
            optimization_enabled=True,
            smart_bgm_enabled=False,
            source="refine_command",
        )
        return _rewrite_accepted_prompt_optimization(session_id, instruction, asset_count, language=language)
    if _initial_edit_choices_completed(session_id):
        return _rewrite_direct_original_instruction(
            session_id,
            instruction,
            asset_count,
            direct_reason="session_default",
            language=language,
        )
    return _request_prompt_optimization_choice(session_id, instruction, asset_count, gateway, event, language)


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
            resume_after_model="command",
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
        if pending_edit and _initial_edit_choices_completed(session_id):
            pending_state = str(pending_edit.get("state") or "")
            if pending_state in {"awaiting_optimization_choice", "awaiting_bgm_choice"}:
                _clear_pending_edit(session_id)
                pending_edit = None
        if (
            pending_edit
            and pending_edit.get("state") == "awaiting_optimization_choice"
            and pending_edit.get("semantic_gate")
            and not _looks_like_prompt_optimization_decline(user_text)
            and not _looks_like_prompt_optimization_accept(user_text)
        ):
            _clear_pending_edit(session_id)
            pending_edit = None
        if pending_edit and pending_edit.get("state") == "awaiting_optimization_choice":
            pending_instruction = str(pending_edit.get("instruction") or "").strip()
            if _looks_like_prompt_optimization_decline(user_text):
                connectivity = _cassette_connectivity_skip(gateway, event, cassette_language)
                if connectivity:
                    return connectivity
                if (
                    pending_edit.get("semantic_gate")
                    and _gateway_model_choice_enabled()
                    and not _cassette_model_selection_completed(session_id)
                ):
                    return _request_cassette_model_choice(
                        session_id,
                        pending_instruction,
                        asset_count,
                        gateway,
                        event,
                        language=cassette_language,
                        resume_after_model="semantic_optimization_choice",
                        optimization_enabled=False,
                    )
                return _request_smart_bgm_choice(
                    session_id,
                    pending_instruction,
                    asset_count,
                    gateway,
                    event,
                    optimization_enabled=False,
                    language=cassette_language,
                )
            if _looks_like_prompt_optimization_accept(user_text):
                connectivity = _cassette_connectivity_skip(gateway, event, cassette_language)
                if connectivity:
                    return connectivity
                if (
                    pending_edit.get("semantic_gate")
                    and _gateway_model_choice_enabled()
                    and not _cassette_model_selection_completed(session_id)
                ):
                    return _request_cassette_model_choice(
                        session_id,
                        pending_instruction,
                        asset_count,
                        gateway,
                        event,
                        language=cassette_language,
                        resume_after_model="semantic_optimization_choice",
                        optimization_enabled=True,
                    )
                return _request_smart_bgm_choice(
                    session_id,
                    pending_instruction,
                    asset_count,
                    gateway,
                    event,
                    optimization_enabled=True,
                    language=cassette_language,
                )
            return _reject_busy_flow(
                session_id, gateway, event, cassette_language, "cassette_prompt_optimization_choice_busy_rejected"
            )
        if pending_edit and pending_edit.get("state") == "awaiting_bgm_choice":
            pending_instruction = str(pending_edit.get("instruction") or "").strip()
            optimization_enabled = bool(pending_edit.get("optimization_enabled"))
            bgm_result: dict[str, Any] | None = None
            if _looks_like_bgm_accept(user_text):
                _mark_initial_edit_choices_completed(
                    session_id,
                    optimization_enabled=optimization_enabled,
                    smart_bgm_enabled=True,
                    source="initial_bgm_accept",
                )
                return _request_exact_bgm_recommendations(
                    session_id,
                    pending_instruction,
                    asset_count,
                    optimization_enabled=optimization_enabled,
                    language=cassette_language,
                )
            elif _looks_like_bgm_decline(user_text):
                _mark_initial_edit_choices_completed(
                    session_id,
                    optimization_enabled=optimization_enabled,
                    smart_bgm_enabled=False,
                    source="initial_bgm_decline",
                )
                bgm_result = {"status": "skipped", "code": "bgm_declined_by_user"}
            else:
                return _reject_busy_flow(
                    session_id, gateway, event, cassette_language, "cassette_smart_bgm_choice_busy_rejected"
                )
            if optimization_enabled:
                return _rewrite_accepted_prompt_optimization(
                    session_id, pending_instruction, asset_count, bgm_result, language=cassette_language
                )
            return _rewrite_direct_original_instruction(
                session_id, pending_instruction, asset_count, bgm_result, language=cassette_language
            )
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
                'If Cassette says it is still editing or needs routine continuation, call cassette_review_completion with decision="continue" and a concise reason.',
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


def _debug_session_hash(session_id: str) -> str:
    try:
        return manifest.resolve_session_hash(session_id=session_id)
    except Exception:
        return safe_hash_id(session_id)


def _summarize_exact_bgm_attempts(attempts: Any) -> list[dict[str, Any]]:
    if not isinstance(attempts, list):
        return []
    result: list[dict[str, Any]] = []
    for attempt in attempts[:6]:
        if not isinstance(attempt, dict):
            continue
        summary = {
            "mode": attempt.get("mode") or "",
            "query": attempt.get("query") or "",
            "candidate_count": attempt.get("candidate_count") or 0,
            "eligible_count": attempt.get("eligible_count") or 0,
            "downloadable_count": attempt.get("downloadable_count") if "downloadable_count" in attempt else "",
            "strict_title": bool(attempt.get("strict_title")),
        }
        failures = attempt.get("candidate_failures")
        if isinstance(failures, list) and failures:
            summary["candidate_failures"] = [
                {
                    "source": failure.get("source") or "",
                    "track_id": failure.get("track_id") or "",
                    "title": failure.get("title") or "",
                    "artist": failure.get("artist") or "",
                    "code": failure.get("code") or "",
                    "details": failure.get("details") or {},
                    "audio_url": failure.get("audio_url") or {},
                }
                for failure in failures[:5]
                if isinstance(failure, dict)
            ]
        result.append(summary)
    return result


def _clean_cassette_debug_value(value: Any, *, key: str = "") -> Any:
    lowered_key = key.lower()
    if any(part in lowered_key for part in ("token", "secret", "password", "credential", "api_key")):
        return "<redacted>"
    if lowered_key in {"file_path", "local_path", "saved_path", "source_path", "manifest_path", "metadata_path"}:
        return "<redacted_path>"
    if isinstance(value, str):
        return redact_for_log(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {
            str(item_key): _clean_cassette_debug_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_clean_cassette_debug_value(item) for item in list(value)[:20]]
    return redact_for_log(str(value))


def _log_cassette_debug_event(event: str, **fields: Any) -> None:
    try:
        log_dir = manifest.get_asset_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": jobs.now_iso(),
            "event": event,
        }
        payload.update({key: _clean_cassette_debug_value(value, key=key) for key, value in fields.items()})
        with (log_dir / "cassette.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        return None


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
