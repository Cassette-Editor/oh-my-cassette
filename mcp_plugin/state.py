"""Persisted typed state machine for local MCP sessions and jobs."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from .models import SessionPhase, SessionState
from runtime_config import data_root, ensure_private_dir, read_protected_json, write_protected_json


class InvalidTransition(RuntimeError):
    def __init__(self, current: SessionPhase, target: SessionPhase):
        super().__init__(f"invalid Cassette state transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


_TERMINAL = {
    SessionPhase.EXPORTED,
    SessionPhase.SUCCEEDED,
    SessionPhase.FAILED,
    SessionPhase.CANCELLED,
    SessionPhase.TIMED_OUT,
}

_ALLOWED: dict[SessionPhase, set[SessionPhase]] = {
    # Persisted jobs can be discovered after a host restart before the session-state file is read.
    # Those transitions are recovery from authoritative job state, not model-inferred routing.
    SessionPhase.NEW: {
        SessionPhase.GUIDED_CHOICES,
        SessionPhase.ASSETS_READY,
        SessionPhase.READY,
        SessionPhase.RUNNING,
        SessionPhase.NEEDS_USER,
        SessionPhase.REVIEW_REQUIRED,
        SessionPhase.EXPORTING,
        SessionPhase.EXPORTED,
        SessionPhase.SUCCEEDED,
        SessionPhase.FAILED,
        SessionPhase.CANCELLED,
        SessionPhase.TIMED_OUT,
    },
    SessionPhase.GUIDED_CHOICES: {SessionPhase.ASSETS_READY, SessionPhase.READY},
    SessionPhase.ASSETS_READY: {SessionPhase.GUIDED_CHOICES, SessionPhase.READY, SessionPhase.RUNNING},
    SessionPhase.READY: {SessionPhase.GUIDED_CHOICES, SessionPhase.ASSETS_READY, SessionPhase.RUNNING},
    SessionPhase.RUNNING: {
        SessionPhase.NEEDS_USER,
        SessionPhase.REVIEW_REQUIRED,
        SessionPhase.EXPORTING,
        SessionPhase.EXPORTED,
        SessionPhase.SUCCEEDED,
        SessionPhase.FAILED,
        SessionPhase.CANCELLED,
        SessionPhase.TIMED_OUT,
    },
    SessionPhase.NEEDS_USER: {
        SessionPhase.RUNNING,
        SessionPhase.REVIEW_REQUIRED,
        SessionPhase.FAILED,
        SessionPhase.CANCELLED,
    },
    SessionPhase.REVIEW_REQUIRED: {
        SessionPhase.RUNNING,
        SessionPhase.EXPORTING,
        SessionPhase.NEEDS_USER,
        SessionPhase.FAILED,
        SessionPhase.CANCELLED,
    },
    SessionPhase.EXPORTING: {
        SessionPhase.EXPORTED,
        SessionPhase.SUCCEEDED,
        SessionPhase.FAILED,
        SessionPhase.CANCELLED,
        SessionPhase.TIMED_OUT,
    },
    SessionPhase.EXPORTED: set(),
    SessionPhase.SUCCEEDED: set(),
    SessionPhase.FAILED: set(),
    SessionPhase.CANCELLED: set(),
    SessionPhase.TIMED_OUT: set(),
}

for terminal in _TERMINAL:
    _ALLOWED[terminal].update(
        {SessionPhase.GUIDED_CHOICES, SessionPhase.ASSETS_READY, SessionPhase.READY, SessionPhase.RUNNING}
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def phase_from_job(job: dict) -> SessionPhase:
    status = str(job.get("status") or "").strip().lower()
    quality = job.get("quality") if isinstance(job.get("quality"), dict) else {}
    if status in {"queued", "running", "cancel_requested"}:
        return SessionPhase.RUNNING
    if status == "needs_user":
        if quality.get("completion_review_required"):
            return SessionPhase.REVIEW_REQUIRED
        return SessionPhase.NEEDS_USER
    if status == "succeeded":
        return SessionPhase.EXPORTED if job.get("outputs") else SessionPhase.SUCCEEDED
    if status == "failed":
        return SessionPhase.FAILED
    if status == "cancelled":
        return SessionPhase.CANCELLED
    if status == "timed_out":
        return SessionPhase.TIMED_OUT
    return SessionPhase.READY


def next_action_for(phase: SessionPhase, *, job_id: str | None = None, editor_url: str | None = None) -> str:
    action = _next_action_base(phase, job_id=job_id)
    # Live-view moments: hand the user the editor deep link exactly where watching helps
    # (running / question / review), never on the mechanical phases.
    if editor_url and phase in {
        SessionPhase.RUNNING,
        SessionPhase.EXPORTING,
        SessionPhase.NEEDS_USER,
        SessionPhase.REVIEW_REQUIRED,
    }:
        return f"{action} Watch live: {editor_url}"
    return action


def _next_action_base(phase: SessionPhase, *, job_id: str | None = None) -> str:
    if phase == SessionPhase.NEW:
        return "Call cassette_ingest_media with a trusted project media path."
    if phase == SessionPhase.GUIDED_CHOICES:
        return "Confirm model, thinking level, prompt optimization, and optional BGM choices."
    if phase == SessionPhase.ASSETS_READY:
        return "Call cassette_make_prompt with the editing instruction."
    if phase == SessionPhase.READY:
        return "Call cassette_run_job; MCP jobs run in the background unless wait=true is explicit."
    if phase in {SessionPhase.RUNNING, SessionPhase.EXPORTING}:
        suffix = f" for {job_id}" if job_id else ""
        return f"Call cassette_job_status{suffix} with wait_for_change_sec up to 30."
    if phase == SessionPhase.NEEDS_USER:
        return "Ask the user, then call cassette_answer_question with job_id and response."
    if phase == SessionPhase.REVIEW_REQUIRED:
        return "Review completion, then call cassette_review_completion; only decision=export renders."
    if phase in {SessionPhase.EXPORTED, SessionPhase.SUCCEEDED}:
        return "Present the validated artifact or start another edit in this session."
    if phase == SessionPhase.CANCELLED:
        return "The job is cancelled; start another edit when ready."
    return "Inspect the structured error and retry or start another edit."


class StateStore:
    def __init__(self, root: Path | None = None):
        selected = (root or data_root() / "mcp-state" / "sessions").expanduser()
        self.root = Path(os.path.abspath(str(selected)))
        ensure_private_dir(self.root)

    def _path(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def load(self, session_id: str) -> SessionState:
        raw = read_protected_json(self._path(session_id))
        if not raw:
            return SessionState(session_id=session_id, updated_at=now_iso())
        state = SessionState.model_validate(raw)
        if state.session_id != session_id:
            raise RuntimeError("session state identity mismatch")
        return state

    def transition(
        self,
        session_id: str,
        target: SessionPhase,
        *,
        job_id: str | None = None,
    ) -> SessionState:
        state = self.load(session_id)
        if target != state.phase and target not in _ALLOWED[state.phase]:
            raise InvalidTransition(state.phase, target)
        updated = state.model_copy(
            update={
                "phase": target,
                "job_id": job_id if job_id is not None else state.job_id,
                "revision": state.revision + 1,
                "updated_at": now_iso(),
            }
        )
        write_protected_json(self._path(session_id), updated.model_dump(mode="json"))
        return updated

    def sync_job(self, session_id: str, job: dict) -> SessionState:
        return self.transition(
            session_id,
            phase_from_job(job),
            job_id=str(job.get("job_id") or "") or None,
        )
