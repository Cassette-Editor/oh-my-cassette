from __future__ import annotations

import concurrent.futures
import json
import mimetypes
import os
import signal
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .cassette_loader import load_cassette_package
from . import deepseek_client, logging_utils, session_store

load_cassette_package()

from cassette import jobs, manifest, security, tools  # noqa: E402
from cassette.errors import CassetteError  # noqa: E402


# The web demo UI is the built Vite/React app under frontend/dist.
# Build it with web_demo/build_frontend.sh (or `npm run build` in web_demo/frontend).
STATIC_DIR = Path(__file__).resolve().parent / "frontend" / "dist"
_ACTIVE_JOB_STATUSES = {"queued", "running", "cancel_requested"}
_LLM_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=int(os.getenv("OMC_WEB_LLM_WORKERS", "4")), thread_name_prefix="omc-web-llm")

app = FastAPI(title="Oh My Cassette Web Demo")
# Mount only when the frontend has been built so the module still imports
# (e.g. for tests) before `npm run build` has produced frontend/dist.
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _security_headers(request, call_next):
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:
        logging_utils.log_event(
            "http_exception",
            method=request.method,
            path=str(request.url.path),
            client=getattr(request.client, "host", ""),
            error_type=type(exc).__name__,
        )
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    path = str(request.url.path)
    if request.method != "GET" or response.status_code >= 400 or path == "/":
        logging_utils.log_event(
            "http_request",
            method=request.method,
            path=path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            client=getattr(request.client, "host", ""),
        )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


def _asset_root() -> Path:
    return manifest.get_asset_root()


def _web_upload_root() -> Path:
    path = _asset_root() / "web_uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_upload_root_allowed() -> None:
    root = str(_web_upload_root())
    raw = os.getenv("CASSETTE_ALLOWED_SOURCE_ROOTS", "")
    roots = [item for item in raw.split(os.pathsep) if item]
    if root not in roots:
        roots.append(root)
        os.environ["CASSETTE_ALLOWED_SOURCE_ROOTS"] = os.pathsep.join(roots)
    os.environ.setdefault(
        "CASSETTE_ALLOWED_EXTENSIONS",
        ".mp4,.mov,.m4v,.webm,.jpg,.jpeg,.png,.webp,.gif,.mp3,.wav,.m4a,.aac",
    )


def _safe_filename(filename: str) -> str:
    name = Path(filename or "upload.bin").name
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", ".", " "} else "_" for ch in name).strip()
    return cleaned or "upload.bin"


def _public_source(session_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(value="web"),
        chat_id=session_id,
        user_id=session_id,
        chat_type="web",
    )


class _WebAdapter:
    platform = SimpleNamespace(value="web")

    def send(self, chat_id: str, text: str, metadata: dict | None = None) -> dict:
        del metadata
        event = session_store.add_event(str(chat_id), role="assistant", text=text, kind="message")
        logging_utils.log_event("web_outbox_send", session_id=str(chat_id), event_id=event.get("id"), text_len=len(text or ""))
        return {"success": True, "message_id": str(event["id"])}


def _web_gateway() -> SimpleNamespace:
    return SimpleNamespace(_is_user_authorized=lambda _event: True, adapters={"web": _WebAdapter()})


def _make_event(session_id: str, *, text: str = "", media_paths: list[str] | None = None, media_types: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        source=_public_source(session_id),
        media_urls=media_paths or [],
        media_types=media_types or [],
        text=text,
        message_id=f"web_{uuid.uuid4().hex}",
    )


def _tool_payload(result: str) -> dict[str, Any]:
    try:
        payload = json.loads(result)
    except Exception:
        payload = {"ok": False, "error": {"code": "invalid_tool_json"}}
    return payload if isinstance(payload, dict) else {"ok": False, "error": {"code": "invalid_tool_payload"}}


