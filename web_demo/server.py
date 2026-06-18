from __future__ import annotations

import json
import mimetypes
import os
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cassette_loader import load_cassette_package
from . import deepseek_client, session_store

load_cassette_package()

from cassette import jobs, manifest, security, tools  # noqa: E402
from cassette.errors import CassetteError  # noqa: E402


STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Oh My Cassette Web Demo")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid session_id") from exc
    session_store.ensure_session(valid_session_id)
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/sessions")
def create_session() -> dict[str, str]:
    state = session_store.ensure_session()
    return {"session_id": state["session_id"], "language": _web_language(state["session_id"])}


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
    _ensure_upload_root_allowed()
    saved_paths: list[str] = []
    media_types: list[str] = []
    session_dir = _web_upload_root() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    for upload in files:
        filename = _safe_filename(upload.filename or "upload.bin")
        suffix = Path(filename).suffix.lower()
        if not suffix or suffix not in security.get_allowed_extensions():
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
    session_store.add_event(session_id, role="user", text=text, kind="message")
    result = tools.ingest_gateway_media(event=_make_event(session_id, text=text), gateway=_web_gateway())
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
        try:
            deepseek_result = deepseek_client.run_turn(
                session_id,
                str(result.get("text") or text),
                api_key_override=_api_key_override(x_deepseek_api_key),
            )
            return {"ok": True, "action": "llm", "result": result, "deepseek": deepseek_result}
        except deepseek_client.DeepSeekError as exc:
            message = _localized(session_id, f"DeepSeek 调用失败：{exc}", f"DeepSeek call failed: {exc}")
            session_store.add_event(session_id, role="assistant", text=message, kind="error")
            return {"ok": False, "action": "llm_error", "error": str(exc)}
    return {"ok": True, "action": "ignored", "result": result}


@app.get("/api/assets")
def get_assets(session_id: str = Query(...)) -> dict[str, Any]:
    session_id = _require_session(session_id)
    return _tool_payload(tools.cassette_list_assets({"session_id": session_id}))


@app.get("/api/jobs")
def get_jobs(session_id: str = Query(...), limit: int = Query(10)) -> dict[str, Any]:
    session_id = _require_session(session_id)
    payload = _tool_payload(tools.cassette_job_status({"session_id": session_id, "limit": limit}))
    for job in ((payload.get("data") or {}).get("jobs") or []):
        if isinstance(job, dict) and job.get("job_id"):
            try:
                full = jobs.load_job(str(job["job_id"]))
                if _job_belongs_to_session(full, session_id):
                    job["downloads"] = _job_download_urls(full, session_id)
            except Exception:
                job["downloads"] = []
    return payload


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    session_id = _require_session(str(payload.get("session_id") or ""))
    _require_job(session_id, job_id)
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
    job = _require_job(session_id, job_id)
    for output in job.get("outputs") or []:
        if not isinstance(output, dict) or not output.get("local_path"):
            continue
        path = _safe_asset_path(str(output["local_path"]))
        if path.name == filename:
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return FileResponse(path, media_type=media_type, filename=path.name)
    raise HTTPException(status_code=404, detail="output not found")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "web_demo.server:app",
        host=os.getenv("OMC_WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("OMC_WEB_PORT", "8080")),
        reload=False,
    )
