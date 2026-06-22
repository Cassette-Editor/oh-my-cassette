"""API-driven Cassette transport.

Replaces Playwright DOM scraping with direct calls to the Cassette server APIs:

  auth    POST {API}/api/agent-auth/verify            -> Supabase JWT (+ registers agent session row)
  upload  POST {API}/api/media/upload/init            -> presigned PUT url
          PUT  <presigned url>                          (raw bytes)
          POST {API}/api/media/upload/complete        -> mediaFileId
          GET  {API}/api/media/upload/status?key=     -> poll until 'completed' (video)
  agent   POST {API}/api/langgraph/threads            -> thread_id
          POST {API}/api/langgraph/threads/{id}/runs  -> run_id (server-side edits commit to the project)
          GET  .../runs/{run_id}                        -> poll status
          GET  .../state                                -> interrupts (only editor_navigate is browser-bound)
          POST .../runs (command.resume)                -> satisfy interrupts headlessly
  export  POST {API}/api/export/projects/{sid}/jobs    -> render the stored project (no browser manifest)
          GET  {API}/api/export/jobs/{id}              -> poll until done
          GET  {API}/api/export/jobs/{id}/file         -> download mp4 to disk

It returns the SAME result dict shape as browser.run_cassette_browser_job_threaded
(status / outputs / questions / errors / quality / final_screenshot) so jobs/notifier/
_scrub_job/_job_report are unaffected. Selected via CASSETTE_TRANSPORT=api (default browser).

NOTE: auth / upload / export / download / result-synthesis are coded against verified
server contracts. The LangGraph run/interrupt wire format (run input, sessionContext config,
resume command shapes) is the surface that must be confirmed during live bring-up — it is
isolated in _run_agent / _resume_value and centralized in _LG_* path builders for that reason.
"""
from __future__ import annotations

import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .manifest import get_asset_root

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
# (useAgentModelPrefsStore DEFAULT_MODEL), so the api transport matches the browser/UI flow. The
# plugin's model_selection holds UI labels (or is empty), so it is only forwarded when it already
# names a product model id; otherwise the configured/default model is used.
DEFAULT_AGENT_MODEL_ID = "deepseek/deepseek-v4-flash"
_SUPPORTED_AGENT_MODEL_IDS = frozenset({
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "openai/gpt-5.4-mini",
})
_DEFAULT_THINKING = "low"  # matches cassette-config DEFAULT_THINKING / per-model defaultThinking


class ApiTransportError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _env(name: str) -> str:
    return str(os.getenv(name, "") or "").strip()


# Deployed Cassette render-server API origin (VITE_REMOTION_RENDER_SERVER_URL in the shipped
# frontend bundle). This is a single request-routed Cloud Run service for both regions and is NOT
# the editor SPA route in CASSETTE_URL (which ends in /agent). Override with CASSETTE_API_URL for
# self-hosted / non-default deployments.
DEFAULT_CASSETTE_API_URL = "https://remotion-canvas-server-5tdb2hkb4q-as.a.run.app"


def _api_base() -> str:
    """Render-server API origin serving /api/agent-auth, /api/media, /api/langgraph,
    /api/projects and /api/export. Defaults to the deployed Cassette API; override per env."""
    base = _env("CASSETTE_API_URL") or _env("CASSETTE_API_BASE_URL") or DEFAULT_CASSETTE_API_URL
    return base.rstrip("/")


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


def _http_timeout() -> float:
    try:
        return max(5.0, float(_env("CASSETTE_API_HTTP_TIMEOUT_SEC") or "60"))
    except ValueError:
        return 60.0


