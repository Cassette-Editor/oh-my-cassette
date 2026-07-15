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

Wire format is coded against the Cassette server source (remotion-canvas-hotfix): the run's
config.configurable carries the full sessionContext + projectContext + runContext.connectionState
the editor sends; uploaded media is linked to the run by session id (sessionContext.mediaSessionId
== the upload x-session-id), NOT by ids in the run input; tool interrupts (editor_navigate) resume
KEYED by toolCall.id while typed interrupts resume bare; export renders the stored project by that
same session id. A run is executed by the upstream LangGraph queue worker: if it never leaves
'pending' (queue not draining) the transport fails fast with 'agent_run_not_started' rather than
hanging until the job timeout.
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

# Quality subkeys that _result computes from the current outcome — never carry these forward from a
# prior job's quality (they would clobber the fresh values with stale ones).
_RESULT_COMPUTED_QUALITY_KEYS = frozenset({
    "transport", "completion_observed", "export_completed", "export_pending",
    "output_link_count", "local_output_count", "risk",
})


class ApiTransportError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class _JobCancelled(Exception):
    """Raised inside a poll loop when jobs.is_cancel_requested(job_id) flips to True.

    Cancellation is cooperative in this plugin (jobs.request_cancel just sets status=cancel_requested);
    the runner must notice and stop. run_job catches this and returns a terminal 'cancelled' result so
    the downstream terminal save does not overwrite the cancel with the run's own status.
    """


