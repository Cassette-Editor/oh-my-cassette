from __future__ import annotations

import functools
import json
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
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import exact_bgm, jamendo, jobs, manifest, notifier, security, transport
from . import prompt as prompt_mod
from .errors import CassetteError
from .security import redact_for_log, safe_hash_id

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
        is_completion_question = any(
            str(q.get("reason") or "").startswith("hermes_completion_review") for q in (job.get("questions") or [])
        )
        has_continuation = isinstance(job.get("continuation"), dict) and bool(job.get("continuation"))
        if is_completion_question and not has_continuation:
            # Post-review user answer: the reviewed turn already completed, so there is no
            # interrupt to resume — route the answer as a follow-up direct-line turn instead.
            answered_quality = dict(quality)
            answered_quality["completion_question_answered"] = True
            job.update({"status": "succeeded", "quality": answered_quality, "finished_at": jobs.now_iso()})
            jobs.save_job(job)
            follow = _completion_follow_up_turn(job, response, kw)
            follow_data = follow.get("data") if isinstance(follow.get("data"), dict) else {}
            return ok(
                {"job": _scrub_job(job), "follow_up": follow_data or follow},
                job_id=job_id,
            )
        if kw.get("runtime_host") == "mcp":
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
    if explicit in {"off", "none", "disabled", "关闭", "关", "无"}:
        return "Off"
    if explicit in {"minimal", "minimum", "最小", "最低"}:
        return "Minimal"
    if explicit in {"high", "deep", "高", "高思考", "深度", "重度"}:
        return "High"
    if explicit in {"xhigh", "extra high", "extra-high", "最高", "超高"}:
        return "XHigh"
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
    del delivery  # session prefs apply on every host (MCP included), not just gateways
    # Precedence: explicit run args > session preference (cassette_config / /cassette_model) > default.
    if args.get("cassette_model") or args.get("model") or args.get("thinking_level"):
        text = "\n".join(
            str(args.get(key) or "") for key in ("chat_message", "cassette_message", "instruction", "prompt")
        )
        return {
            "model": (args.get("cassette_model") or args.get("model") or "").strip(),
            "thinking_level": _normalize_thinking_level(args.get("thinking_level"), text),
            "source": "user_or_default",
        }
    if session_id:
        preference = _cassette_model_preference_for_session(session_id)
        if preference:
            return {**preference, "source": "session_preference"}
    text = "\n".join(str(args.get(key) or "") for key in ("chat_message", "cassette_message", "instruction", "prompt"))
    return {"model": "", "thinking_level": _normalize_thinking_level(None, text), "source": "default"}


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


def shutdown_gateway_job_executor(*, wait: bool = True) -> None:
    """Drain the background gateway worker and drop the pool.

    A submitted worker outlives the call that queued it, and it resolves the asset root when
    it finally writes rather than when it was submitted. Anything that sandboxes the asset
    root for the duration of a call -- the test suite, most obviously -- therefore has to
    drain the pool before restoring the environment, or the escaping worker persists its job
    record into the developer's real Hermes data directory, where it stays visible forever as
    a running job. A long-lived host never needs this; it wants the pool to keep working.
    """
    global _GATEWAY_JOB_EXECUTOR
    with _GATEWAY_JOB_EXECUTOR_LOCK:
        executor = _GATEWAY_JOB_EXECUTOR
        _GATEWAY_JOB_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=wait)


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
    message = (a.get("message") or "").strip()
    prompt_text = (a.get("prompt") or "").strip()
    chat_message = (a.get("chat_message") or a.get("cassette_message") or "").strip()
    if not prompt_text and chat_message:
        prompt_text = chat_message
    if not prompt_text and message:
        prompt_text = message
    if not (message or prompt_text):
        raise CassetteError("missing_required_arg", "message (preferred) or prompt is required")
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
            "message": message,
            "chat_message": chat_message,
            "url": a.get("url"),
            "timeout_sec": a.get("timeout_sec"),
            "selectors": a.get("selectors") or {},
            "delivery": delivery,
            "cassette_session_id": a.get("session_id"),
            "model_selection": _cassette_model_selection(a, delivery),
            "cassette_language": _cassette_language_for_run(a, delivery),
            # Tri-state: absent means no render, so a turn ends with the edit committed only.
            **({"export_on_complete": "true" if a.get("export") else "false"} if a.get("export") is not None else {}),
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
    # The transport clears completion_observed when the agent itself reported it could not do the
    # job (terminalDecision outcome 'not_done'). The run still ends 'succeeded' because the LangGraph
    # run succeeded — the transport fact and the editorial outcome are different things. Only an
    # explicit False downgrades: a succeeded export always sets it True, and an absent key means no
    # transport ever measured it, so neither is mistaken for a refusal.
    if status == "succeeded" and quality.get("completion_observed") is False:
        user_summary = (
            "Cassette reported it could not carry out this edit, so the timeline is unchanged. "
            "Read the chat message for the reason and retry with a more specific instruction."
        )
    elif status == "succeeded" and export_pending:
        user_summary = "Cassette chat panel indicates the edit is complete, but no exported video was recorded."
    elif status == "succeeded":
        user_summary = "Cassette edit completed and exported output is available."
    elif status == "timed_out":
        user_summary = "Cassette did not expose a completion signal before timeout. Check the latest progress summary before treating this as failure."
    elif status == "needs_user":
        user_summary = "Cassette needs user input before it can continue."
    elif status == "cancelled":
        user_summary = (
            "Cassette job was paused by request; the session is preserved for retry or follow-up editing instructions."
        )
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


