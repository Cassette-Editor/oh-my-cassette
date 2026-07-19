from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import CassetteError
from .manifest import get_asset_root, now_iso


def get_jobs_dir() -> Path:
    path = get_asset_root() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(job_id: str) -> Path:
    value = str(job_id or "")
    if not value or Path(value).name != value or value in {".", ".."}:
        raise CassetteError("invalid_job_id", "Job ID must be a single path-safe identifier")
    return get_jobs_dir() / f"{value}.json"


def _redact_prompt(prompt: str) -> str:
    return f"<redacted:{len(prompt or '')} chars>"


def _job_timeout_sec(options: dict) -> int:
    default = int(os.getenv("CASSETTE_BROWSER_TIMEOUT_SEC", "1800"))
    requested = int(options.get("timeout_sec") or default)
    minimum = int(os.getenv("CASSETTE_MIN_BROWSER_TIMEOUT_SEC", "1800"))
    if minimum > 0:
        return max(requested, minimum)
    return requested


def create_job(session_hash: str, prompt: str, instruction: str | None, asset_paths: list[str], options: dict | None = None) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    job_id = f"cassette_{ts}_{secrets.token_hex(3)}"
    options = options or {}
    job = {
        "job_id": job_id,
        "session_hash": session_hash,
        "cassette_session_id": options.get("cassette_session_id") or "",
        "status": "queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "started_at": None,
        "finished_at": None,
        "prompt_redacted": _redact_prompt(prompt),
        "prompt": prompt,
        "chat_message": options.get("chat_message") or instruction or prompt,
        "instruction": instruction or "",
        "asset_paths": asset_paths,
        "url": options.get("url") or os.getenv("CASSETTE_URL", "https://sg.trycassette.online/agent"),
        "timeout_sec": _job_timeout_sec(options),
        "selectors": options.get("selectors") or {},
        "model_selection": options.get("model_selection") or {},
        "cassette_language": options.get("cassette_language") or "",
        "delivery": options.get("delivery") or {},
        "outputs": [],
        "questions": [],
        "errors": [],
        "quality": {},
        "final_screenshot": None,
        "worker_pid": None,
    }
    save_job(job)
    return job


def load_job(job_id: str) -> dict:
    path = _job_path(job_id)
    if not path.exists():
        raise CassetteError("job_not_found", f"Job {job_id} was not found")
    with path.open("r", encoding="utf-8") as fh:
        job = json.load(fh)
    if not isinstance(job, dict) or str(job.get("job_id") or "") != str(job_id):
        raise CassetteError("job_identity_mismatch", "Persisted job identity does not match its filename")
    return job


def save_job(job: dict) -> None:
    path = _job_path(job["job_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    job["updated_at"] = now_iso()
    fd, tmp_name = tempfile.mkstemp(prefix=".job.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(job, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def update_job(job_id: str, **fields: Any) -> dict:
    job = load_job(job_id)
    job.update(fields)
    terminal_statuses = {"succeeded", "failed", "needs_user", "cancelled", "timed_out"}
    if fields.get("status") in terminal_statuses and not job.get("finished_at"):
        job["finished_at"] = now_iso()
    save_job(job)
    return job


def merge_persisted_runtime_fields(job: dict) -> dict:
    """Keep browser-side progress updates written while a job is running."""
    try:
        persisted = load_job(job["job_id"])
    except Exception:
        return job
    for field in (
        "progress_events",
        "stage_timings",
        "current_stage",
        "progress_snapshot_notifications",
        "model_selection",
        "model_selection_notification",
        "cassette_language",
        "language_selection",
        "browser_events",
        "continuation",
        "resume_request",
    ):
        if field in {"continuation", "resume_request"} and field in persisted:
            job[field] = persisted.get(field)
        elif persisted.get(field) and not job.get(field):
            job[field] = persisted[field]
    return job


def list_jobs(session_hash: str | None = None, limit: int = 10) -> list[dict]:
    items: list[dict] = []
    for path in sorted(get_jobs_dir().glob("cassette_*.json"), reverse=True):
        try:
            with path.open("r", encoding="utf-8") as fh:
                job = json.load(fh)
            if session_hash and job.get("session_hash") != session_hash:
                continue
            job.pop("prompt", None)
            job.pop("asset_paths", None)
            job.pop("worker_command", None)
            job.pop("continuation", None)
            job.pop("resume_request", None)
            job.pop("delivery", None)
            public_outputs = []
            for output in job.get("outputs") or []:
                if isinstance(output, dict):
                    cleaned = {k: v for k, v in output.items() if k != "local_path"}
                    if output.get("local_path"):
                        cleaned["downloaded"] = True
                        cleaned["filename"] = Path(str(output.get("local_path"))).name
                    public_outputs.append(cleaned)
            job["outputs"] = public_outputs
            items.append(job)
        except Exception:
            continue
        if len(items) >= max(1, limit):
            break
    return items


def request_cancel(job_id: str, **fields: Any) -> dict:
    return update_job(job_id, status="cancel_requested", **fields)


def is_cancel_requested(job_id: str) -> bool:
    try:
        return load_job(job_id).get("status") == "cancel_requested"
    except CassetteError:
        return False


def start_worker(job_id: str, *, action: str = "run", response: str | None = None) -> dict:
    job = load_job(job_id)
    if action not in {"run", "resume"}:
        raise CassetteError("invalid_worker_action", f"Unsupported worker action: {action}")
    cmd = [
        sys.executable,
        str(Path(__file__).resolve().parent / "worker.py"),
        "--job-id",
        job_id,
        "--action",
        action,
    ]
    env = os.environ.copy()
    previous = dict(job)
    job["status"] = "running"
    job["started_at"] = now_iso()
    job["finished_at"] = None
    if action == "resume":
        # Persist before spawning so a fast detached worker can never observe an
        # empty response. This field is private and stripped from public results.
        job["resume_request"] = {"response": str(response or "")}
    save_job(job)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    except Exception as exc:
        save_job(previous)
        raise CassetteError("internal_error", "Failed to start Cassette background worker", {"reason": type(exc).__name__}) from exc
    # Reload so a worker that already advanced the job is never overwritten by
    # the parent's stale pre-spawn copy.
    return update_job(job_id, worker_pid=proc.pid, worker_command=cmd)