def _require_session(session_id: str) -> str:
    try:
        valid_session_id = session_store.validate_session_id(session_id)
        session_store.ensure_session(valid_session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc
    return valid_session_id


def _session_hash(session_id: str) -> str:
    return manifest.resolve_session_hash(session_id=session_id)


def _job_belongs_to_session(job: dict, session_id: str) -> bool:
    if str(job.get("cassette_session_id") or "") == session_id:
        return True
    try:
        return job.get("session_hash") == _session_hash(session_id)
    except Exception:
        return False


def _require_job(session_id: str, job_id: str) -> dict:
    try:
        job = jobs.load_job(job_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    if not _job_belongs_to_session(job, session_id):
        raise HTTPException(status_code=403, detail="job does not belong to this session")
    return job


def _require_web_job(session_id: str, job_id: str) -> dict:
    job = _require_job(session_id, job_id)
    if not _web_job_is_owned(job, session_id, _session_hash(session_id)):
        raise HTTPException(status_code=403, detail="job does not belong to this web session")
    return job


def _safe_asset_path(path: str) -> Path:
    candidate = Path(path).expanduser().resolve()
    root = _asset_root().resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="file is outside Cassette asset root") from exc
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return candidate


def _job_download_urls(job: dict, session_id: str) -> list[dict[str, str]]:
    outputs: list[dict[str, str]] = []
    for output in job.get("outputs") or []:
        if not isinstance(output, dict):
            continue
        local_path = str(output.get("local_path") or "")
        if not local_path:
            continue
        path = _safe_asset_path(local_path)
        outputs.append({
            "filename": path.name,
            "url": f"/api/jobs/{quote(str(job.get('job_id') or ''))}/outputs/{quote(path.name)}?session_id={quote(session_id)}",
        })
    return outputs


def _job_log_url(job: dict, session_id: str) -> str:
    return f"/api/jobs/{quote(str(job.get('job_id') or ''))}/log?session_id={quote(session_id)}"


def _public_job_log(job: dict) -> str:
    lines: list[str] = []

    def add(label: str, value: Any) -> None:
        if value not in (None, "", [], {}):
            lines.append(f"{label}: {value}")

    add("job_id", job.get("job_id"))
    add("status", job.get("status"))
    add("cassette_session_id", job.get("cassette_session_id"))
    add("session_hash", job.get("session_hash"))
    add("cassette_language", job.get("cassette_language"))
    add("current_stage", job.get("current_stage"))
    add("created_at", job.get("created_at"))
    add("started_at", job.get("started_at"))
    add("updated_at", job.get("updated_at"))
    add("finished_at", job.get("finished_at"))
    add("worker_kind", job.get("worker_kind"))
    add("worker_pid", job.get("worker_pid"))
    add("prompt_redacted", job.get("prompt_redacted"))
    add("instruction", job.get("instruction"))
    add("chat_message", job.get("chat_message"))
    add("model_selection", json.dumps(job.get("model_selection"), ensure_ascii=False, sort_keys=True) if job.get("model_selection") else "")
    add("language_selection", json.dumps(job.get("language_selection"), ensure_ascii=False, sort_keys=True) if job.get("language_selection") else "")
    if job.get("stage_timings"):
        lines.append("\n[stage_timings]")
        lines.append(json.dumps(job.get("stage_timings"), ensure_ascii=False, indent=2, sort_keys=True))
    if job.get("quality"):
        lines.append("\n[quality]")
        lines.append(json.dumps(job.get("quality"), ensure_ascii=False, indent=2, sort_keys=True))
    if job.get("errors"):
        lines.append("\n[errors]")
        lines.append(json.dumps(job.get("errors"), ensure_ascii=False, indent=2, sort_keys=True))
    if job.get("questions"):
        lines.append("\n[questions]")
        lines.append(json.dumps(job.get("questions"), ensure_ascii=False, indent=2, sort_keys=True))
    if job.get("progress_events"):
        lines.append("\n[progress_events]")
        for event in job.get("progress_events") or []:
            if isinstance(event, dict):
                lines.append(json.dumps(event, ensure_ascii=False, sort_keys=True))
    if job.get("browser_events"):
        lines.append("\n[browser_events]")
        for event in job.get("browser_events") or []:
            if isinstance(event, dict):
                lines.append(json.dumps(event, ensure_ascii=False, sort_keys=True))
    if job.get("progress_snapshot_notifications"):
        lines.append("\n[progress_snapshot_notifications]")
        lines.append(json.dumps(job.get("progress_snapshot_notifications"), ensure_ascii=False, indent=2, sort_keys=True))
    if job.get("outputs"):
        lines.append("\n[outputs]")
        for output in job.get("outputs") or []:
            if not isinstance(output, dict):
                continue
            cleaned = {key: value for key, value in output.items() if key != "local_path"}
            if output.get("local_path"):
                cleaned["filename"] = Path(str(output["local_path"])).name
            lines.append(json.dumps(cleaned, ensure_ascii=False, sort_keys=True))
    screenshot = str(job.get("final_screenshot") or "")
    if screenshot:
        add("final_screenshot", Path(screenshot).name)
    return "\n".join(lines).strip() + "\n"


def _web_language(session_id: str) -> str:
    try:
        return tools._cassette_language_for_session(session_id, "web")
    except Exception:
        return "zh"


def _set_web_language(session_id: str, language: str) -> str:
    normalized = tools._normalize_cassette_language(language)
    if normalized not in {"zh", "en"}:
        raise HTTPException(status_code=400, detail="language must be zh or en")
    tools._save_cassette_language_preference(session_id, normalized, "web")
    return normalized


def _localized(session_id: str, zh: str, en: str) -> str:
    return en if _web_language(session_id) == "en" else zh


def _api_key_override(value: str | None) -> str:
    key = str(value or "").strip()
    if len(key) > 4096:
        raise HTTPException(status_code=400, detail="DeepSeek API key header is too large")
    return key


def _message_preview(text: str) -> str:
    return " ".join(str(text or "").split())[:120]


def _processing_message(session_id: str) -> str:
    return _localized(
        session_id,
        "已收到剪辑指令，正在调用 DeepSeek 编排 Cassette 流程。请稍等，结果会自动显示在这里。",
        "Got the edit instruction. Calling DeepSeek to orchestrate the Cassette flow; results will appear here automatically.",
    )


def _llm_error_message(session_id: str, error: str) -> str:
    return _localized(session_id, f"DeepSeek 调用失败：{error}", f"DeepSeek call failed: {error}")


def _add_web_event(session_id: str, *, role: str, text: str, kind: str = "message", **kwargs: Any) -> None:
    try:
        session_store.add_event(session_id, role=role, text=text, kind=kind, **kwargs)
    except Exception as exc:
        logging_utils.log_event("web_event_write_failed", session_id=session_id, kind=kind, error_type=type(exc).__name__)


def _run_llm_background(session_id: str, prompt_text: str, api_key_override: str) -> None:
    logging_utils.log_event("llm_background_start", session_id=session_id, prompt_len=len(prompt_text or ""), has_api_key_override=bool(api_key_override))
    try:
        result = deepseek_client.run_turn(session_id, prompt_text, api_key_override=api_key_override)
        logging_utils.log_event(
            "llm_background_done",
            session_id=session_id,
            tool_call_count=result.get("tool_call_count"),
            content_len=len(str(result.get("content") or "")),
        )
    except deepseek_client.DeepSeekError as exc:
        _add_web_event(session_id, role="assistant", text=_llm_error_message(session_id, str(exc)), kind="error")
        logging_utils.log_event("llm_background_error", session_id=session_id, error_type=type(exc).__name__, error=str(exc))
    except Exception as exc:
        _add_web_event(
            session_id,
            role="assistant",
            text=_localized(session_id, f"Web demo 后台处理失败：{type(exc).__name__}", f"Web demo background processing failed: {type(exc).__name__}"),
            kind="error",
        )
        logging_utils.log_event("llm_background_exception", session_id=session_id, error_type=type(exc).__name__)


def _submit_llm_background(session_id: str, prompt_text: str, api_key_override: str) -> None:
    _LLM_EXECUTOR.submit(_run_llm_background, session_id, prompt_text, api_key_override)


def _normalize_web_platform(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _is_web_platform(value: Any) -> bool:
    return _normalize_web_platform(value) in {"web", "browser", "web_demo", "webdemo"}


def _safe_remove_file(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
        resolved.relative_to(_asset_root().resolve())
    except Exception:
        return False
    try:
        if resolved.exists() and resolved.is_file():
            resolved.unlink()
            return True
    except OSError:
        return False
    return False


def _safe_remove_tree(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
        resolved.relative_to(_asset_root().resolve())
    except Exception:
        return False
    try:
        if resolved.exists() and resolved.is_dir():
            shutil.rmtree(resolved)
            return True
    except OSError:
        return False
    return False


def _web_manifest_dir_is_owned(session_id: str, session_hash: str) -> bool:
    path = manifest.get_manifest_path(session_hash)
    if not path.exists():
        return True
    try:
        data = manifest.load_manifest(session_hash)
    except Exception:
        return False
    delivery = data.get("delivery") if isinstance(data.get("delivery"), dict) else {}
    platform = str(delivery.get("platform") or "").strip()
    # Web demo sessions may have preferences/pending files before media is uploaded,
    # so an empty platform is still considered web-owned for a `web_` session.
    return not platform or _is_web_platform(platform)


def _web_job_is_owned(job: dict, session_id: str, session_hash: str) -> bool:
    delivery = job.get("delivery") if isinstance(job.get("delivery"), dict) else {}
    if not _is_web_platform(delivery.get("platform")):
        return False
    return str(job.get("cassette_session_id") or "") == session_id or str(job.get("session_hash") or "") == session_hash


def _parse_job_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _job_timeout_sec(job: dict) -> int:
    try:
        return max(1, int(job.get("timeout_sec") or os.getenv("CASSETTE_BROWSER_TIMEOUT_SEC", "1800")))
    except (TypeError, ValueError):
        return 1800


def _timeout_stale_web_job(job: dict, *, session_id: str = "") -> dict:
    if str(job.get("status") or "") not in _ACTIVE_JOB_STATUSES:
        return job
    delivery = job.get("delivery") if isinstance(job.get("delivery"), dict) else {}
    if not _is_web_platform(delivery.get("platform")):
        return job
    started = _parse_job_time(job.get("started_at") or job.get("created_at"))
    if started is None:
        return job
    timeout_sec = _job_timeout_sec(job)
    elapsed_sec = (datetime.now(timezone.utc) - started).total_seconds()
    if elapsed_sec <= timeout_sec:
        return job
    errors = list(job.get("errors") or [])
    if not any(isinstance(error, dict) and error.get("code") == "web_demo_job_timeout" for error in errors):
        errors.append({
            "code": "web_demo_job_timeout",
            "message": f"Web demo marked this Cassette job timed out after {timeout_sec} seconds.",
            "details": {
                "current_stage": job.get("current_stage") or "",
                "elapsed_sec": round(max(0.0, elapsed_sec), 1),
                "timeout_sec": timeout_sec,
            },
        })
    quality = dict(job.get("quality") or {})
    quality["web_demo_timeout_reconciled"] = True
    quality["timeout_sec"] = timeout_sec
    quality["elapsed_sec"] = round(max(0.0, elapsed_sec), 1)
    updated = jobs.update_job(
        str(job.get("job_id") or ""),
        status="timed_out",
        finished_at=jobs.now_iso(),
        errors=errors,
        quality=quality,
    )
    logging_utils.log_event(
        "web_job_timeout_reconciled",
        session_id=session_id or str(job.get("cassette_session_id") or ""),
        job_id=updated.get("job_id"),
        elapsed_sec=round(max(0.0, elapsed_sec), 1),
        timeout_sec=timeout_sec,
        current_stage=updated.get("current_stage"),
    )
    return updated


def _reconcile_stale_web_jobs_for_session(session_id: str) -> None:
    session_hash = _session_hash(session_id)
    for path in sorted(jobs.get_jobs_dir().glob("cassette_*.json"), reverse=True):
        try:
            job = jobs.load_job(path.stem)
        except Exception:
            continue
        if not _web_job_is_owned(job, session_id, session_hash):
            continue
        before = str(job.get("status") or "")
        updated = _timeout_stale_web_job(job, session_id=session_id)
        if before in _ACTIVE_JOB_STATUSES and str(updated.get("status") or "") == "timed_out":
            _add_web_event(
                session_id,
                role="assistant",
                kind="error",
                job_id=str(updated.get("job_id") or ""),
                text=_localized(
                    session_id,
                    f"Cassette 任务已超时退出：{updated.get('job_id')}",
                    f"Cassette job timed out: {updated.get('job_id')}",
                ),
            )


def _reconcile_stale_web_jobs_on_startup() -> None:
    for path in sorted(jobs.get_jobs_dir().glob("cassette_*.json"), reverse=True):
        try:
            job = jobs.load_job(path.stem)
        except Exception:
            continue
        delivery = job.get("delivery") if isinstance(job.get("delivery"), dict) else {}
        if not _is_web_platform(delivery.get("platform")):
            continue
        _timeout_stale_web_job(job)


def _terminate_worker_if_any(job: dict) -> bool:
    try:
        pid = int(job.get("worker_pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _remove_job_record(job_id: str) -> bool:
    return _safe_remove_file(jobs.get_jobs_dir() / f"{job_id}.json")


def _cleanup_web_session(session_id: str) -> dict[str, Any]:
    try:
        valid_session_id = session_store.validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc

    session_hash = manifest.resolve_session_hash(session_id=valid_session_id)
    session_store.close_session(valid_session_id)

    removed_uploads = _safe_remove_tree(_web_upload_root() / valid_session_id)
    removed_session_dir = False
    if _web_manifest_dir_is_owned(valid_session_id, session_hash):
        removed_session_dir = _safe_remove_tree(manifest.get_session_dir(session_hash))

    removed_outputs = 0
    removed_jobs = 0
    cancelled_jobs = 0
    terminated_workers = 0
    for path in sorted(jobs.get_jobs_dir().glob("cassette_*.json")):
        try:
            job = jobs.load_job(path.stem)
        except Exception:
            continue
        if not _web_job_is_owned(job, valid_session_id, session_hash):
            continue
        status = str(job.get("status") or "")
        if status in _ACTIVE_JOB_STATUSES:
            try:
                jobs.request_cancel(str(job.get("job_id") or path.stem))
                cancelled_jobs += 1
            except Exception:
                pass
            if _terminate_worker_if_any(job):
                terminated_workers += 1
            # Keep active job records long enough for workers to observe cancel_requested.
            continue
        for output in job.get("outputs") or []:
            if isinstance(output, dict) and output.get("local_path"):
                if _safe_remove_file(Path(str(output["local_path"]))):
                    removed_outputs += 1
        screenshot = str(job.get("final_screenshot") or "")
        if screenshot and _safe_remove_file(Path(screenshot)):
            removed_outputs += 1
        if _remove_job_record(str(job.get("job_id") or path.stem)):
            removed_jobs += 1

    result = {
        "ok": True,
        "session_id": valid_session_id,
        "session_hash": session_hash,
        "removed_uploads": removed_uploads,
        "removed_session_dir": removed_session_dir,
        "removed_outputs": removed_outputs,
        "removed_jobs": removed_jobs,
        "cancelled_jobs": cancelled_jobs,
        "terminated_workers": terminated_workers,
    }
    logging_utils.log_event("web_session_cleanup", **result)
    return result


def reconcile_stale_web_jobs_on_startup() -> None:
    _reconcile_stale_web_jobs_on_startup()


app.router.add_event_handler("startup", reconcile_stale_web_jobs_on_startup)


@app.get("/")
def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return PlainTextResponse(
            "Web demo frontend is not built. Run: cd web_demo/frontend && npm install && npm run build",
            status_code=503,
        )
    return FileResponse(index_file)


@app.post("/api/sessions")
def create_session(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    cleanup_result: dict[str, Any] | None = None
    cleanup_session_id = str((payload or {}).get("cleanup_session_id") or "").strip()
    if cleanup_session_id:
        try:
            cleanup_result = _cleanup_web_session(cleanup_session_id)
        except HTTPException:
            cleanup_result = {"ok": False, "error": "invalid_cleanup_session"}
    state = session_store.ensure_session()
    language = str((payload or {}).get("language") or "").strip()
    if language:
        _set_web_language(state["session_id"], language)
    response = {"session_id": state["session_id"], "language": _web_language(state["session_id"]), "cleanup": cleanup_result}
    logging_utils.log_event("web_session_created", session_id=state["session_id"], language=response["language"], cleaned_previous=bool(cleanup_result))
    return response


@app.post("/api/sessions/{session_id}/cleanup")
def cleanup_session(session_id: str) -> dict[str, Any]:
    return _cleanup_web_session(session_id)


@app.post("/api/sessions/{session_id}/language")
def set_session_language(session_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, str]:
    valid_session_id = _require_session(session_id)
    language = _set_web_language(valid_session_id, str(payload.get("language") or ""))
    return {"session_id": valid_session_id, "language": language}


@app.get("/api/events")
def get_events(session_id: str = Query(...), after: int = Query(0)) -> dict[str, Any]:
    session_id = _require_session(session_id)
    return {"events": session_store.get_events(session_id, after)}


@app.get("/api/events/{event_id}/attachment")
def get_event_attachment(event_id: int, session_id: str = Query(...)) -> FileResponse:
    session_id = _require_session(session_id)
    event = session_store.get_event(session_id, event_id)
    if not event or not event.get("attachment_path"):
        raise HTTPException(status_code=404, detail="attachment not found")
    path = _safe_asset_path(str(event["attachment_path"]))
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.post("/api/uploads")
def upload_media(
    session_id: str = Form(...),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    session_id = _require_session(session_id)
    logging_utils.log_event("web_upload_start", session_id=session_id, file_count=len(files or []))
    _ensure_upload_root_allowed()
    saved_paths: list[str] = []
    media_types: list[str] = []
    session_dir = _web_upload_root() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    for upload in files:
        filename = _safe_filename(upload.filename or "upload.bin")
        suffix = Path(filename).suffix.lower()
        if not suffix or suffix not in security.get_allowed_extensions():
            logging_utils.log_event("web_upload_rejected", session_id=session_id, filename=filename, reason="extension_not_allowed", suffix=suffix)
            raise HTTPException(status_code=400, detail=f"extension not allowed: {suffix}")
        target = session_dir / f"{uuid.uuid4().hex}_{filename}"
        try:
            with target.open("wb") as fh:
                shutil.copyfileobj(upload.file, fh)
            security.validate_size(target)
        except CassetteError as exc:
            try:
                target.unlink()
            except OSError:
                pass
            logging_utils.log_event("web_upload_rejected", session_id=session_id, filename=filename, reason=exc.code)
            raise HTTPException(status_code=400, detail=exc.code) from exc
        saved_paths.append(str(target))
        media_types.append(upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream")
        session_store.add_event(
            session_id,
            role="user",
            kind="upload",
            text=_localized(session_id, f"上传素材：{filename}", f"Uploaded asset: {filename}"),
            attachment_path=str(target),
            attachment_type=tools._mime_to_media_type(media_types[-1], str(target)),
        )
    result = tools.ingest_gateway_media(
        event=_make_event(session_id, media_paths=saved_paths, media_types=media_types),
        gateway=_web_gateway(),
    )
    logging_utils.log_event("web_upload_done", session_id=session_id, saved_count=len(saved_paths), action=(result or {}).get("action"), reason=(result or {}).get("reason"))
    return {"ok": True, "result": result, "events": session_store.get_events(session_id, 0)}


@app.post("/api/messages")
def send_message(
    payload: dict[str, Any] = Body(...),
    x_deepseek_api_key: str | None = Header(default=None, alias="X-DeepSeek-Api-Key"),
) -> dict[str, Any]:
    session_id = _require_session(str(payload.get("session_id") or ""))
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    session_store.ensure_session(session_id)
    if payload.get("language"):
        _set_web_language(session_id, str(payload.get("language") or ""))
    logging_utils.log_event("web_message_received", session_id=session_id, text_len=len(text), text_preview=_message_preview(text))
    session_store.add_event(session_id, role="user", text=text, kind="message")
    result = tools.ingest_gateway_media(event=_make_event(session_id, text=text), gateway=_web_gateway())
    logging_utils.log_event(
        "web_message_gateway_result",
        session_id=session_id,
        action=(result or {}).get("action") if isinstance(result, dict) else "",
        reason=(result or {}).get("reason") if isinstance(result, dict) else "",
    )
    if result is None:
        asset_payload = _tool_payload(tools.cassette_list_assets({"session_id": session_id}))
        assets = (((asset_payload.get("data") or {}).get("manifest") or {}).get("assets") or [])
        reply = (
            _localized(session_id, "请先上传视频、图片或音频素材，然后发送剪辑指令。", "Please upload video, image, or audio assets first, then send an edit instruction.")
            if not assets
            else _localized(session_id, "收到。可以继续发送剪辑指令，或使用 /check_assets 查看素材。", "Got it. You can continue with an edit instruction, or use /check_assets to inspect assets.")
        )
        session_store.add_event(session_id, role="assistant", text=reply, kind="message")
        return {"ok": True, "action": "local_reply", "result": result}
    if result.get("action") == "skip":
        return {"ok": True, "action": "skip", "result": result}
    if result.get("action") == "rewrite":
        api_key = _api_key_override(x_deepseek_api_key)
        session_store.add_event(session_id, role="assistant", text=_processing_message(session_id), kind="status")
        _submit_llm_background(session_id, str(result.get("text") or text), api_key)
        logging_utils.log_event("web_message_llm_submitted", session_id=session_id, prompt_len=len(str(result.get("text") or text)), has_api_key_override=bool(api_key))
        return {"ok": True, "action": "llm_background", "result": result}
    return {"ok": True, "action": "ignored", "result": result}


@app.get("/api/assets")
def get_assets(session_id: str = Query(...)) -> dict[str, Any]:
    session_id = _require_session(session_id)
    return _tool_payload(tools.cassette_list_assets({"session_id": session_id}))


@app.get("/api/jobs")
def get_jobs(session_id: str = Query(...), limit: int = Query(10)) -> dict[str, Any]:
    session_id = _require_session(session_id)
    _reconcile_stale_web_jobs_for_session(session_id)
    payload = _tool_payload(tools.cassette_job_status({"session_id": session_id, "limit": limit}))
    visible_jobs: list[dict[str, Any]] = []
    for job in ((payload.get("data") or {}).get("jobs") or []):
        if isinstance(job, dict) and job.get("job_id"):
            try:
                full = jobs.load_job(str(job["job_id"]))
                if _web_job_is_owned(full, session_id, _session_hash(session_id)):
                    job["downloads"] = _job_download_urls(full, session_id)
                    job["log_url"] = _job_log_url(full, session_id)
                    visible_jobs.append(job)
            except Exception:
                job["downloads"] = []
    if isinstance(payload.get("data"), dict):
        payload["data"]["jobs"] = visible_jobs
    return payload


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    session_id = _require_session(str(payload.get("session_id") or ""))
    _require_web_job(session_id, job_id)
    result = _tool_payload(tools.cassette_cancel_job({"job_id": job_id}))
    session_store.add_event(
        session_id,
        role="assistant",
        text=_localized(session_id, "已请求暂停当前 Cassette 任务。", "Requested cancellation for the current Cassette job."),
        kind="message",
        job_id=job_id,
    )
    return result


@app.get("/api/jobs/{job_id}/outputs/{filename}")
def download_output(job_id: str, filename: str, session_id: str = Query(...)) -> FileResponse:
    session_id = _require_session(session_id)
    job = _require_web_job(session_id, job_id)
    for output in job.get("outputs") or []:
        if not isinstance(output, dict) or not output.get("local_path"):
            continue
        path = _safe_asset_path(str(output["local_path"]))
        if path.name == filename:
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return FileResponse(path, media_type=media_type, filename=path.name)
    raise HTTPException(status_code=404, detail="output not found")


@app.get("/api/jobs/{job_id}/log")
def download_job_log(job_id: str, session_id: str = Query(...)) -> PlainTextResponse:
    session_id = _require_session(session_id)
    job = _require_web_job(session_id, job_id)
    return PlainTextResponse(
        _public_job_log(job),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{job_id}.log"'},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "web_demo.server:app",
        host=os.getenv("OMC_WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("OMC_WEB_PORT", "8080")),
        reload=False,
    )