def _env(name: str) -> str:
    # Match the rest of the plugin's config resolution: prefer the process env, then fall back to
    # ~/.hermes/.env (notifier._runtime_env), so settings placed in the Hermes env file are honored
    # even when Hermes does not export them into os.environ.
    try:
        from . import notifier
        getter = getattr(notifier, "_runtime_env", None)
        if callable(getter):
            return str(getter(name) or "").strip()
    except Exception:  # noqa: BLE001 — fall back to the process env
        pass
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
        # Re-drive/collect the export for a Hermes-reviewed completion. Seed from the job so the
        # accumulated questions/errors and prior quality survive (mirrors browser.export_reviewed_
        # completion_job); the review decision is recorded in quality.completion_review.
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        decision = decision or {}
        outputs: list[dict] = []
        questions = list(job.get("questions") or [])
        errors = list(job.get("errors") or [])
        prior_quality = dict(job.get("quality") or {})
        export_deadline = time.monotonic() + self._export_timeout(job)
        try:
            self._authenticate()
            outputs = self._export_project(session_id, job_id, deadline=export_deadline)
        except _JobCancelled:
            return self._result(
                _CANCELLED, questions=questions, errors=errors,
                completion_observed=bool(prior_quality.get("completion_observed")),
                export_completed=False, risk="medium",
                extra_quality={k: v for k, v in prior_quality.items() if k not in _RESULT_COMPUTED_QUALITY_KEYS},
                final_screenshot=job.get("final_screenshot"),
            )
        except ApiTransportError as exc:
            errors.append(self._error(exc))
        except Exception as exc:  # noqa: BLE001 — never let export crash the job record
            errors.append(self._error(exc))
        # Carry forward only the DESCRIPTIVE prior-quality keys; the outcome keys _result computes
        # (export_pending/completion_observed/risk/…) must reflect THIS export, not the stale run.
        carried = {k: v for k, v in prior_quality.items() if k not in _RESULT_COMPUTED_QUALITY_KEYS}
        review_quality = {
            "completion_source": "hermes_completion_review",
            "completion_review": {
                "decision": str(decision.get("decision") or "export"),
                "reason": str(decision.get("reason") or "")[:500],
            },
            "progress_summary": str(decision.get("summary") or prior_quality.get("progress_summary") or "")[:700] or None,
            "current_stage": prior_quality.get("current_stage") or None,
        }
        return self._result(
            _SUCCEEDED if outputs else _FAILED,
            outputs=outputs,
            questions=questions,
            errors=errors,
            completion_observed=bool(outputs) or bool(prior_quality.get("completion_observed")),
            export_completed=bool(outputs),
            export_pending=not outputs,
            risk="low" if outputs else "high",
            extra_quality={**carried, **review_quality},
            final_screenshot=job.get("final_screenshot"),
        )

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
            self._raise_if_cancelled(job_id)
            self._authenticate()

            media_file_ids: list[str] = []
            for path in asset_paths:
                media_file_ids.append(self._upload_asset(path, session_id, deadline, job_id))

            # Media derivatives (analysis evidence/embeddings for the agent, render-source for the
            # export) are generated asynchronously after upload. Starting the run early makes the
            # agent commit an empty edit ("succeeds" but exports a blank 1-frame video); exporting
            # early fails with "render-source is missing". Wait for full readiness first.
            self._await_media_ready(session_id, media_file_ids, deadline, job_id)

            thread_id = self._create_thread(session_id, job)
            run_status, run_questions = self._run_agent(thread_id, session_id, prompt, job, deadline, media_file_ids)
            questions.extend(run_questions)

            if run_status == _NEEDS_USER:
                return self._result(_NEEDS_USER, questions=questions, errors=errors,
                                    completion_observed=True, export_completed=False, risk="medium",
                                    extra_quality={"progress_summary": self._questions_summary(questions)})
            if run_status == _TIMED_OUT:
                errors.append({"code": "agent_run_timeout", "message": "Agent run did not finish before the job timeout", "details": {}})
                return self._result(_TIMED_OUT, questions=questions, errors=errors,
                                    completion_observed=False, export_completed=False, risk="medium")
            if run_status != _SUCCEEDED:
                errors.append({"code": "agent_run_incomplete", "message": f"Agent run ended with status '{run_status}'", "details": {}})
                return self._result(_FAILED, questions=questions, errors=errors,
                                    completion_observed=True, export_completed=False, risk="high")

            # Agent edit committed server-side. Export is a separate step: if it fails, the edit still
            # happened, so report 'succeeded' with export_pending rather than masking it as a failure
            # (mirrors browser.py's "succeeded but export pending" story consumed by _job_report).
            try:
                outputs = self._export_project(session_id, job_id, deadline=deadline)
            except _JobCancelled:
                raise
            except ApiTransportError as exc:
                errors.append(self._error(exc))
                return self._result(_SUCCEEDED, questions=questions, errors=errors,
                                    completion_observed=True, export_completed=False, export_pending=True, risk="medium",
                                    extra_quality={"progress_summary": "Cassette edit committed; the export did not complete in time."})
            has_local = any(o.get("local_path") for o in outputs)
            return self._result(
                _SUCCEEDED,
                outputs=outputs,
                questions=questions,
                errors=errors,
                completion_observed=True,
                export_completed=bool(outputs),
                export_pending=not outputs,
                risk="low" if has_local else "medium",
            )
        except _JobCancelled:
            return self._result(_CANCELLED, questions=questions, errors=errors,
                                completion_observed=False, export_completed=False, risk="medium",
                                extra_quality={"progress_summary": "Cassette job was cancelled before it finished."})
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
    def _upload_asset(self, path: str, session_id: str, deadline: float, job_id: str = "") -> str:
        self._raise_if_cancelled(job_id)
        file_path = Path(path)
        if not file_path.exists():
            raise ApiTransportError("asset_missing", f"Asset not found on disk: {path}")
        file_name = file_path.name
        mime, _ = mimetypes.guess_type(file_name)
        mime = mime or "application/octet-stream"
        # The editor scopes uploads by BOTH x-session-id (media catalog the agent reads) and
        # x-project-id (project<->asset binding used by export). Send the same id for both so the
        # uploaded media is visible to the agent run AND bound to the project that gets exported.
        headers = {"x-session-id": session_id, "x-project-id": session_id}

        _, init = self._request("POST", "/api/media/upload/init",
                                json_body={"fileName": file_name, "mimeType": mime}, headers=headers, expect=200)
        if not isinstance(init, dict) or not init.get("uploadUrl") or not init.get("key"):
            raise ApiTransportError("upload_init_failed", f"upload/init returned an unexpected body for {file_name}")
        key = str(init["key"])
        upload_content_type = str(init.get("uploadContentType") or mime)
        storage_backend = str(init.get("storageBackend") or "r2")
        upload_attempt_id = init.get("uploadAttemptId")

        self._put_bytes(str(init["uploadUrl"]), file_path.read_bytes(), upload_content_type)

        complete_body = {"key": key, "fileName": file_name, "mimeType": mime,
                         "storageBackend": storage_backend, "metadata": {}}
        if upload_attempt_id:
            complete_body["uploadAttemptId"] = upload_attempt_id
        _, complete = self._request("POST", "/api/media/upload/complete",
                                    json_body=complete_body, headers=headers, expect=200)
        if not isinstance(complete, dict) or not complete.get("mediaFileId"):
            raise ApiTransportError("upload_complete_failed", f"upload/complete returned no mediaFileId for {file_name}")

        media_file_id = str(complete["mediaFileId"])
        if str(mime).startswith("video/") and complete.get("uploadStatus") != "completed":
            self._poll_upload_completed(key, session_id, deadline, job_id)
        return media_file_id

    def _poll_upload_completed(self, key: str, session_id: str, deadline: float, job_id: str = "") -> None:
        # The status endpoint returns 200 + uploadStatus 'completed' when finalized, 202 +
        # 'processing' while in flight, and 409 + 'failed' on error — so do not force expect=200.
        headers = {"x-session-id": session_id, "x-project-id": session_id}
        query = "?" + urlencode({"key": key})
        while time.monotonic() < deadline:
            self._raise_if_cancelled(job_id)
            status_code, body = self._request("GET", "/api/media/upload/status" + query, headers=headers)
            upload_status = str((body or {}).get("uploadStatus") or "") if isinstance(body, dict) else ""
            if upload_status == "completed":
                return
            if upload_status == "failed" or status_code == 409:
                raise ApiTransportError("upload_processing_failed",
                                        str((body or {}).get("error") or f"Media processing failed for key {key}"))
            time.sleep(self._poll_interval())
        raise ApiTransportError("upload_processing_timeout", f"Media processing did not complete for key {key}")

    def _await_media_ready(self, session_id: str, media_file_ids: list[str], deadline: float,
                           job_id: str = "") -> None:
        """Wait until every uploaded media file is fully processed before starting the agent run.

        GET /api/media/operations/status?ids= reports per-file readiness: aiReady/analysisReady (the
        agent's session catalog is filtered to analysis-ready media) and exportReady/renderStatus (the
        render-source derivative the export needs). Both derivatives are async; running before they
        finish yields an empty edit or an "render-source is missing" export failure. Bounded by a
        media-ready timeout and the job deadline; a hard processing failure or timeout is surfaced
        (a clear error beats a blank video)."""
        wanted = {str(m) for m in media_file_ids if m}
        if not wanted:
            return
        ready_deadline = min(deadline, time.monotonic() + self._media_ready_timeout())
        query = "?" + urlencode({"ids": ",".join(sorted(wanted))})
        headers = {"x-session-id": session_id}
        last_phase = ""
        while time.monotonic() < ready_deadline:
            self._raise_if_cancelled(job_id)
            _, body = self._request("GET", "/api/media/operations/status" + query, headers=headers, expect=200)
            statuses = (body or {}).get("statuses") or [] if isinstance(body, dict) else []
            by_id = {str(s.get("mediaFileId")): s for s in statuses if isinstance(s, dict)}
            ready: set[str] = set()
            for mid in wanted:
                s = by_id.get(mid) or {}
                # A failed analysis or render derivative won't self-heal (same bytes re-fail), and the
                # server leaves terminalState 'active' on a failed analyze chunk — so surface the real
                # error fast instead of spinning until the media-ready timeout.
                if (s.get("terminalState") == "failed" or s.get("renderStatus") == "failed"
                        or s.get("analyzeStatus") == "analyze_failed"):
                    raise ApiTransportError(
                        "media_processing_failed",
                        str(s.get("errorMessage") or f"Media {mid} failed processing"),
                        details={"media_file_id": mid, "analyze_status": s.get("analyzeStatus"),
                                 "render_status": s.get("renderStatus")},
                    )
                if s.get("fullyReady") or (self._ai_ready(s) and self._export_ready(s)):
                    ready.add(mid)
                else:
                    last_phase = str(s.get("readinessPhase") or last_phase)
            if wanted <= ready:
                return
            time.sleep(self._poll_interval())
        raise ApiTransportError(
            "media_analysis_timeout",
            f"Uploaded media did not finish processing in time (last phase: {last_phase or 'unknown'}); "
            "the agent cannot edit and the render cannot export media that is not ready.",
            details={"session_id": session_id, "readiness_phase": last_phase},
        )

    @staticmethod
    def _ai_ready(status: dict) -> bool:
        return bool(status.get("aiReady") or status.get("analysisReady"))

    @staticmethod
    def _export_ready(status: dict) -> bool:
        return bool(status.get("exportReady") or status.get("renderStatus") == "completed")

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
        thread_id = (body.get("thread_id") or body.get("threadId")) if isinstance(body, dict) else None
        if not thread_id:
            raise ApiTransportError("thread_create_failed", "LangGraph thread create returned no thread_id")
        return str(thread_id)

    def _session_context(self, session_id: str, job: dict, prompt: str) -> dict:
        # Mirrors CassetteAgentSessionContext (buildCurrentSessionContext in the editor). All of
        # projectId/mediaSessionId/chatSessionId/threadId collapse onto one id in the real editor
        # (BROWSER_SESSION_ID == getCurrentProjectId()), so the plugin uses the single session id.
        return {
            "chatSessionId": session_id,
            "threadId": session_id,
            "mediaSessionId": session_id,
            "projectId": session_id,
            "mode": "auto",
            "turnStrategy": "default",
            "turnKind": "conversation",
            "reinitMode": None,
            "editorSnapshot": None,
            "mentionedTimelineEntities": None,
            "queryImageIds": None,
            "modelId": self._resolve_model_id(job),
            "thinkingConfig": self._resolve_thinking_config(job),
            "locale": job.get("cassette_language") or None,
            "currentUserRequest": prompt,
            "stoppedTurn": None,
        }

    @staticmethod
    def _project_context() -> dict:
        # A headless run starts from an empty project (no prior editor state); the graph builds it.
        return {
            "cassetteContext": "",
            "revision": 0,
            "sourceKind": None,
            "updatedAt": None,
            "status": "missing",
        }

    def _run_context(self, session_context: dict, turn_id: str) -> dict:
        # Mirrors buildConfigurable().runContext — the connectionState the graph uses to load the
        # session media catalog and commit edits to the project keyed by projectId.
        return {
            "connectionState": {
                "threadId": session_context["threadId"],
                "sessionId": session_context["mediaSessionId"],
                "chatSessionId": session_context["chatSessionId"],
                "mediaSessionId": session_context["mediaSessionId"],
                "projectId": session_context["projectId"],
                "activeMode": session_context["mode"],
                "queryImageIds": None,
                "locale": session_context["locale"],
                "modelId": session_context["modelId"],
                "thinkingConfig": session_context["thinkingConfig"],
                "currentTurnId": turn_id,
            },
            "executionBudget": None,
            "contextCompactionPolicy": None,
            "agentGateRequirements": None,
        }

    @staticmethod
    def _resolve_model_id(job: dict) -> str:
        # model_selection carries a UI *label* under 'model' (not an id), so only forward it when it
        # already names a product model id (or an explicit 'model_id'/'modelId'); otherwise use the
        # env override or the editor's default. The default IS the UI default, so an unmapped label
        # runs the same model the browser/UI would default to rather than an arbitrary one.
        ms = job.get("model_selection") or {}
        candidate = str(ms.get("model_id") or ms.get("modelId") or ms.get("model") or "").strip()
        if candidate in _SUPPORTED_AGENT_MODEL_IDS:
            return candidate
        env_model = _env("CASSETTE_API_MODEL_ID")
        return env_model if env_model in _SUPPORTED_AGENT_MODEL_IDS else DEFAULT_AGENT_MODEL_ID

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

    def _run_agent(self, thread_id: str, session_id: str, prompt: str, job: dict, deadline: float,
                   media_file_ids: list[str] | None = None) -> tuple[str, list[dict]]:
        """Start the run, satisfy interrupts headlessly, return (terminal_status, questions).

        Uploaded media is NOT passed as ids — the cassette-chat graph reads the session-scoped media
        catalog keyed by sessionContext.mediaSessionId (== the upload x-session-id). media_file_ids is
        accepted only so callers can log/verify what was uploaded."""
        job_id = str(job.get("job_id") or "")
        turn_id = f"{job_id or session_id}-turn"
        session_context = self._session_context(session_id, job, prompt)
        config = {
            # recursion_limit is what the upstream LangGraph server reads; the editor also sends the
            # camelCase duplicate, harmless to include for parity.
            "recursion_limit": self._recursion_limit(),
            "recursionLimit": self._recursion_limit(),
            "configurable": {
                "sessionContext": session_context,
                "projectContext": self._project_context(),
                "runContext": self._run_context(session_context, turn_id),
            },
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
            status = self._await_run(thread_id, run_id, deadline, job_id)
            if status == "interrupted":
                interrupts = self._pending_interrupts(thread_id)
                if not interrupts:
                    # Interrupted with nothing pending == treat as needing user.
                    return _NEEDS_USER, questions
                resume_value, new_questions, needs_user = self._resume_value(interrupts)
                questions.extend(new_questions)
                if needs_user:
                    # A genuine user question was raised (e.g. ask_user) with no auto-reply configured:
                    # leave the thread interrupted and hand back to the Hermes/user review loop.
                    return _NEEDS_USER, questions
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
        run_id = (resp.get("run_id") or resp.get("runId")) if isinstance(resp, dict) else None
        if not run_id:
            raise ApiTransportError("run_create_failed", "LangGraph run create returned no run_id")
        return str(run_id)

    def _await_run(self, thread_id: str, run_id: str, deadline: float, job_id: str = "") -> str:
        # Fail fast if the run never leaves 'pending' — a healthy LangGraph worker moves a run to
        # 'running' within seconds, so a run stuck 'pending' means the run queue is not being drained
        # (worker down/misconfigured). Without this the job would hang until the full job timeout.
        start = time.monotonic()
        start_timeout = self._run_start_timeout()
        ever_started = False
        while time.monotonic() < deadline:
            if self._cancelled(job_id):
                self._cancel_run(thread_id, run_id)
                raise _JobCancelled()
            _, body = self._request("GET", f"/api/langgraph/threads/{thread_id}/runs/{run_id}", expect=200)
            status = str((body or {}).get("status") or "") if isinstance(body, dict) else ""
            if status in _LG_TERMINAL:
                return status
            if status and status != "pending":
                ever_started = True
            if not ever_started and (time.monotonic() - start) > start_timeout:
                raise ApiTransportError(
                    "agent_run_not_started",
                    f"Agent run stayed '{status or 'pending'}' for {int(start_timeout)}s without starting — "
                    "the Cassette agent run queue is not draining (backend worker unavailable).",
                    details={"run_id": run_id, "status": status or "pending"},
                )
            time.sleep(self._poll_interval())
        return "timeout"

    def _cancel_run(self, thread_id: str, run_id: str) -> None:
        """Best-effort server-side cancel of an in-flight LangGraph run (so it actually stops,
        not just locally abandoned). Failures are swallowed — the job is already terminating."""
        try:
            self._request("POST", f"/api/langgraph/threads/{thread_id}/runs/{run_id}/cancel?action=interrupt",
                          json_body={})
        except Exception:  # noqa: BLE001
            pass

    def _pending_interrupts(self, thread_id: str) -> list[dict]:
        _, state = self._request("GET", f"/api/langgraph/threads/{thread_id}/state", expect=200)
        out: list[dict] = []
        if not isinstance(state, dict):
            return out
        for task in state.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for interrupt in (task.get("interrupts") or []):
                value = interrupt.get("value") if isinstance(interrupt, dict) else None
                if isinstance(value, dict):
                    out.append({"id": interrupt.get("id"), "value": value})
        # Some LangGraph versions surface interrupts on the top-level __interrupt__ channel. Guard
        # against an explicit null (values["__interrupt__"] == None) which .get(..., []) would return.
        for interrupt in ((state.get("values") or {}).get("__interrupt__") or []):
            if isinstance(interrupt, dict) and isinstance(interrupt.get("value"), dict):
                out.append({"id": interrupt.get("id"), "value": interrupt["value"]})
        return out

    def _resume_value(self, interrupts: list[dict]) -> tuple[Any, list[dict], bool]:
        """Build the resume payload. Returns (resume_value, questions, needs_user).

        Headless tool interrupts (editor_navigate) resume KEYED by toolCall.id; typed interrupts
        (edit_plan_review/mode_switch/init_questions) resume with a BARE object. A genuine ``ask_user``
        question hands control back to the user (needs_user=True) unless CASSETTE_API_DEFAULT_ASK_USER_REPLY
        is set, matching the browser path which only auto-handles *routine* interactions and surfaces real
        questions as needs_user. A typed interrupt is resolved before any batched tool acks so its bare
        payload is never shadowed by the keyed map (LangGraph resumes one interrupt at a time)."""
        questions: list[dict] = []
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
            # Typed interrupt — resume bare. Resolve it first so its payload wins over any keyed acks.
            if kind == "edit_plan_review":
                return {"action": "approve"}, questions, False
            if kind == "mode_switch":
                return {"action": "switch_mode", "selectedMode": "auto"}, questions, False
            if kind == "init_questions":
                return {}, questions, False
            if kind == "ask_user":
                text = str(value.get("prompt") or value.get("question") or "")[:500]
                auto_reply = _env("CASSETTE_API_DEFAULT_ASK_USER_REPLY")
                if auto_reply:
                    questions.append({"question": text, "requires_user": False,
                                      "reason": "cassette_agent_question", "answer": auto_reply})
                    return {"action": "respond", "userResponse": auto_reply}, questions, False
                questions.append({"question": text, "requires_user": True,
                                  "reason": "cassette_agent_question", "answer": ""})
                return None, questions, True
        if keyed:
            return keyed, questions, False
        # Unknown interrupt shape — resume empty rather than hang.
        return {}, questions, False

    # ── export ────────────────────────────────────────────────────────────────
    def _export_project(self, session_id: str, job_id: str, deadline: float | None = None) -> list[dict]:
        if deadline is None:
            deadline = time.monotonic() + 600.0
        from urllib.parse import quote
        _, created = self._request("POST", f"/api/export/projects/{quote(str(session_id), safe='')}/jobs",
                                   json_body={}, expect=202)
        export_job_id = created.get("jobId") if isinstance(created, dict) else None
        if not export_job_id:
            raise ApiTransportError("export_create_failed", "Export create returned no jobId")
        export_job_id = str(export_job_id)

        file_url: str | None = None
        while time.monotonic() < deadline:
            self._raise_if_cancelled(job_id)
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
        timeout: float | None = None,
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
            with urlopen(request, timeout=timeout or _http_timeout()) as response:
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
                                     authed=authed, expect=expect, timeout=timeout, _retried=True)
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
        export_pending: bool = False,
        risk: str = "medium",
        extra_quality: dict[str, Any] | None = None,
        final_screenshot: Any | None = None,
    ) -> dict:
        outputs = outputs or []
        quality = {
            "transport": "api",
            "completion_observed": completion_observed,
            "export_completed": export_completed,
            "export_pending": export_pending,
            "output_link_count": len(outputs),
            "local_output_count": sum(1 for o in outputs if isinstance(o, dict) and o.get("local_path")),
            "risk": risk,
        }
        if extra_quality:
            quality.update({k: v for k, v in extra_quality.items() if v is not None})
        return {
            "status": status,
            "outputs": outputs,
            "questions": questions or [],
            "errors": errors or [],
            "quality": quality,
            "final_screenshot": final_screenshot,
        }

    @staticmethod
    def _error(exc: Exception) -> dict:
        if isinstance(exc, ApiTransportError):
            return {"code": exc.code, "message": exc.message, "details": exc.details}
        return {"code": "internal_error", "message": str(exc), "details": {"type": type(exc).__name__}}

    @staticmethod
    def _cancelled(job_id: str) -> bool:
        # Cooperative cancellation: the browser path polls jobs.is_cancel_requested during its waits
        # (browser.py); the API path must do the same so /cut and the web cancel actually stop the run.
        if not job_id:
            return False
        try:
            from . import jobs
            return bool(jobs.is_cancel_requested(job_id))
        except Exception:  # noqa: BLE001 — never let a cancel probe crash the run
            return False

    def _raise_if_cancelled(self, job_id: str) -> None:
        if self._cancelled(job_id):
            raise _JobCancelled()

    @staticmethod
    def _questions_summary(questions: list[dict]) -> str | None:
        for q in questions:
            if isinstance(q, dict) and q.get("question"):
                return str(q["question"])[:700]
        return None

    @staticmethod
    def _job_timeout(job: dict) -> float:
        try:
            return max(60.0, float(job.get("timeout_sec") or 1800))
        except (TypeError, ValueError):
            return 1800.0

    @staticmethod
    def _export_timeout(job: dict) -> float:
        # A reviewed-completion export gets its own budget (env override, else the job timeout),
        # so it never inherits an already-exhausted run deadline.
        raw = _env("CASSETTE_EXPORT_TIMEOUT_SEC")
        if raw:
            try:
                return max(60.0, float(raw))
            except ValueError:
                pass
        return ApiTransport._job_timeout(job)

    @staticmethod
    def _media_ready_timeout() -> float:
        # How long to wait for uploaded media to become analysis-ready. Generous — scene analysis +
        # embeddings take real time for longer clips. Override with CASSETTE_API_MEDIA_READY_TIMEOUT_SEC.
        try:
            return max(30.0, float(_env("CASSETTE_API_MEDIA_READY_TIMEOUT_SEC") or "300"))
        except ValueError:
            return 300.0

    @staticmethod
    def _run_start_timeout() -> float:
        # How long a run may stay 'pending' before we declare the queue stalled. Generous by default
        # to tolerate cold starts; override with CASSETTE_API_RUN_START_TIMEOUT_SEC.
        try:
            return max(30.0, float(_env("CASSETTE_API_RUN_START_TIMEOUT_SEC") or "120"))
        except ValueError:
            return 120.0

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