class ApiTransport:
    def __init__(self) -> None:
        self._token: str | None = None
        self._is_full_user: bool = False

    # ── public Transport surface ──────────────────────────────────────────────
    def check_available(self) -> bool:
        email, password = _credentials()
        return bool(_api_base() and email and password)

    def close_sessions(self, session_key: str | None = None) -> None:
        # Stateless over HTTP — just drop the cached token.
        self._token = None

    def export(self, job: dict, decision: dict[str, Any] | None = None) -> dict:
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        outputs: list[dict] = []
        errors: list[dict] = []
        try:
            self._authenticate()
            outputs = self._export_project(session_id, job_id)
        except ApiTransportError as exc:
            errors.append(self._error(exc))
        except Exception as exc:  # noqa: BLE001 — never let export crash the job record
            errors.append(self._error(exc))
        status = _SUCCEEDED if outputs else _FAILED
        return self._result(status, outputs=outputs, errors=errors)

    def run_job(self, job: dict) -> dict:
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        prompt = str(job.get("prompt") or job.get("chat_message") or "").strip()
        asset_paths = [p for p in (job.get("asset_paths") or []) if p]
        questions: list[dict] = []
        errors: list[dict] = []
        deadline = time.monotonic() + self._job_timeout(job)

        try:
            if not _api_base():
                raise ApiTransportError("api_base_missing", "CASSETTE_API_URL is not configured for the API transport")
            self._authenticate()

            media_file_ids: list[str] = []
            for path in asset_paths:
                media_file_ids.append(self._upload_asset(path, session_id, deadline))

            thread_id = self._create_thread(session_id, job)
            run_status, run_questions = self._run_agent(thread_id, session_id, prompt, job, deadline)
            questions.extend(run_questions)

            if run_status == _CANCELLED:
                return self._result(_CANCELLED, questions=questions, errors=errors,
                                    completion_observed=True, export_completed=False, risk="medium")
            if run_status == _NEEDS_USER:
                return self._result(_NEEDS_USER, questions=questions, errors=errors,
                                    completion_observed=True, export_completed=False, risk="medium")
            if run_status != _SUCCEEDED:
                errors.append({"code": "agent_run_incomplete", "message": f"Agent run ended with status '{run_status}'", "details": {}})
                return self._result(_FAILED, questions=questions, errors=errors,
                                    completion_observed=True, export_completed=False, risk="high")

            outputs = self._export_project(session_id, job_id, deadline=deadline)
            status = _SUCCEEDED if outputs else _SUCCEEDED  # edit done even if export yielded no link
            has_local = any(o.get("local_path") for o in outputs)
            return self._result(
                status,
                outputs=outputs,
                questions=questions,
                errors=errors,
                completion_observed=True,
                export_completed=bool(outputs),
                risk="low" if has_local else "medium",
            )
        except ApiTransportError as exc:
            errors.append(self._error(exc))
            return self._result(_FAILED, questions=questions, errors=errors,
                                completion_observed=False, export_completed=False, risk="high")
        except Exception as exc:  # noqa: BLE001
            errors.append(self._error(exc))
            return self._result(_FAILED, questions=questions, errors=errors,
                                completion_observed=False, export_completed=False, risk="high")

    # ── auth ──────────────────────────────────────────────────────────────────
    def _authenticate(self) -> None:
        if self._token:
            return
        email, password = _credentials()
        if not email or not password:
            raise ApiTransportError("auth_missing_credentials", "CASSETTE_AUTH_EMAIL/PASSWORD are required for the API transport")
        status, body = self._request("POST", "/api/agent-auth/verify", json_body={"email": email, "password": password}, authed=False)
        if status != 200 or not isinstance(body, dict):
            raise ApiTransportError("auth_failed", f"Cassette sign-in failed (HTTP {status})")
        session = body.get("session") or {}
        token = session.get("access_token")
        if not token:
            raise ApiTransportError("auth_failed", "Cassette sign-in returned no access token")
        self._token = str(token)
        self._is_full_user = bool(body.get("isFullUser"))

    # ── media upload ────────────────────────────────────────────────────────
    def _upload_asset(self, path: str, session_id: str, deadline: float) -> str:
        file_path = Path(path)
        if not file_path.exists():
            raise ApiTransportError("asset_missing", f"Asset not found on disk: {path}")
        file_name = file_path.name
        mime, _ = mimetypes.guess_type(file_name)
        mime = mime or "application/octet-stream"
        headers = {"x-session-id": session_id}

        _, init = self._request("POST", "/api/media/upload/init",
                                json_body={"fileName": file_name, "mimeType": mime}, headers=headers, expect=200)
        if not isinstance(init, dict) or not init.get("uploadUrl") or not init.get("key"):
            raise ApiTransportError("upload_init_failed", f"upload/init returned an unexpected body for {file_name}")
        key = str(init["key"])
        storage_backend = str(init.get("storageBackend") or "r2")

        self._put_bytes(str(init["uploadUrl"]), file_path.read_bytes(), mime)

        _, complete = self._request("POST", "/api/media/upload/complete",
                                    json_body={"key": key, "fileName": file_name, "mimeType": mime,
                                               "storageBackend": storage_backend, "metadata": {}},
                                    headers=headers, expect=200)
        if not isinstance(complete, dict) or not complete.get("mediaFileId"):
            raise ApiTransportError("upload_complete_failed", f"upload/complete returned no mediaFileId for {file_name}")

        media_file_id = str(complete["mediaFileId"])
        if str(mime).startswith("video/") and complete.get("uploadStatus") != "completed":
            self._poll_upload_completed(key, session_id, deadline)
        return media_file_id

    def _poll_upload_completed(self, key: str, session_id: str, deadline: float) -> None:
        headers = {"x-session-id": session_id}
        query = "?" + urlencode({"key": key})
        while time.monotonic() < deadline:
            _, body = self._request("GET", "/api/media/upload/status" + query, headers=headers, expect=200)
            if isinstance(body, dict) and body.get("uploadStatus") == "completed":
                return
            time.sleep(self._poll_interval())
        raise ApiTransportError("upload_processing_timeout", f"Media processing did not complete for key {key}")

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

    # ── agent run ─────────────────────────────────────────────────────────────
    def _create_thread(self, session_id: str, job: dict) -> str:
        metadata = {
            "graph_id": "cassette-chat",
            "projectId": session_id,
            "mediaSessionId": session_id,
            "chatSessionId": session_id,
        }
        _, body = self._request("POST", "/api/langgraph/threads",
                                json_body={"metadata": metadata, "if_exists": "do_nothing"}, expect=200)
        thread_id = isinstance(body, dict) and (body.get("thread_id") or body.get("threadId"))
        if not thread_id:
            raise ApiTransportError("thread_create_failed", "LangGraph thread create returned no thread_id")
        return str(thread_id)

    def _session_context(self, session_id: str, job: dict, prompt: str) -> dict:
        return {
            "projectId": session_id,
            "mediaSessionId": session_id,
            "chatSessionId": session_id,
            "threadId": session_id,
            "mode": "auto",
            "turnStrategy": "default",
            "turnKind": "conversation",
            "modelId": self._resolve_model_id(job),
            "thinkingConfig": self._resolve_thinking_config(job),
            "locale": job.get("cassette_language") or None,
            "currentUserRequest": prompt,
        }

    @staticmethod
    def _resolve_model_id(job: dict) -> str:
        ms = job.get("model_selection") or {}
        candidate = str(ms.get("modelId") or ms.get("model") or "").strip()
        if candidate in _SUPPORTED_AGENT_MODEL_IDS:
            return candidate
        return _env("CASSETTE_API_MODEL_ID") or DEFAULT_AGENT_MODEL_ID

    @staticmethod
    def _resolve_thinking_config(job: dict) -> str:
        # The editor thinking values are lowercase 'low'|'medium'|'high'; default 'low' matches the UI
        # (cassette-config). Honor an env override or the job's thinking selection (case-insensitive).
        valid = {"low", "medium", "high"}
        override = _env("CASSETTE_API_THINKING").lower()
        if override in valid:
            return override
        ms = job.get("model_selection") or {}
        raw = str(ms.get("thinkingConfig") or ms.get("thinking_level") or "").strip().lower()
        return raw if raw in valid else _DEFAULT_THINKING

    def _run_agent(self, thread_id: str, session_id: str, prompt: str, job: dict, deadline: float) -> tuple[str, list[dict]]:
        """Start the run, satisfy interrupts headlessly, return (terminal_status, questions)."""
        config = {
            "recursion_limit": self._recursion_limit(),
            "configurable": {"sessionContext": self._session_context(session_id, job, prompt)},
        }
        run_body = {
            "assistant_id": "cassette-chat",
            "input": {"messages": [{"type": "human", "content": prompt}]},
            "config": config,
            "multitask_strategy": "rollback",
        }
        run_id = self._post_run(thread_id, run_body)
        questions: list[dict] = []

        while True:
            status = self._await_run(thread_id, run_id, deadline)
            if status == "interrupted":
                interrupts = self._pending_interrupts(thread_id)
                if not interrupts:
                    # Interrupted with nothing pending == treat as needing user.
                    return _NEEDS_USER, questions
                resume_value, needs_user_questions = self._resume_value(interrupts)
                questions.extend(needs_user_questions)
                run_id = self._post_run(thread_id, {
                    "assistant_id": "cassette-chat",
                    "command": {"resume": resume_value},
                    "config": config,
                    "multitask_strategy": "interrupt",
                })
                continue
            if status == "success":
                return _SUCCEEDED, questions
            if status == "timeout":
                return _TIMED_OUT, questions
            raise ApiTransportError("agent_run_error", f"Agent run failed: {self._run_error_detail(thread_id)}")

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

    def _post_run(self, thread_id: str, body: dict) -> str:
        _, resp = self._request("POST", f"/api/langgraph/threads/{thread_id}/runs", json_body=body, expect=200)
        run_id = isinstance(resp, dict) and (resp.get("run_id") or resp.get("runId"))
        if not run_id:
            raise ApiTransportError("run_create_failed", "LangGraph run create returned no run_id")
        return str(run_id)

    def _await_run(self, thread_id: str, run_id: str, deadline: float) -> str:
        while time.monotonic() < deadline:
            _, body = self._request("GET", f"/api/langgraph/threads/{thread_id}/runs/{run_id}", expect=200)
            status = str((body or {}).get("status") or "") if isinstance(body, dict) else ""
            if status in _LG_TERMINAL:
                return status
            time.sleep(self._poll_interval())
        return "timeout"

    def _pending_interrupts(self, thread_id: str) -> list[dict]:
        _, state = self._request("GET", f"/api/langgraph/threads/{thread_id}/state", expect=200)
        out: list[dict] = []
        if not isinstance(state, dict):
            return out
        for task in state.get("tasks") or []:
            for interrupt in (task.get("interrupts") or []):
                value = interrupt.get("value") if isinstance(interrupt, dict) else None
                if isinstance(value, dict):
                    out.append({"id": interrupt.get("id"), "value": value})
        # Some LangGraph versions surface interrupts on the top-level __interrupt__ channel.
        for interrupt in (state.get("values", {}) or {}).get("__interrupt__", []) if isinstance(state.get("values"), dict) else []:
            if isinstance(interrupt, dict) and isinstance(interrupt.get("value"), dict):
                out.append({"id": interrupt.get("id"), "value": interrupt["value"]})
        return out

    def _resume_value(self, interrupts: list[dict]) -> tuple[Any, list[dict]]:
        """Build the resume payload. Headless tool interrupts (editor_navigate) resume KEYED by
        toolCall.id; typed interrupts (ask_user/edit_plan_review/mode_switch/init_questions) resume
        with a BARE object. This keyed-vs-bare distinction is the load-bearing headless rule."""
        questions: list[dict] = []
        # Headless tool interrupts can be batched; collect a keyed map.
        keyed: dict[str, Any] = {}
        for item in interrupts:
            value = item["value"]
            kind = value.get("type")
            if kind == "tool":
                tool_call = value.get("toolCall") or {}
                call_id = tool_call.get("id")
                if call_id:
                    # Only editor_navigate is browser-bound; ack any tool interrupt as a no-op so
                    # the headless run never hangs.
                    keyed[str(call_id)] = {"result": dict(_NAVIGATE_NOOP_RESULT)}
                continue
            # Typed interrupt — resume bare. Take the first one (LangGraph resumes one at a time).
            if kind == "edit_plan_review":
                return {"action": "approve"}, questions
            if kind == "mode_switch":
                return {"action": "switch_mode", "selectedMode": "auto"}, questions
            if kind == "ask_user":
                questions.append({"type": "ask_user", "prompt": value.get("prompt") or value.get("question") or ""})
                return {"action": "respond", "userResponse": _env("CASSETTE_API_DEFAULT_ASK_USER_REPLY") or "Please proceed using your best judgment."}, questions
            if kind == "init_questions":
                return {}, questions
        if keyed:
            return keyed, questions
        # Unknown interrupt shape — resume empty rather than hang.
        return {}, questions

    # ── export ────────────────────────────────────────────────────────────────
    def _export_project(self, session_id: str, job_id: str, deadline: float | None = None) -> list[dict]:
        if deadline is None:
            deadline = time.monotonic() + 600.0
        _, created = self._request("POST", f"/api/export/projects/{session_id}/jobs", json_body={}, expect=202)
        export_job_id = isinstance(created, dict) and created.get("jobId")
        if not export_job_id:
            raise ApiTransportError("export_create_failed", "Export create returned no jobId")
        export_job_id = str(export_job_id)

        file_url: str | None = None
        while time.monotonic() < deadline:
            _, body = self._request("GET", f"/api/export/jobs/{export_job_id}", expect=200)
            status = str((body or {}).get("status") or "") if isinstance(body, dict) else ""
            if status == "done":
                file_url = (body or {}).get("fileUrl") or f"/api/export/jobs/{export_job_id}/file"
                break
            if status == "error":
                raise ApiTransportError("export_failed", str((body or {}).get("error") or "Export job failed"))
            time.sleep(self._poll_interval())
        if not file_url:
            raise ApiTransportError("export_timeout", "Export job did not complete in time")

        target = _exports_dir(job_id) / f"{job_id}.mp4"
        self._download(f"/api/export/jobs/{export_job_id}/file", target)
        return [{
            "text": target.name,
            "href": file_url,
            "download": target.name,
            "local_path": str(target),
            "kind": "video",
        }]

    def _download(self, path: str, target: Path) -> None:
        request = Request(_api_base() + path, method="GET", headers=self._auth_headers({}))
        try:
            with urlopen(request, timeout=max(120.0, self._http_timeout_for_upload(0))) as response, target.open("wb") as fh:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
        except HTTPError as exc:
            raise ApiTransportError("export_download_failed", f"Export download failed (HTTP {exc.code})") from exc
        except URLError as exc:
            raise ApiTransportError("export_download_failed", f"Export download failed: {exc.reason}") from exc

    # ── http + result helpers ──────────────────────────────────────────────────
    def _auth_headers(self, headers: dict[str, str]) -> dict[str, str]:
        merged = dict(headers)
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
        _retried: bool = False,
    ) -> tuple[int, Any]:
        url = _api_base() + path
        data = None
        req_headers = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        if authed:
            req_headers = self._auth_headers(req_headers)
        request = Request(url, data=data, method=method, headers=req_headers)
        try:
            with urlopen(request, timeout=_http_timeout()) as response:
                status = response.status
                raw = response.read()
        except HTTPError as exc:
            status = exc.code
            raw = exc.read()
            # Re-verify once on auth expiry, then retry.
            if status == 401 and authed and not _retried:
                self._token = None
                self._authenticate()
                return self._request(method, path, json_body=json_body, headers=headers,
                                     authed=authed, expect=expect, _retried=True)
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
                f"{method} {path} -> 403{f': {detail}' if detail else ''}. The Cassette account likely needs "
                f"full API access (agent_allowed_emails.access_level='full') for /api/projects and /api/export.",
                details={"status": 403, "path": path},
            )
        if expect is not None and status != expect:
            detail = body.get("error") if isinstance(body, dict) else None
            raise ApiTransportError("http_error", f"{method} {path} -> HTTP {status}{f': {detail}' if detail else ''}",
                                    details={"status": status, "path": path})
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
        risk: str = "medium",
    ) -> dict:
        outputs = outputs or []
        return {
            "status": status,
            "outputs": outputs,
            "questions": questions or [],
            "errors": errors or [],
            "quality": {
                "transport": "api",
                "completion_observed": completion_observed,
                "export_completed": export_completed,
                "export_pending": False,
                "output_link_count": len(outputs),
                "local_output_count": sum(1 for o in outputs if isinstance(o, dict) and o.get("local_path")),
                "risk": risk,
            },
            "final_screenshot": None,
        }

    @staticmethod
    def _error(exc: Exception) -> dict:
        if isinstance(exc, ApiTransportError):
            return {"code": exc.code, "message": exc.message, "details": exc.details}
        return {"code": "internal_error", "message": str(exc), "details": {"type": type(exc).__name__}}

    @staticmethod
    def _job_timeout(job: dict) -> float:
        try:
            return max(60.0, float(job.get("timeout_sec") or 1800))
        except (TypeError, ValueError):
            return 1800.0

    @staticmethod
    def _poll_interval() -> float:
        try:
            return max(1.0, float(_env("CASSETTE_API_POLL_INTERVAL_SEC") or "3"))
        except ValueError:
            return 3.0

    @staticmethod
    def _recursion_limit() -> int:
        try:
            return max(25, int(_env("CASSETTE_API_RECURSION_LIMIT") or "344"))
        except ValueError:
            return 344

    @staticmethod
    def _http_timeout_for_upload(num_bytes: int) -> float:
        # Allow large uploads/downloads more time than ordinary JSON calls.
        return max(60.0, _http_timeout(), num_bytes / (256 * 1024))