def _completion_follow_up_turn(job: dict, message: str, kw: dict) -> dict:
    """Start a follow-up direct-line turn on the reviewed job's session; return the parsed envelope.

    A completed turn has no interrupt to resume, so post-review continuation is a NEW turn on the
    persisted session thread (0.4.1 direct line) — same context, verbatim message. This is the only
    correct continuation on the API transport; ``transport.resume`` raises resume_not_waiting_for_user.
    """
    delivery = job.get("delivery") if isinstance(job.get("delivery"), dict) else {}
    follow_args: dict[str, Any] = {
        "message": message,
        "session_id": str(job.get("cassette_session_id") or "").strip(),
        "chat_id": delivery.get("chat_id"),
        "url": job.get("url"),
    }
    follow_args = {k: v for k, v in follow_args.items() if v}
    return json.loads(cassette_run_job.__wrapped__(follow_args, **kw))


@safe_tool
def cassette_review_completion(a: dict, **kw) -> str:
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
    # The review is resolved by this call for every decision; a completion-review question that
    # remains needs_user must be answerable via cassette_answer_question afterwards.
    quality["completion_review_required"] = False
    if decision == "continue":
        follow_message = str(a.get("message") or "").strip() or reason
        job.update({"status": "succeeded", "quality": quality, "finished_at": jobs.now_iso()})
        jobs.save_job(job)
        follow = _completion_follow_up_turn(job, follow_message, kw)
        follow_data = follow.get("data") if isinstance(follow.get("data"), dict) else {}
        follow_job = follow_data.get("job") if isinstance(follow_data.get("job"), dict) else {}
        quality["follow_up_job_id"] = str(follow_job.get("job_id") or follow.get("job_id") or "")
        job["quality"] = quality
        jobs.save_job(job)
        return ok(
            {"job": _scrub_job(job), "completion_review": review, "follow_up": follow_data or follow},
            job_id=job_id,
        )
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


def _model_label_from_input(value: str) -> str:
    """Accept a product model id or display label; return the canonical display label."""
    from . import api_transport

    norm = "".join(ch for ch in str(value).lower() if ch.isalnum())
    for option in api_transport.AGENT_MODEL_OPTIONS:
        if value == option["id"] or norm == "".join(ch for ch in option["label"].lower() if ch.isalnum()):
            return option["label"]
    raise CassetteError(
        "invalid_cassette_model",
        f"Unknown Cassette model {value!r}",
        {"options": [f"{option['label']} ({option['id']})" for option in api_transport.AGENT_MODEL_OPTIONS]},
    )


