from __future__ import annotations

import secrets
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()
_SESSIONS: dict[str, dict[str, Any]] = {}
_CLOSED_SESSIONS: set[str] = set()
_MAX_EVENTS = 500
_MAX_LLM_MESSAGES = 80


def new_session_id() -> str:
    return f"web_{secrets.token_urlsafe(18).replace('-', '').replace('_', '')[:24]}"


def validate_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    if not value.startswith("web_"):
        raise ValueError("web session_id must start with web_")
    suffix = value[4:]
    if not suffix or len(value) > 96 or not all(ch.isalnum() or ch in {"_", "-"} for ch in value):
        raise ValueError("invalid web session_id")
    return value


def ensure_session(session_id: str | None = None) -> dict[str, Any]:
    sid = validate_session_id(session_id) if session_id else new_session_id()
    with _LOCK:
        if sid in _CLOSED_SESSIONS:
            raise ValueError("web session is closed")
        state = _SESSIONS.get(sid)
        if state is None:
            state = {
                "session_id": sid,
                "next_event_id": 1,
                "events": [],
                "llm_messages": [],
                "active_flow": None,
                "cancelled_flows": [],
            }
            _SESSIONS[sid] = state
        else:
            state.setdefault("cancelled_flows", [])
        return state


def close_session(session_id: str) -> None:
    sid = validate_session_id(session_id)
    with _LOCK:
        _SESSIONS.pop(sid, None)
        _CLOSED_SESSIONS.add(sid)


def is_closed(session_id: str) -> bool:
    sid = validate_session_id(session_id)
    with _LOCK:
        return sid in _CLOSED_SESSIONS


def reset_all() -> None:
    with _LOCK:
        _SESSIONS.clear()
        _CLOSED_SESSIONS.clear()


def add_event(
    session_id: str,
    *,
    role: str,
    text: str = "",
    kind: str = "message",
    attachment_path: str = "",
    attachment_type: str = "",
    job_id: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _LOCK:
        if validate_session_id(session_id) in _CLOSED_SESSIONS:
            raise ValueError("web session is closed")
        state = ensure_session(session_id)
        event_id = int(state["next_event_id"])
        state["next_event_id"] = event_id + 1
        event = {
            "id": event_id,
            "session_id": state["session_id"],
            "role": role,
            "kind": kind,
            "text": str(text or ""),
            "attachment_path": str(attachment_path or ""),
            "attachment_type": str(attachment_type or ""),
            "job_id": str(job_id or ""),
        }
        if extra:
            event.update(extra)
        state["events"].append(event)
        state["events"] = state["events"][-_MAX_EVENTS:]
        return public_event(event)


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: deepcopy(value) for key, value in event.items() if key not in {"attachment_path"}}
    if event.get("attachment_path"):
        cleaned["has_attachment"] = True
        cleaned["attachment_name"] = Path(str(event["attachment_path"])).name
        cleaned["attachment_url"] = f"/api/events/{event['id']}/attachment?session_id={event['session_id']}"
    return cleaned


def get_events(session_id: str, after: int = 0) -> list[dict[str, Any]]:
    with _LOCK:
        state = ensure_session(session_id)
        return [public_event(event) for event in state["events"] if int(event.get("id") or 0) > after]


def get_event(session_id: str, event_id: int) -> dict[str, Any] | None:
    with _LOCK:
        state = ensure_session(session_id)
        for event in state["events"]:
            if int(event.get("id") or 0) == int(event_id):
                return deepcopy(event)
    return None


def get_llm_messages(session_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        state = ensure_session(session_id)
        return deepcopy(state["llm_messages"])


def set_llm_messages(session_id: str, messages: list[dict[str, Any]]) -> None:
    with _LOCK:
        state = ensure_session(session_id)
        state["llm_messages"] = deepcopy(messages[-_MAX_LLM_MESSAGES:])


def begin_flow(session_id: str, kind: str) -> str | None:
    with _LOCK:
        state = ensure_session(session_id)
        active = state.get("active_flow")
        if isinstance(active, dict) and not active.get("cancelled"):
            return None
        token = secrets.token_urlsafe(12)
        state["active_flow"] = {"token": token, "kind": str(kind or "flow"), "cancelled": False}
        cancelled = [str(item) for item in (state.get("cancelled_flows") or []) if item]
        state["cancelled_flows"] = cancelled[-20:]
        return token


def end_flow(session_id: str, token: str) -> None:
    with _LOCK:
        state = ensure_session(session_id)
        active = state.get("active_flow")
        if isinstance(active, dict) and active.get("token") == token:
            state["active_flow"] = None


def cancel_flow(session_id: str) -> bool:
    with _LOCK:
        state = ensure_session(session_id)
        active = state.get("active_flow")
        if not isinstance(active, dict):
            return False
        active["cancelled"] = True
        state["active_flow"] = active
        token = str(active.get("token") or "")
        cancelled = [str(item) for item in (state.get("cancelled_flows") or []) if item]
        if token and token not in cancelled:
            cancelled.append(token)
        state["cancelled_flows"] = cancelled[-20:]
        return True


def is_flow_active(session_id: str) -> bool:
    with _LOCK:
        state = ensure_session(session_id)
        active = state.get("active_flow")
        return bool(isinstance(active, dict) and not active.get("cancelled"))


def is_flow_cancelled(session_id: str, token: str | None = None) -> bool:
    with _LOCK:
        state = ensure_session(session_id)
        active = state.get("active_flow")
        cancelled = {str(item) for item in (state.get("cancelled_flows") or []) if item}
        if token is not None and str(token) in cancelled:
            return True
        if not isinstance(active, dict):
            return False
        if token is not None and active.get("token") != token:
            return False
        return bool(active.get("cancelled"))