@safe_tool
def cassette_config(a: dict, **kw) -> str:
    """Get/set the session's Cassette model and thinking level (applies from the next turn)."""
    from . import api_transport

    session_id = str(a.get("session_id") or "").strip()
    if not session_id:
        raise CassetteError("missing_required_arg", "session_id is required")
    model_arg = str(a.get("model") or a.get("cassette_model") or "").strip()
    thinking_arg = str(a.get("thinking_level") or "").strip()
    if thinking_arg and thinking_arg.lower() not in set(api_transport.AGENT_THINKING_LEVELS):
        raise CassetteError(
            "invalid_thinking_level",
            f"Unknown thinking level {thinking_arg!r}",
            {"options": list(api_transport.AGENT_THINKING_LEVELS)},
        )
    if model_arg or thinking_arg:
        current = _cassette_model_preference_for_session(session_id)
        label = _model_label_from_input(model_arg) if model_arg else (current.get("model") or "")
        if not label:
            label = next(
                option["label"]
                for option in api_transport.AGENT_MODEL_OPTIONS
                if option["id"] == api_transport.DEFAULT_AGENT_MODEL_ID
            )
        thinking = thinking_arg or current.get("thinking_level") or api_transport._DEFAULT_THINKING
        _save_cassette_model_preference(session_id, label, thinking, source="cassette_config")
    preference = _cassette_model_preference_for_session(session_id)
    default_label = next(
        option["label"]
        for option in api_transport.AGENT_MODEL_OPTIONS
        if option["id"] == api_transport.DEFAULT_AGENT_MODEL_ID
    )
    return ok(
        {
            "session_id": session_id,
            "model": preference.get("model") or default_label,
            "thinking_level": preference.get("thinking_level") or "Low",
            "source": "session_preference" if preference else "default",
            "options": _cassette_model_options(),
        }
    )


# Kept beside the handler so every failure the user can actually act on reads as a sentence
# rather than a status code. Passwords are server-generated and mailed, never chosen, so an
# invalid one means "stale", not "you mistyped it".
_AUTH_MESSAGES = {
    # /api/agent-auth/verify checks the password before the allowlist, so this one really is
    # about the password and nothing else — an address with no Cassette access answers 403
    # (auth_not_authorized) once the password checks out.
    "auth_invalid_password": (
        "Cassette rejected that password. Passwords are generated and emailed, never chosen, so "
        "nothing was mistyped — the stored one is stale. Request a new one to replace it."
    ),
    "auth_not_authorized": (
        "That address is not authorised for Cassette, so nothing was stored. The password itself "
        "was fine — the account has no Cassette access. Request access for it first."
    ),
    "auth_rate_limited": "Too many attempts for that address. Wait for the retry window and try again.",
    "auth_verify_failed": "Cassette could not verify the credentials; nothing was stored.",
    "auth_password_request_failed": (
        "Cassette could not send a new password. It mails the replacement before storing it, so "
        "the previous password should still work — try signing in with it before retrying, "
        "because every attempt spends the hourly limit."
    ),
}


@safe_tool
def cassette_login(a: dict, **kw) -> str:
    """Verify Cassette credentials and store them privately, or request a replacement by email.

    In-band credential setup, chosen deliberately over the terminal command: the owner
    accepted that a pasted password reaches the host's transcript in exchange for removing the
    terminal step entirely. Note that the MCP spec forbids form-mode elicitation for
    credentials (2025-11-25 elicitation, Security Considerations) and that a tool argument is
    strictly worse, because the model itself must emit the value. URL-mode elicitation to a
    loopback page and an OAuth-style device flow were both evaluated and declined -- the first
    needs a listening port, the second needs backend routes this plugin does not own. Revisit
    that decision before "fixing" this; it is a product choice, not an oversight.
    """
    from . import api_transport

    email = str(a.get("email") or "").strip()
    if not email:
        raise CassetteError("missing_required_arg", "email is required")
    # Strip on write because load_credentials() strips on read: a pasted password carrying a
    # trailing space would verify here and then reach the transport as a different string.
    # The generated alphabet contains no whitespace, so this is lossless.
    password = str(a.get("password") or "").strip()
    request_new = bool(a.get("request_new_password"))

    if str(kw.get("runtime_host") or "hermes") != "mcp":
        # Hermes resolves credentials from its process env and ~/.hermes/.env, and
        # mcp_env_value() reads the stored file only under the mcp adapter -- anything written
        # here would never be read back. Refuse rather than write somewhere inert.
        return err(
            "cassette_login",
            "auth_unsupported_adapter",
            "This host reads Cassette credentials from its environment. Set CASSETTE_AUTH_EMAIL "
            "and CASSETTE_AUTH_PASSWORD in ~/.hermes/.env instead.",
            {"credential_source": "environment"},
            recoverable=False,
        )

    import runtime_config

    try:
        stored = runtime_config.load_credentials()
    except runtime_config.RuntimeConfigError as exc:
        return err(
            "cassette_login",
            exc.code,
            str(exc),
            {"path": str(exc.path or "")},
            recoverable=False,
        )
    if stored.get("source") == "environment":
        # Process environment wins over the private config, so storing anything here would
        # leave those variables shadowing whatever we wrote.
        return err(
            "cassette_login",
            "auth_env_precedence",
            "Cassette credentials already come from environment variables, which take "
            "precedence over the private config. Update those variables instead — storing "
            "credentials here would have no effect.",
            {"credential_source": "environment"},
        )

    if request_new:
        if not bool(a.get("confirm_replace")):
            # request-code replaces the account password on every machine and spends one of
            # three hourly attempts. A bound second argument -- not prose in a tool
            # description -- is what stops an agent firing this casually.
            return err(
                "cassette_login",
                "auth_confirm_required",
                "Requesting a new password replaces the account password on every machine and "
                "emails the replacement. Confirm with the user, then re-call with "
                "confirm_replace=true.",
                {"replaces_existing": True, "email": email, "limit": "3 per hour"},
            )
        outcome = api_transport.request_new_agent_password(email)
        if outcome.get("ok"):
            # Not ok=True: the caller asked to be signed in and is not yet, because the new
            # password only exists in the user's inbox. The agent must collect it next.
            return err(
                "cassette_login",
                "auth_password_emailed",
                f"A new password was sent to {email}. The previous one no longer works on any "
                "machine. Ask the user for the new password, then call cassette_login again.",
                {"replaces_existing": True, "email": email, "limit": "3 per hour"},
            )
        details: dict[str, Any] = {"email": email}
        if outcome.get("retry_after_sec") is not None:
            details["retry_after_sec"] = outcome["retry_after_sec"]
        code = str(outcome.get("code") or "auth_password_request_failed")
        return err("cassette_login", code, _AUTH_MESSAGES.get(code, "Cassette could not send a new password."), details)

    if not password:
        raise CassetteError("missing_required_arg", "password is required unless request_new_password is set")

    outcome = api_transport.verify_agent_credentials(email, password)
    if not outcome.get("ok"):
        # Nothing has been written at this point, so a bad paste leaves an existing working
        # credential exactly as it was.
        details = {"email": email}
        if outcome.get("retry_after_sec") is not None:
            details["retry_after_sec"] = outcome["retry_after_sec"]
        code = str(outcome.get("code") or "auth_verify_failed")
        return err("cassette_login", code, _AUTH_MESSAGES.get(code, "Cassette rejected the credentials."), details)

    verified_at = manifest.now_iso()
    runtime_config.write_protected_json(
        runtime_config.credentials_path(),
        {"email": email, "password": password, "verified_at": verified_at},
    )
    return ok(
        {
            "email": email,
            "stored": True,
            "verified_at": verified_at,
            "credentials_path": str(runtime_config.credentials_path()),
        }
    )


def _previews_dir(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:120] or "session"
    return Path(os.getenv("CASSETTE_ASSET_ROOT", str(manifest.get_asset_root()))) / "previews" / safe


_SHEET_CLIP_TYPES = {"video", "image", "motion-graphic"}
_SHEET_CELL_FILTER = "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2:color=0x07080b"


def _clip_source_midpoint_sec(clip: dict, fps: float) -> float:
    """Source-time midpoint of what the timeline actually uses from this clip."""
    try:
        in_sec = float(clip.get("inSec") or 0.0)
        duration = float(clip.get("durationInFrames") or 0) / fps if fps else 0.0
        speed = float(clip.get("speed") or 1.0) or 1.0
        mid = in_sec + (duration * speed) / 2.0
        source_max = clip.get("sourceDurationSeconds")
        if isinstance(source_max, (int, float)) and source_max > 0.2:
            mid = min(mid, float(source_max) - 0.1)
        return max(0.0, mid)
    except (TypeError, ValueError):
        return 0.0


def _sheet_media_lookup(session_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """(mediaFileId -> local path, lowercase name -> local path) for the session's ingested media.

    The upload cache maps local-file fingerprints to the mediaFileIds the server assigned, so
    clips resolve back to the exact files the plugin uploaded. The project session id and the
    ingest session id can differ by the namespace prefix (Hermes mode) — try both."""
    from . import api_transport as api_mod

    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    candidates = [session_id]
    split = api_mod.split_session_prefix(session_id)
    if split:
        candidates.append(split[1])
    cache: dict[str, str] = {}
    for sid in candidates:
        try:
            cache.update(api_mod.ApiTransport()._load_upload_cache(sid) or {})
        except Exception:  # noqa: BLE001
            pass
    for sid in candidates:
        try:
            listed = manifest.list_assets(sid)
            assets = (listed.get("manifest") or {}).get("assets") or []
        except Exception:  # noqa: BLE001
            continue
        for asset in assets:
            path = str(asset.get("saved_path") or "")
            if not path or not Path(path).exists():
                continue
            fingerprint = api_mod.ApiTransport._asset_fingerprint(path)
            media_id = cache.get(fingerprint)
            if media_id:
                by_id.setdefault(str(media_id), path)
            for key in (asset.get("original_name"), Path(path).name):
                normalized = str(key or "").strip().lower()
                if normalized:
                    by_name.setdefault(normalized, path)
    return by_id, by_name


def build_contact_sheet(document: dict, session_id: str) -> str | None:
    """Tile one frame per timeline clip into a contact-sheet jpeg (zero server render).

    Frame sources, in order: the clip's stored poster data URI (browser uploads), the locally
    ingested media file (matched by mediaFileId via the session upload cache, then by name via
    the manifest), then the clip's preview/source URL fetched by ffmpeg with the agent bearer
    (server-side/generated assets). Frames are taken at each clip's mid-point source position,
    so the sheet is a filmstrip of what the timeline actually uses — still source frames, not
    composed output. Returns None when nothing can be tiled or ffmpeg is unavailable."""
    import base64
    import shutil
    import subprocess

    from . import api_transport as api_mod
    from . import timeline as timeline_mod

    try:
        fps = float((document.get("sequenceTimebase") or {}).get("num") or document.get("fps") or 30) / float(
            (document.get("sequenceTimebase") or {}).get("den") or 1
        )
        clips = [
            c
            for c in timeline_mod.clips_in_timeline_order(document)
            if c.get("type") in _SHEET_CLIP_TYPES or str(c.get("thumbnail") or "").startswith("data:image")
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

        by_id, by_name = _sheet_media_lookup(session_id)
        transport: Any = None

        with tempfile.TemporaryDirectory(dir=str(out_dir)) as tmp:
            cell_index = 0
            for clip in clips:
                source: str | None = None
                headers: str | None = None
                seek: float | None = _clip_source_midpoint_sec(clip, fps)
                thumb = str(clip.get("thumbnail") or "")
                if thumb.startswith("data:image"):
                    _, _, b64 = thumb.partition(",")
                    poster = Path(tmp) / f"poster_{cell_index:02d}.img"
                    poster.write_bytes(base64.b64decode(b64 or "", validate=False))
                    source, seek = str(poster), None
                if source is None:
                    source = by_id.get(str(clip.get("mediaFileId") or ""))
                if source is None:
                    for key in (clip.get("originalFileName"), clip.get("sourceDisplayName"), clip.get("name")):
                        normalized = str(key or "").strip().lower()
                        if normalized and normalized in by_name:
                            source = by_name[normalized]
                            break
                if source is None:
                    url = str(clip.get("previewSrc") or clip.get("src") or "")
                    if url.startswith("/"):
                        url = api_mod._api_base() + url
                    if url.startswith("http"):
                        if transport is None:
                            transport = api_mod.ApiTransport()
                            transport._authenticate()
                        source = url
                        headers = f"Authorization: Bearer {transport._token}\r\n"
                if source is None:
                    continue
                if clip.get("type") == "image":
                    seek = None
                cell = Path(tmp) / f"cell_{cell_index:02d}.jpg"
                for attempt_seek in [seek, 0.0] if seek else [None]:
                    cmd = [ffmpeg, "-v", "error", "-y"]
                    if headers:
                        cmd += ["-headers", headers]
                    if attempt_seek:
                        cmd += ["-ss", f"{attempt_seek:.3f}"]
                    cmd += ["-i", source, "-frames:v", "1", "-vf", _SHEET_CELL_FILTER, str(cell)]
                    subprocess.run(cmd, capture_output=True, timeout=25)
                    if cell.exists() and cell.stat().st_size:
                        cell_index += 1
                        break
                    # A seek past the media's end yields no frame; retry from the start.
            if not cell_index:
                return None
            cols = min(4, cell_index)
            rows = -(-cell_index // cols)
            subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-y",
                    "-framerate",
                    "1",
                    "-i",
                    str(Path(tmp) / "cell_%02d.jpg"),
                    "-vf",
                    f"tile={cols}x{rows}:padding=4:color=0x07080b",
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


def build_storyboard_sheet(session_id: str, frames: list[dict]) -> str | None:
    """Tile one poster frame per planned storyboard beat into a jpeg (zero server render).

    ``frames`` are decoded ``media://storyboard/...`` refs from the plan's reviewMarkdown
    (timeline.storyboard_frames). Each beat's frame comes from the locally ingested source
    file (mediaFileId via the session upload cache), taken at the beat's source-window
    midpoint — the exact footage the plan proposes, before anything is executed. A beat
    with no resolvable source (generated coverage, or media the plugin never ingested)
    gets a dark placeholder cell so cell order always matches the digest's beat order,
    mirroring the web StoryboardCard's fail-open. Restyle-moment refs (role 'restyle')
    duplicate beat windows and are skipped. Sheets are cached per frame-set digest.
    Returns None when ffmpeg is unavailable or no beats decode."""
    import hashlib
    import shutil
    import subprocess

    try:
        beats = [f for f in frames if isinstance(f, dict) and f.get("role") != "restyle"][:8]
        if not beats:
            return None
        ffmpeg = os.getenv("CASSETTE_FFMPEG_BIN", "ffmpeg")
        if not shutil.which(ffmpeg):
            return None
        out_dir = _previews_dir(session_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(json.dumps(beats, sort_keys=True).encode("utf-8")).hexdigest()[:10]
        target = out_dir / f"storyboard-{digest}.jpg"
        if target.exists() and target.stat().st_size:
            return str(target)  # per-plan sheets are immutable

        by_id, _ = _sheet_media_lookup(session_id)
        with tempfile.TemporaryDirectory(dir=str(out_dir)) as tmp:
            cell_index = 0
            for frame in beats:
                cell = Path(tmp) / f"cell_{cell_index:02d}.jpg"
                source = by_id.get(str(frame.get("mediaFileId") or ""))
                start, end = frame.get("startSec"), frame.get("endSec")
                seek = (
                    (float(start) + float(end)) / 2.0
                    if isinstance(start, (int, float)) and isinstance(end, (int, float))
                    else float(start)
                    if isinstance(start, (int, float))
                    else None
                )
                if source:
                    for attempt_seek in [seek, 0.0] if seek else [None]:
                        cmd = [ffmpeg, "-v", "error", "-y"]
                        if attempt_seek:
                            cmd += ["-ss", f"{attempt_seek:.3f}"]
                        cmd += ["-i", source, "-frames:v", "1", "-vf", _SHEET_CELL_FILTER, str(cell)]
                        subprocess.run(cmd, capture_output=True, timeout=25)
                        if cell.exists() and cell.stat().st_size:
                            break
                        # A seek past the media's end yields no frame; retry from the start.
                if not (cell.exists() and cell.stat().st_size):
                    subprocess.run(
                        [
                            ffmpeg,
                            "-v",
                            "error",
                            "-y",
                            "-f",
                            "lavfi",
                            "-i",
                            "color=c=0x1a1c22:size=320x180",
                            "-frames:v",
                            "1",
                            str(cell),
                        ],
                        capture_output=True,
                        timeout=25,
                    )
                if cell.exists() and cell.stat().st_size:
                    cell_index += 1
            if not cell_index:
                return None
            cols = min(4, cell_index)
            rows = -(-cell_index // cols)
            subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-y",
                    "-framerate",
                    "1",
                    "-i",
                    str(Path(tmp) / "cell_%02d.jpg"),
                    "-vf",
                    f"tile={cols}x{rows}:padding=4:color=0x07080b",
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


# Curated no-LLM direct-edit surface: the intersection of the public tool catalog with the
# server command lane's dispatch (toolNameToTimelineIntentType — verified live: addTextClip/
# textStyle/textLayout/effect 500 as "Unsupported server project tool"), minus the
# media-resolution/generation tools. Inputs are {"payload": {...}} and the server statically
# validates them, returning precise messages on mismatch.
_DIRECT_EDIT_TOOLS = {
    "timeline_trim": "trim/retime a clip",
    "timeline_arrange": "move/reorder clips",
    "timeline_deleteClips": "delete clips",
    "timeline_text": "text content/typography/box (op: setText/setStyle/setTypography/...)",
    "timeline_properties": "clip properties (volume/speed/opacity/...)",
    "timeline_filter": "apply/adjust filters",
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
            data["contact_sheet_uri"] = Path(sheet).as_uri()
        else:
            data["contact_sheet_path"] = None
            data["contact_sheet_note"] = "no clip posters available yet (or ffmpeg missing)"
    return ok(data)


def check_transport_ready() -> bool:
    # Transport-readiness gate: the API base URL and credentials are configured.
    return transport.get_transport().check_available()


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


def _gateway_asset_count(session_id: str) -> int:
    try:
        data = manifest.list_assets(session_id=session_id)
    except CassetteError:
        return 0
    except Exception:
        return 0
    assets = data.get("manifest", {}).get("assets", [])
    return len([asset for asset in assets if asset.get("exists", True)])


def _fixed_flow_busy_message(language: str = "zh") -> str:
    if _normalize_cassette_language(language) == "en":
        return "Use /cut to stop the current flow or edit job before starting a new edit task."
    return "请使用/cut命令终止当前流程或剪辑任务后再尝试开始新的剪辑任务"


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


def _cassette_model_preference_for_session(session_id: str) -> dict[str, str]:
    prefs = _load_session_preferences(session_id)
    model = str(prefs.get("cassette_model") or "").strip()
    thinking = _normalize_thinking_level(str(prefs.get("cassette_thinking_level") or ""))
    if not model or not prefs.get("cassette_model_selection_completed"):
        return {}
    return {"model": model, "thinking_level": thinking or "Low"}


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
    # Static product list single-sourced from the API transport, so it cannot fail or block.
    # Labels are locale-independent brand names, so `language` is unused.
    from . import api_transport

    del language
    return {
        "models": [{"label": option["label"], "id": option["id"]} for option in api_transport.AGENT_MODEL_OPTIONS],
        "thinking_levels": [
            {"label": "Extra High" if level == "xhigh" else level.capitalize(), "value": level}
            for level in api_transport.AGENT_THINKING_LEVELS
        ],
        "source": "static_product_list",
    }


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
    state: str = "awaiting_optimized_brief_confirmation",
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


def _cassette_language_for_run(args: dict, delivery: dict | None = None) -> str:
    explicit = _normalize_cassette_language(args.get("cassette_language") or args.get("language"))
    if explicit:
        return explicit
    session_id = str(args.get("session_id") or "").strip()
    platform = (delivery or {}).get("platform")
    if session_id:
        return _cassette_language_for_session(session_id, platform)
    return _default_cassette_language_for_platform(platform)


def _cassette_run_job_tool_chain_guard(language: str = "zh") -> str:
    language = _normalize_cassette_language(language) or "zh"
    return (
        "Use the direct Cassette run path: call cassette_run_job with message set to the edit instruction "
        "EXACTLY as written above — never rewrite, optimize, summarize, translate, or expand it — with the "
        f"same cassette session_id and cassette_language='{language}'. "
        "Do not call cassette_make_prompt; the plugin sends the message to the Cassette agent verbatim and "
        "the agent reads the session's uploaded media itself. "
        "When the instruction expresses finish/export intent, also pass export=true; otherwise omit export — "
        "the turn ends with the edit committed plus a timeline preview, not a render. "
        "For QQ, Telegram, or Weixin gateway sessions, call cassette_run_job with wait=false so gateway commands such as /cut remain responsive while the plugin sends progress and final notifications through the stored delivery target. "
        "After cassette_run_job returns a gateway background job, tell the user the Cassette job has started and end the turn; do not call cassette_job_status repeatedly unless the user explicitly asks for status. "
        "This must use the same model-selection notice, progress notifications, terminal status reporting, and gateway delivery behavior as every other Cassette job."
    )


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
            "cassette_make_prompt, or cassette_run_job; only report the saved BGM material status."
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
