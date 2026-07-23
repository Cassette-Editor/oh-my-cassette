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
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
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
_SUPPORTED_AGENT_MODEL_IDS = frozenset(
    {
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "openai/gpt-5.4-mini",
    }
)
# The plugin's model_selection stores a UI *label* (browser.py scrapes only the label, not the id),
# so map the label -> agent model id to honor the user's model choice on the api path. Labels are
# locale-independent brand names (cassette-config MODEL_OPTIONS i18n; identical in zh and en).
_MODEL_LABEL_TO_ID = {
    "deepseekv4flash": "deepseek/deepseek-v4-flash",
    "deepseekv4pro": "deepseek/deepseek-v4-pro",
    "gpt54mini": "openai/gpt-5.4-mini",
}
_DEFAULT_THINKING = "low"  # matches cassette-config DEFAULT_THINKING / per-model defaultThinking


def _require_model_selection() -> bool:
    # Default true (matches browser.py): a chosen-but-unresolvable model fails the job rather than
    # silently running the default.
    return _env("CASSETTE_REQUIRE_MODEL_SELECTION").lower() not in {"0", "false", "no", "off"}


def _export_on_complete(job: dict) -> bool:
    # Whether a finished turn should route to the export/review ceremony. API default is FALSE
    # (a conversational turn ends committed-but-unrendered; run_job export=true opts in per turn).
    # browser._export_on_complete keeps its own default-true — single-shot browser parity.
    raw = _env("CASSETTE_EXPORT_ON_COMPLETE") or str(job.get("export_on_complete", "false"))
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _auto_export() -> bool:
    # Opt-in: export directly on api-success instead of routing through the Hermes completion review.
    # This is NOT browser-parity (the browser always routes completion through the supervisor).
    return _env("CASSETTE_API_AUTO_EXPORT").lower() in {"1", "true", "yes", "on"}


def _model_id_from_label(label: str) -> str | None:
    """Map a scraped model display label (e.g. 'DeepSeek V4 Pro') to its agent model id."""
    norm = "".join(ch for ch in str(label).lower() if ch.isalnum())
    if not norm:
        return None
    if norm in _MODEL_LABEL_TO_ID:
        return _MODEL_LABEL_TO_ID[norm]
    # Token fallback, robust to label drift / localization.
    if "flash" in norm:
        return "deepseek/deepseek-v4-flash"
    if "deepseek" in norm and "pro" in norm:
        return "deepseek/deepseek-v4-pro"
    if "gpt" in norm or "mini" in norm:
        return "openai/gpt-5.4-mini"
    return None


# Quality subkeys that _result computes from the current outcome — never carry these forward from a
# prior job's quality (they would clobber the fresh values with stale ones).
_RESULT_COMPUTED_QUALITY_KEYS = frozenset(
    {
        "transport",
        "completion_observed",
        "export_completed",
        "export_pending",
        "output_link_count",
        "local_output_count",
        "risk",
    }
)


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
    # MCP reads only the host-neutral protected config (after process env); the web demo reads only
    # process env. Hermes retains its historical ~/.hermes/.env fallback.
    try:
        import runtime_config

        adapter = runtime_config.runtime_adapter()
        if adapter == runtime_config.MCP_ADAPTER:
            return runtime_config.mcp_env_value(name)
        if adapter == runtime_config.WEB_ADAPTER:
            return str(os.getenv(name, "") or "").strip()
    except Exception:  # noqa: BLE001 — preserve the legacy adapter below
        pass
    try:
        from . import notifier

        getter = getattr(notifier, "_runtime_env", None)
        if callable(getter):
            return str(getter(name) or "").strip()
    except Exception:  # noqa: BLE001 — fall back to the process env
        pass
    return str(os.getenv(name, "") or "").strip()


def _env_num(name: str, default, floor, *, cast=float, getter=None):
    # Shared env-number parse: read name (via _env by default, or os.getenv), coerce with cast,
    # clamp to floor, and fall back to default on missing/garbage input.
    getter = getter or _env
    try:
        return max(floor, cast(getter(name) or default))
    except (TypeError, ValueError):
        return default


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


_TRY_SESSION_PREFIX = "try-session-"


def _editor_url(session_id: str, thread_id: str, job: dict) -> str | None:
    """Deep link into the live editor for this session's project and chat thread.

    Only try-session-* projects are reachable token-free (publicTry tier); older un-prefixed
    session ids have no browsable link, so this returns None for them."""
    if not session_id.startswith(_TRY_SESSION_PREFIX):
        return None
    base = _env("CASSETTE_WEB_URL")
    if not base:
        raw = str(job.get("url") or os.getenv("CASSETTE_URL", "https://sg.trycassette.online/agent"))
        parts = urlparse(raw)
        if not parts.scheme or not parts.netloc:
            return None
        base = f"{parts.scheme}://{parts.netloc}"
    session_hash = session_id[len(_TRY_SESSION_PREFIX) :]
    query = urlencode({"projectSessionId": session_hash, "chatSessionId": thread_id})
    return f"{base.rstrip('/')}/try?{query}"


def _http_timeout() -> float:
    return _env_num("CASSETTE_API_HTTP_TIMEOUT_SEC", 60.0, 5.0)


def _unattended() -> bool:
    # One switch restoring today's fully headless semantics (auto-approve plans, classifier-answered
    # questions) for CI-style runs.
    return _env("CASSETTE_UNATTENDED").lower() in {"1", "true", "yes", "on"}


def _plan_review_mode() -> str:
    """'user' surfaces edit_plan_review as a real question; 'auto' keeps silent approval.

    Default: 'user' on MCP hosts (the person is present in the chat), 'auto' for gateway/Hermes
    background jobs where a blocking review would stall unattended pipelines."""
    value = _env("CASSETTE_PLAN_REVIEW").lower()
    if value in {"user", "auto"}:
        return value
    try:
        import runtime_config

        if runtime_config.runtime_adapter() == runtime_config.MCP_ADAPTER:
            return "user"
    except Exception:  # noqa: BLE001
        pass
    return "auto"


def _plan_review_resume(answer: str) -> dict:
    """Map a free-text reply onto the bare PlanReviewDecision the graph expects."""
    text = str(answer or "").strip()
    lowered = text.lower()
    if lowered in {"approve", "approved", "yes", "ok", "okay", "lgtm", "同意", "批准", "可以"} or lowered.startswith(
        "approve"
    ):
        return {"action": "approve"}
    if lowered in {"reject", "rejected", "no", "cancel", "拒绝", "取消"}:
        return {"action": "reject"}
    if lowered.startswith("revise"):
        feedback = text[len("revise") :].strip(" :,-") or "Please revise the plan."
        return {"action": "revise", "feedback": feedback}
    # Any other free text is revision feedback — the safest reading of "change it to…".
    return {"action": "revise", "feedback": text}


def _stream_enabled() -> bool:
    # SSE event listener (timeline deltas + plan progress). Default on; CASSETTE_API_STREAM=0
    # restores a pure-poll transport. Status polling stays the run driver either way — the
    # stream is the event channel, never the completion signal.
    return _env("CASSETTE_API_STREAM").lower() not in {"0", "false", "no", "off"}


def _stream_read_timeout() -> float:
    return _env_num("CASSETTE_API_STREAM_TIMEOUT_SEC", 900.0, 30.0)


# Stream modes requested for every run so a later join (plugin listener OR the user's /try tab)
# replays commit/plan events; 'custom' carries ProjectCommitEvents, 'updates' the node progress.
_RUN_STREAM_MODES = ["updates", "custom"]


class ApiTransport:
    def __init__(self) -> None:
        self._token: str | None = None
        self._active_stream_stop: threading.Event | None = None
        # Progress state (reset per run in _init_progress; defaults keep helpers safe on the export path).
        self._job: dict | None = None
        self._stage_timings: dict[str, dict] = {}
        self._current_stage: str = ""
        self._last_event: float = 0.0
        self._last_heartbeat: float = 0.0
        self._run_started: float = 0.0

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
        self._init_progress(job)
        self._enter_stage(job_id, "export", "Rendering the reviewed export")
        try:
            self._authenticate()
            outputs = self._export_project(session_id, job_id, deadline=export_deadline)
        except _JobCancelled:
            return self._result(
                _CANCELLED,
                questions=questions,
                errors=errors,
                completion_observed=bool(prior_quality.get("completion_observed")),
                export_completed=False,
                risk="medium",
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
            "progress_summary": str(decision.get("summary") or prior_quality.get("progress_summary") or "")[:700]
            or None,
            "current_stage": "export",
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
            final_screenshot=self._export_thumbnail(outputs) or job.get("final_screenshot"),
        )

    def run_job(self, job: dict) -> dict:
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        # Direct line: the agent hears the user's verbatim words (message), matching the web chat
        # box. chat_message (the user-facing text of legacy briefs) beats the make_prompt wrapper.
        prompt = str(job.get("message") or job.get("chat_message") or job.get("prompt") or "").strip()
        asset_paths = [p for p in (job.get("asset_paths") or []) if p]
        questions: list[dict] = []
        errors: list[dict] = []
        deadline = time.monotonic() + self._job_timeout(job)
        self._init_progress(job)

        try:
            if not _api_base():
                raise ApiTransportError("api_base_missing", "CASSETTE_API_URL is not configured for the API transport")
            self._raise_if_cancelled(job_id)
            self._authenticate()

            media_file_ids: list[str] = []
            if asset_paths:
                self._enter_stage(job_id, "upload", "Uploading media to Cassette")
                media_file_ids = self._upload_assets(asset_paths, session_id, deadline, job_id)

            # Media derivatives (analysis evidence/embeddings for the agent, render-source for the
            # export) are generated asynchronously after upload. Starting the run early makes the
            # agent commit an empty edit ("succeeds" but exports a blank 1-frame video); exporting
            # early fails with "render-source is missing". Wait for full readiness first.
            if media_file_ids:
                self._enter_stage(job_id, "media_ready", "Processing uploaded media")
                self._await_media_ready(session_id, media_file_ids, deadline, job_id)

            self._notify_model_selection(job, self._resolve_model_id(job), self._resolve_thinking_config(job))
            self._enter_stage(job_id, "agent", "Cassette agent is editing")
            thread_id = self._ensure_thread(session_id, job)
            run_status, run_questions = self._run_agent(thread_id, session_id, prompt, job, deadline)
            questions.extend(run_questions)

            if run_status == _NEEDS_USER:
                return self._result(
                    _NEEDS_USER,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    risk="medium",
                    extra_quality={
                        "progress_summary": self._questions_summary(questions),
                        # Plan review is judged against the timeline, so attach the digest.
                        **(
                            self._timeline_review_context(session_id)
                            if any(q.get("reason") == "edit_plan_review" for q in questions)
                            else {}
                        ),
                    },
                )
            if run_status == _TIMED_OUT:
                errors.append(
                    {
                        "code": "agent_run_timeout",
                        "message": "Agent run did not finish before the job timeout",
                        "details": {},
                    }
                )
                return self._result(
                    _TIMED_OUT,
                    questions=questions,
                    errors=errors,
                    completion_observed=False,
                    export_completed=False,
                    risk="medium",
                )
            if run_status != _SUCCEEDED:
                errors.append(
                    {
                        "code": "agent_run_incomplete",
                        "message": f"Agent run ended with status '{run_status}'",
                        "details": {},
                    }
                )
                return self._result(
                    _FAILED,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    risk="high",
                )

            edit_summary = self._latest_agent_summary(thread_id) or "Cassette reports the requested edit is complete."

            # The agent committed the edit. Mirror the browser path: unless auto-export is opted into,
            # hand completion to the Hermes supervisor for semantic review (export/continue/needs_user/
            # failed) via a needs_user gate that cassette_review_completion -> ApiTransport.export()
            # resolves. Only auto-export when CASSETTE_API_AUTO_EXPORT is set (the api-success signal is
            # authoritative) — that path is NOT browser-parity and is documented as such.
            if _export_on_complete(job) and not _auto_export():
                questions.append(
                    {
                        "question": edit_summary[:500],
                        "requires_user": False,
                        "reason": "completion_requires_hermes_review",
                        "answer": (
                            "The latest Cassette reply needs Hermes supervisor semantic review before deciding "
                            "whether to export, continue, fail, or ask the user."
                        ),
                    }
                )
                return self._result(
                    _NEEDS_USER,
                    questions=questions,
                    errors=errors,
                    completion_observed=False,
                    export_completed=False,
                    risk="medium",
                    extra_quality={
                        "completion_review_required": True,
                        "completion_source": "cassette_agent_success",
                        "progress_summary": edit_summary,
                        "current_stage": "agent",
                        # The export gate must never again be judged blind: attach the timeline
                        # digest + contact sheet so the reviewer sees what would be exported.
                        **self._timeline_review_context(session_id),
                    },
                )
            if not _export_on_complete(job) and not _auto_export():
                # Conversational turn: the edit is committed, nothing rendered. Attach the CTL
                # digest + contact sheet as the per-turn preview so the user judges the result
                # visually (0.4.0 preview stack) and decides when to export.
                return self._result(
                    _SUCCEEDED,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    export_pending=False,
                    risk="medium",
                    extra_quality={
                        "progress_summary": edit_summary,
                        "current_stage": "agent",
                        **self._timeline_review_context(session_id),
                    },
                )

            # Auto-export (opt-in). If it fails, the edit still happened, so report 'succeeded' with
            # export_pending rather than masking it as a failure (consumed by _job_report).
            self._enter_stage(job_id, "export", "Rendering the export")
            try:
                outputs = self._export_project(session_id, job_id, deadline=deadline)
            except _JobCancelled:
                raise
            except ApiTransportError as exc:
                errors.append(self._error(exc))
                return self._result(
                    _SUCCEEDED,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    export_pending=True,
                    risk="medium",
                    extra_quality={
                        "progress_summary": edit_summary
                        or "Cassette edit committed; the export did not complete in time.",
                        "current_stage": "export",
                    },
                )
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
                extra_quality={"progress_summary": edit_summary or None, "current_stage": "export"},
                final_screenshot=self._export_thumbnail(outputs),
            )
        except _JobCancelled:
            return self._result(
                _CANCELLED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="medium",
                extra_quality={"progress_summary": "Cassette job was cancelled before it finished."},
            )
        except ApiTransportError as exc:
            errors.append(self._error(exc))
            return self._result(
                _FAILED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="high",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(self._error(exc))
            return self._result(
                _FAILED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="high",
            )

    def resume(self, job: dict, response: str) -> dict:
        """Resume a persisted API ``ask_user`` interrupt on the same LangGraph thread."""
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        continuation = job.get("continuation") if isinstance(job.get("continuation"), dict) else {}
        questions = list(job.get("questions") or [])
        errors = list(job.get("errors") or [])
        deadline = time.monotonic() + self._job_timeout(job)
        self._init_progress(job)
        try:
            if continuation.get("transport") != "api":
                raise ApiTransportError(
                    "resume_state_missing",
                    "This API job has no persisted continuation state to resume.",
                )
            thread_id = str(continuation.get("thread_id") or "")
            config = continuation.get("config")
            if not thread_id or not isinstance(config, dict):
                raise ApiTransportError(
                    "resume_state_missing",
                    "The persisted API continuation is incomplete.",
                )
            answer = str(response or "").strip()
            if not answer:
                raise ApiTransportError("missing_required_arg", "response is required to resume a Cassette job")
            self._authenticate()
            interrupts = self._pending_interrupts(thread_id)
            pending_kinds = {(item.get("value") or {}).get("type") for item in interrupts}
            if "edit_plan_review" in pending_kinds:
                # Bare PlanReviewDecision — approve / revise <feedback> / reject, mapped from the
                # user's free-text reply. First-answer-wins with an open editor tab: if the tab
                # already decided, the pending set is empty and we raise the same clean error.
                resume_payload: dict[str, Any] = _plan_review_resume(answer)
            elif "ask_user" in pending_kinds:
                resume_payload = {"action": "respond", "userResponse": answer}
            else:
                raise ApiTransportError(
                    "resume_not_waiting_for_user",
                    "The persisted API thread is no longer waiting for a user response.",
                )
            self._enter_stage(job_id, "agent", "Resuming the Cassette agent")
            run_id = self._post_run(
                thread_id,
                {
                    "assistant_id": "cassette-chat",
                    "command": {"resume": resume_payload},
                    "config": config,
                    "multitask_strategy": "interrupt",
                    "stream_mode": _RUN_STREAM_MODES,
                },
            )
            self._persist_continuation(job_id, thread_id, session_id, config, run_id, interrupts=[])
            run_status, new_questions = self._drive_run(thread_id, run_id, config, job, deadline)
            questions.append(
                {
                    "question": "Cassette requested user input.",
                    "requires_user": False,
                    "reason": "user_response",
                    "answer": "Response supplied by the user.",
                }
            )
            questions.extend(new_questions)
            if run_status == _NEEDS_USER:
                return self._result(
                    _NEEDS_USER,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    risk="medium",
                    extra_quality={
                        "progress_summary": self._questions_summary(questions),
                        # Plan review is judged against the timeline, so attach the digest.
                        **(
                            self._timeline_review_context(session_id)
                            if any(q.get("reason") == "edit_plan_review" for q in questions)
                            else {}
                        ),
                    },
                )
            if run_status == _TIMED_OUT:
                errors.append(
                    {
                        "code": "agent_run_timeout",
                        "message": "Agent run did not finish before the job timeout",
                        "details": {},
                    }
                )
                return self._result(
                    _TIMED_OUT,
                    questions=questions,
                    errors=errors,
                    completion_observed=False,
                    export_completed=False,
                    risk="medium",
                )
            if run_status != _SUCCEEDED:
                raise ApiTransportError("agent_run_incomplete", f"Agent run ended with status '{run_status}'")

            edit_summary = self._latest_agent_summary(thread_id) or "Cassette reports the requested edit is complete."
            if _export_on_complete(job) and not _auto_export():
                questions.append(
                    {
                        "question": edit_summary[:500],
                        "requires_user": False,
                        "reason": "completion_requires_hermes_review",
                        "answer": "The latest Cassette reply needs completion review before export.",
                    }
                )
                return self._result(
                    _NEEDS_USER,
                    questions=questions,
                    errors=errors,
                    completion_observed=False,
                    export_completed=False,
                    risk="medium",
                    extra_quality={
                        "completion_review_required": True,
                        "completion_source": "cassette_agent_success",
                        "progress_summary": edit_summary,
                        "current_stage": "agent",
                        # The export gate must never again be judged blind: attach the timeline
                        # digest + contact sheet so the reviewer sees what would be exported.
                        **self._timeline_review_context(session_id),
                    },
                )
            if not _export_on_complete(job) and not _auto_export():
                return self._result(
                    _SUCCEEDED,
                    questions=questions,
                    errors=errors,
                    completion_observed=True,
                    export_completed=False,
                    export_pending=False,
                    risk="medium",
                    extra_quality={
                        "progress_summary": edit_summary,
                        "current_stage": "agent",
                        **self._timeline_review_context(session_id),
                    },
                )
            self._enter_stage(job_id, "export", "Rendering the export")
            outputs = self._export_project(session_id, job_id, deadline=deadline)
            return self._result(
                _SUCCEEDED,
                outputs=outputs,
                questions=questions,
                errors=errors,
                completion_observed=True,
                export_completed=bool(outputs),
                export_pending=not outputs,
                risk="low" if outputs else "medium",
                extra_quality={"progress_summary": edit_summary, "current_stage": "export"},
                final_screenshot=self._export_thumbnail(outputs),
            )
        except _JobCancelled:
            return self._result(
                _CANCELLED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="medium",
            )
        except ApiTransportError as exc:
            errors.append(self._error(exc))
            return self._result(
                _FAILED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="high",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(self._error(exc))
            return self._result(
                _FAILED,
                questions=questions,
                errors=errors,
                completion_observed=False,
                export_completed=False,
                risk="high",
            )

    # ── auth ──────────────────────────────────────────────────────────────────
    def _authenticate(self) -> None:
        if self._token:
            return
        override = _env("CASSETTE_AUTH_TOKEN")
        if override:
            # Pre-issued bearer (local dev's local-dev-access-token, CI-minted JWTs); skips
            # /api/agent-auth/verify entirely.
            self._token = override
            return
        email, password = _credentials()
        if not email or not password:
            raise ApiTransportError(
                "auth_missing_credentials", "CASSETTE_AUTH_EMAIL/PASSWORD are required for the API transport"
            )
        status, body = self._request(
            "POST", "/api/agent-auth/verify", json_body={"email": email, "password": password}, authed=False
        )
        if status != 200 or not isinstance(body, dict):
            raise ApiTransportError("auth_failed", f"Cassette sign-in failed (HTTP {status})")
        session = body.get("session") or {}
        token = session.get("access_token")
        if not token:
            raise ApiTransportError("auth_failed", "Cassette sign-in returned no access token")
        self._token = str(token)

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

        _, init = self._request(
            "POST",
            "/api/media/upload/init",
            json_body={"fileName": file_name, "mimeType": mime},
            headers=headers,
            expect=200,
        )
        if not isinstance(init, dict) or not init.get("uploadUrl") or not init.get("key"):
            raise ApiTransportError("upload_init_failed", f"upload/init returned an unexpected body for {file_name}")
        key = str(init["key"])
        upload_content_type = str(init.get("uploadContentType") or mime)
        storage_backend = str(init.get("storageBackend") or "r2")
        upload_attempt_id = init.get("uploadAttemptId")

        self._put_bytes(str(init["uploadUrl"]), file_path.read_bytes(), upload_content_type)

        complete_body = {
            "key": key,
            "fileName": file_name,
            "mimeType": mime,
            "storageBackend": storage_backend,
            "metadata": {},
        }
        if upload_attempt_id:
            complete_body["uploadAttemptId"] = upload_attempt_id
        _, complete = self._request(
            "POST", "/api/media/upload/complete", json_body=complete_body, headers=headers, expect=200
        )
        if not isinstance(complete, dict) or not complete.get("mediaFileId"):
            raise ApiTransportError(
                "upload_complete_failed", f"upload/complete returned no mediaFileId for {file_name}"
            )

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
                raise ApiTransportError(
                    "upload_processing_failed",
                    str((body or {}).get("error") or f"Media processing failed for key {key}"),
                )
            time.sleep(self._poll_interval())
        raise ApiTransportError("upload_processing_timeout", f"Media processing did not complete for key {key}")

    def _await_media_ready(self, session_id: str, media_file_ids: list[str], deadline: float, job_id: str = "") -> None:
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
                if (
                    s.get("terminalState") == "failed"
                    or s.get("renderStatus") == "failed"
                    or s.get("analyzeStatus") == "analyze_failed"
                ):
                    raise ApiTransportError(
                        "media_processing_failed",
                        str(s.get("errorMessage") or f"Media {mid} failed processing"),
                        details={
                            "media_file_id": mid,
                            "analyze_status": s.get("analyzeStatus"),
                            "render_status": s.get("renderStatus"),
                        },
                    )
                if s.get("fullyReady") or (self._ai_ready(s) and self._export_ready(s)):
                    ready.add(mid)
                else:
                    last_phase = str(s.get("readinessPhase") or last_phase)
            if wanted <= ready:
                return
            self._tick(job_id, "Processing uploaded media (" + (last_phase or "analyzing") + ")")
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

    # ── upload (with incremental dedupe, parity with browser _asset_paths_needing_upload) ──
    def _upload_assets(self, asset_paths: list[str], session_id: str, deadline: float, job_id: str = "") -> list[str]:
        """Upload each asset once. Skips assets already uploaded in this session (a reused gateway
        session that edits then refines would otherwise accumulate duplicate media in the project),
        matching the browser path's per-session uploaded-asset cache."""
        cache = self._load_upload_cache(session_id)
        batch: dict[str, str] = {}
        ids: list[str] = []
        changed = False
        for path in asset_paths:
            fp = self._asset_fingerprint(path)
            if fp and fp in batch:
                ids.append(batch[fp])
                continue
            if fp and fp in cache:
                batch[fp] = cache[fp]
                ids.append(cache[fp])
                continue
            media_id = self._upload_asset(path, session_id, deadline, job_id)
            ids.append(media_id)
            if fp:
                batch[fp] = media_id
                cache[fp] = media_id
                changed = True
        if changed:
            self._save_upload_cache(session_id, cache)
        return ids

    @staticmethod
    def _asset_fingerprint(path: str) -> str:
        # Gateway media filenames are content-digest based (manifest.py), so name+size is a stable key.
        try:
            p = Path(path)
            return p.name + ":" + str(p.stat().st_size)
        except OSError:
            return ""

    @staticmethod
    def _upload_cache_path(session_id: str) -> Path:
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(session_id))[:96] or "default"
        base = Path(os.getenv("CASSETTE_ASSET_ROOT", str(get_asset_root()))) / "api_uploads"
        base.mkdir(parents=True, exist_ok=True)
        return base / (safe + ".json")

    def _load_upload_cache(self, session_id: str) -> dict[str, str]:
        try:
            return json.loads(self._upload_cache_path(session_id).read_text("utf-8")) or {}
        except (OSError, ValueError):
            return {}

    def _save_upload_cache(self, session_id: str, cache: dict[str, str]) -> None:
        try:
            self._upload_cache_path(session_id).write_text(json.dumps(cache), "utf-8")
        except OSError:
            pass

    def _latest_agent_summary(self, thread_id: str) -> str:
        """Latest assistant message text from the thread state — a real edit summary for the terminal
        report/notification (the browser path derives this from the chat panel)."""
        try:
            _, state = self._request("GET", f"/api/langgraph/threads/{thread_id}/state", expect=200)
            messages = ((state or {}).get("values") or {}).get("messages") or []
            for message in reversed(messages):
                if not isinstance(message, dict):
                    continue
                if (message.get("type") or message.get("role")) not in ("ai", "assistant"):
                    continue
                content = message.get("content")
                if isinstance(content, list):
                    content = " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
                content = str(content or "").strip()
                if content:
                    return content[:700]
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _export_thumbnail(self, outputs: list[dict]) -> str | None:
        """Best-effort still frame from the exported mp4 — the api path has no browser to screenshot,
        so this gives final_screenshot consumers (web demo, terminal image) a real visual artifact."""
        if _env("CASSETTE_API_EXPORT_THUMBNAIL").lower() in {"0", "false", "no", "off"}:
            return None
        try:
            path = next(
                (o.get("local_path") for o in (outputs or []) if isinstance(o, dict) and o.get("local_path")), None
            )
            if not path or not Path(path).exists():
                return None
            import subprocess

            ffmpeg = _env("CASSETTE_FFMPEG_BIN") or "ffmpeg"
            target = Path(path).with_suffix(".thumb.jpg")
            subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-ss", "0.5", "-i", path, "-frames:v", "1", str(target)],
                capture_output=True,
                timeout=30,
            )
            return str(target) if target.exists() and target.stat().st_size > 0 else None
        except Exception:  # noqa: BLE001
            return None

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

    def _timeline_review_context(self, session_id: str) -> dict:
        """Best-effort CTL + contact sheet for review moments — never fails the run."""
        try:
            from . import timeline as timeline_mod
            from . import tools as tools_mod

            document = self.get_project_document(session_id)
            context: dict[str, Any] = {"timeline_ctl": timeline_mod.render_ctl(document)}
            sheet = tools_mod.build_contact_sheet(document, session_id)
            if sheet:
                context["contact_sheet"] = sheet
            return context
        except Exception:  # noqa: BLE001
            return {}

    # ── project document read ─────────────────────────────────────────────────
    def get_project_document(self, session_id: str) -> dict:
        """Fetch the live ProjectDocument for a session's project (agent-tier read)."""
        from urllib.parse import quote

        self._authenticate()
        _, body = self._request("GET", f"/api/projects/{quote(str(session_id), safe='')}", expect=200)
        document = body.get("document") if isinstance(body, dict) else None
        if not isinstance(document, dict):
            raise ApiTransportError("project_document_missing", "Cassette returned no project document")
        return document

    def post_project_command(self, session_id: str, envelope: dict) -> dict:
        """POST a no-LLM project command (the manual-editor lane); returns the ProjectCommitEvent.

        The route replies 200 for both outcomes: a commit event on success, or
        {ok:false, code, message} on validation/commit failure."""
        from urllib.parse import quote

        self._authenticate()
        _, body = self._request(
            "POST",
            f"/api/projects/{quote(str(session_id), safe='')}/commands",
            json_body=envelope,
            expect=200,
        )
        if not isinstance(body, dict):
            raise ApiTransportError("command_failed", "Cassette returned a malformed command response")
        if body.get("ok") is False:
            code = str(body.get("code") or "command_failed").lower()
            raise ApiTransportError(code, str(body.get("message") or "Project command failed"))
        return body

    # ── agent run ─────────────────────────────────────────────────────────────
    def _thread_metadata(self, session_id: str, thread_id: str, job: dict) -> dict:
        # Full cassette shape, mirroring the editor's buildThreadMetadata: the /try tab's resume
        # path (plan review answered in the browser) hard-requires isCassetteThreadMetadata to
        # pass, so every field below is load-bearing — a partial dict breaks tab-side resume.
        return {
            "schemaVersion": 1,
            "threadKind": "cassette-chat",
            "chatSessionId": thread_id,
            "projectId": session_id,
            "mediaSessionId": session_id,
            "mode": "auto",
            "turnStrategy": "default",
            "turnKind": "conversation",
            "reinitMode": None,
            "modelId": self._resolve_model_id(job),
            "thinkingConfig": self._resolve_thinking_config(job),
            "locale": job.get("cassette_language") or None,
        }

    def _ensure_thread(self, session_id: str, job: dict) -> str:
        # One thread per SESSION, reused across jobs: each job is one conversational turn on the
        # same LangGraph thread, so the agent keeps memory and the deep link stays stable. The
        # upstream LangGraph server 422s non-UUID thread ids, so the id is minted client-side;
        # if_exists:'do_nothing' makes the ensure idempotent (and heals a server-side deletion —
        # the checkpointed conversation is lost then, but the session keeps working).
        from . import manifest as manifest_mod

        session_hash = manifest_mod.resolve_session_hash(session_id=session_id)
        saved_thread = str(manifest_mod.load_session_thread(session_hash).get("thread_id") or "")
        thread_id = saved_thread or str(uuid.uuid4())
        metadata = self._thread_metadata(session_id, thread_id, job)
        _, body = self._request(
            "POST",
            "/api/langgraph/threads",
            json_body={"thread_id": thread_id, "metadata": metadata, "if_exists": "do_nothing"},
            expect=200,
        )
        created = (body.get("thread_id") or body.get("threadId")) if isinstance(body, dict) else None
        if created:
            thread_id = str(created)
        # if_exists:'do_nothing' never updates an existing thread, but the tab rebuilds its resume
        # session context from this metadata — keep modelId/thinkingConfig fresh per turn.
        if saved_thread:
            try:
                self._request(
                    "PATCH",
                    f"/api/langgraph/threads/{thread_id}",
                    json_body={"metadata": self._thread_metadata(session_id, thread_id, job)},
                )
            except Exception:  # noqa: BLE001 — the run's own sessionContext stays authoritative
                pass
        editor_url = _editor_url(session_id, thread_id, job)
        manifest_mod.save_session_thread(session_hash, thread_id, editor_url)
        job["chat_thread_id"] = thread_id
        job["editor_url"] = editor_url
        job_id = str(job.get("job_id") or "")
        if job_id:
            try:
                from . import jobs

                jobs.update_job(job_id, chat_thread_id=thread_id, editor_url=editor_url)
            except Exception:  # noqa: BLE001 — persisting the link must not fail the run
                pass
        return thread_id

    def _session_context(self, session_id: str, job: dict, prompt: str, thread_id: str | None = None) -> dict:
        # Mirrors CassetteAgentSessionContext (buildCurrentSessionContext in the editor):
        # projectId/mediaSessionId collapse onto the project session id, while chatSessionId/
        # threadId carry the UUID thread the editor's ChatPanel also uses as its stream thread.
        chat_id = thread_id or str(job.get("chat_thread_id") or session_id)
        return {
            "chatSessionId": chat_id,
            "threadId": chat_id,
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
        # Honor the user's model choice. model_selection stores a UI label under 'model' (browser.py
        # captures only the label); an explicit id ('model_id'/'modelId') wins if present, otherwise
        # map the label -> id so the api path runs the SAME model the browser path would select.
        # Fall back to the env override or the editor default only when nothing maps.
        ms = job.get("model_selection") or {}
        explicit = str(ms.get("model_id") or ms.get("modelId") or "").strip()
        if explicit in _SUPPORTED_AGENT_MODEL_IDS:
            return explicit
        label = str(ms.get("model") or "").strip()
        if label in _SUPPORTED_AGENT_MODEL_IDS:  # already an id
            return label
        mapped = _model_id_from_label(label) if label else None
        if mapped in _SUPPORTED_AGENT_MODEL_IDS:
            return mapped
        env_model = _env("CASSETTE_API_MODEL_ID")
        if env_model in _SUPPORTED_AGENT_MODEL_IDS:
            return env_model
        # A model was explicitly chosen but could not be mapped: fail loudly when selection is
        # required (browser parity — browser.py raises rather than silently running the default).
        if label and _require_model_selection():
            raise ApiTransportError(
                "model_selection_failed",
                f"Could not map the selected Cassette model '{label}' to a supported model id; "
                "set CASSETTE_API_MODEL_ID or disable CASSETTE_REQUIRE_MODEL_SELECTION.",
            )
        return DEFAULT_AGENT_MODEL_ID

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
        if raw in valid:
            return raw
        # Honor the browser path's CASSETTE_DEFAULT_THINKING_LEVEL default before the hard-coded one.
        env_default = _env("CASSETTE_DEFAULT_THINKING_LEVEL").lower()
        return env_default if env_default in valid else _DEFAULT_THINKING

    def _run_agent(
        self, thread_id: str, session_id: str, prompt: str, job: dict, deadline: float
    ) -> tuple[str, list[dict]]:
        """Start the run, satisfy interrupts headlessly, return (terminal_status, questions).

        Uploaded media is NOT passed as ids — the cassette-chat graph reads the session-scoped media
        catalog keyed by sessionContext.mediaSessionId (== the upload x-session-id)."""
        job_id = str(job.get("job_id") or "")
        turn_id = f"{job_id or session_id}-turn"
        session_context = self._session_context(session_id, job, prompt, thread_id=thread_id)
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
            # 'reject' (not the editor's 'rollback'): a plugin turn must never cancel a run the
            # user started from the open editor tab — the busy turn fails typed instead.
            "multitask_strategy": "reject",
            "stream_mode": _RUN_STREAM_MODES,
        }
        run_id = self._post_run(thread_id, run_body)
        self._persist_continuation(
            job_id,
            thread_id,
            session_id,
            config,
            run_id,
            interrupts=[],
        )
        return self._drive_run(thread_id, run_id, config, job, deadline)

    def _drive_run(
        self,
        thread_id: str,
        run_id: str,
        config: dict[str, Any],
        job: dict,
        deadline: float,
    ) -> tuple[str, list[dict]]:
        """Drive a new or resumed run until success, timeout, or genuine user input."""
        questions: list[dict] = []
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        try:
            return self._drive_run_inner(thread_id, run_id, config, job, deadline, questions, job_id, session_id)
        finally:
            if (stop := getattr(self, "_active_stream_stop", None)) is not None:
                stop.set()
                self._active_stream_stop = None

    def _drive_run_inner(
        self,
        thread_id: str,
        run_id: str,
        config: dict[str, Any],
        job: dict,
        deadline: float,
        questions: list[dict],
        job_id: str,
        session_id: str,
    ) -> tuple[str, list[dict]]:
        while True:
            self._active_stream_stop = self._refresh_stream_listener(
                thread_id, run_id, job, getattr(self, "_active_stream_stop", None)
            )
            status = self._await_run(thread_id, run_id, deadline, job_id)
            if status == "interrupted":
                interrupts = self._pending_interrupts(thread_id)
                if not interrupts:
                    # Interrupted with nothing pending == treat as needing user. Carry a summary so the
                    # terminal message is not a bare headline (parity with the browser needs_user path).
                    summary = self._latest_agent_summary(thread_id) or "Cassette paused and needs input to continue."
                    questions.append(
                        {
                            "question": summary[:500],
                            "requires_user": True,
                            "reason": "cassette_agent_question",
                            "answer": "",
                        }
                    )
                    self._persist_continuation(job_id, thread_id, session_id, config, run_id, interrupts=[])
                    return _NEEDS_USER, questions
                resume_value, new_questions, needs_user = self._resume_value(interrupts)
                questions.extend(new_questions)
                if needs_user:
                    # A genuine user question was raised (e.g. ask_user) with no auto-reply configured:
                    # leave the thread interrupted and hand back to the Hermes/user review loop.
                    self._persist_continuation(
                        job_id,
                        thread_id,
                        session_id,
                        config,
                        run_id,
                        interrupts=interrupts,
                    )
                    return _NEEDS_USER, questions
                run_id = self._post_run(
                    thread_id,
                    {
                        "assistant_id": "cassette-chat",
                        "command": {"resume": resume_value},
                        "config": config,
                        "multitask_strategy": "interrupt",
                        "stream_mode": _RUN_STREAM_MODES,
                    },
                )
                self._persist_continuation(job_id, thread_id, session_id, config, run_id, interrupts=[])
                continue
            if status == "success":
                self._clear_continuation(job_id)
                return _SUCCEEDED, questions
            if status == "timeout":
                return _TIMED_OUT, questions
            self._clear_continuation(job_id)
            raise ApiTransportError("agent_run_error", f"Agent run failed: {self._run_error_detail(thread_id)}")

    @staticmethod
    def _interrupt_metadata(interrupts: list[dict]) -> list[dict]:
        metadata: list[dict] = []
        for item in interrupts:
            value = item.get("value") if isinstance(item, dict) else {}
            if not isinstance(value, dict):
                value = {}
            metadata.append(
                {
                    "id": str(item.get("id") or "") if isinstance(item, dict) else "",
                    "type": str(value.get("type") or "unknown"),
                    "tool_call_id": str((value.get("toolCall") or {}).get("id") or "")
                    if isinstance(value.get("toolCall"), dict)
                    else "",
                }
            )
        return metadata

    def _persist_continuation(
        self,
        job_id: str,
        thread_id: str,
        session_id: str,
        config: dict[str, Any],
        run_id: str,
        *,
        interrupts: list[dict],
    ) -> None:
        if not job_id:
            return
        try:
            from . import jobs

            jobs.update_job(
                job_id,
                continuation={
                    "transport": "api",
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "session_id": session_id,
                    "config": config,
                    "interrupts": self._interrupt_metadata(interrupts),
                    "updated_at": self._now_iso(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            try:
                import runtime_config

                restart_safe_required = runtime_config.is_mcp_runtime()
            except Exception:  # noqa: BLE001
                restart_safe_required = False
            if restart_safe_required:
                raise ApiTransportError(
                    "continuation_persist_failed",
                    "Could not persist the private API continuation required for restart-safe resume.",
                    details={"type": type(exc).__name__},
                ) from exc

    @staticmethod
    def _clear_continuation(job_id: str) -> None:
        if not job_id:
            return
        try:
            from . import jobs

            jobs.update_job(job_id, continuation=None, resume_request=None)
        except Exception:  # noqa: BLE001
            pass

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

    # ── run event stream (enhancement channel; poll drives completion) ───────
    @staticmethod
    def _iter_sse(resp):
        """Yield (event, data) pairs from a line-iterable SSE response body."""
        event: str | None = None
        data_lines: list[str] = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n") if isinstance(raw, bytes) else str(raw).rstrip("\r\n")
            if line == "":
                if data_lines:
                    yield (event or "message", "\n".join(data_lines))
                event, data_lines = None, []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        if data_lines:
            yield (event or "message", "\n".join(data_lines))

    def _refresh_stream_listener(self, thread_id: str, run_id: str, job: dict, prev: threading.Event | None):
        """(Re)start the run-stream listener when the run id changes; return the live stop event."""
        if not _stream_enabled() or not str(job.get("job_id") or ""):
            return prev
        if prev is not None and getattr(prev, "run_id", None) == run_id:
            return prev
        if prev is not None:
            prev.set()
        stop = threading.Event()
        stop.run_id = run_id  # type: ignore[attr-defined]
        threading.Thread(
            target=self._consume_run_stream,
            args=(thread_id, run_id, dict(job), stop),
            daemon=True,
            name=f"cassette-run-stream-{run_id[:8]}",
        ).start()
        return stop

    def _consume_run_stream(self, thread_id: str, run_id: str, job: dict, stop: threading.Event) -> None:
        """Join the run's SSE stream and fold events onto the persisted job.

        Best-effort by design: any failure (drop, parse error, timeout) simply ends the listener —
        the poll loop still drives the run, so the only loss is event granularity."""
        job_id = str(job.get("job_id") or "")
        session_id = _session_id(job)
        try:
            from . import jobs as jobs_mod
            from . import timeline as timeline_mod

            try:
                baseline = self.get_project_document(session_id)
            except Exception:  # noqa: BLE001 — fresh project: delta baseline is the empty document
                baseline = {"version": 0, "entities": {}, "order": {}}
            url = f"{_api_base()}/api/langgraph/threads/{thread_id}/runs/{run_id}/stream"
            request = Request(url, headers=self._auth_headers({"Accept": "text/event-stream"}))
            with urlopen(request, timeout=_stream_read_timeout()) as resp:
                progress: list[str] = list(job.get("plan_progress") or [])
                for event, data in self._iter_sse(resp):
                    if stop.is_set():
                        return
                    if event != "custom":
                        continue
                    try:
                        payload = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("type") == "project_operation_committed" and isinstance(
                        payload.get("document"), dict
                    ):
                        delta = timeline_mod.render_delta(baseline, payload["document"])
                        jobs_mod.update_job(job_id, timeline_delta=delta)
                    else:
                        label = str(
                            payload.get("label")
                            or payload.get("status")
                            or payload.get("title")
                            or payload.get("type")
                            or ""
                        ).strip()[:80]
                        if label:
                            progress = (progress + [label])[-10:]
                            jobs_mod.update_job(job_id, plan_progress=progress)
        except Exception:  # noqa: BLE001 — enhancement channel only
            return

    def _post_run(self, thread_id: str, body: dict) -> str:
        status, resp = self._request("POST", f"/api/langgraph/threads/{thread_id}/runs", json_body=body)
        if status == 422 and body.get("multitask_strategy") == "reject":
            raise ApiTransportError(
                "thread_busy",
                "The session's editor thread is already running a turn (likely started from the "
                "open editor tab); wait for it to finish and retry.",
            )
        if status != 200:
            raise ApiTransportError("run_create_failed", f"LangGraph run create failed with HTTP {status}")
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
            self._tick(job_id, "Cassette agent is editing (" + (status or "running") + ")")
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
            self._request(
                "POST", f"/api/langgraph/threads/{thread_id}/runs/{run_id}/cancel?action=interrupt", json_body={}
            )
        except Exception:  # noqa: BLE001
            pass

    def _cancel_export(self, export_job_id: str) -> None:
        """Best-effort server-side cancel of an in-flight export/render job on cancellation."""
        try:
            self._request("POST", f"/api/export/jobs/{export_job_id}/cancel", json_body={})
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
            for interrupt in task.get("interrupts") or []:
                value = interrupt.get("value") if isinstance(interrupt, dict) else None
                if isinstance(value, dict):
                    out.append({"id": interrupt.get("id"), "value": value})
        # Some LangGraph versions surface interrupts on the top-level __interrupt__ channel. Guard
        # against an explicit null (values["__interrupt__"] == None) which .get(..., []) would return.
        for interrupt in (state.get("values") or {}).get("__interrupt__") or []:
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
            # Each auto-handled interrupt leaves an audit record (requires_user=False), matching the
            # browser path's routine-approval question entries.
            if kind == "edit_plan_review":
                if _unattended() or _plan_review_mode() != "user":
                    questions.append(
                        {
                            "question": "Cassette requested plan approval.",
                            "requires_user": False,
                            "reason": "routine_plan_approval",
                            "answer": "Auto-approved the edit plan.",
                        }
                    )
                    return {"action": "approve"}, questions, False
                # Web-agent-page parity: the plan is a decision, not a formality. Surface it as a
                # real question (approve / revise <feedback> / reject) instead of eating it.
                from . import timeline as timeline_mod

                payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
                block = timeline_mod.plan_review_block(payload)
                questions.append(
                    {
                        "question": block,
                        "requires_user": True,
                        "reason": "edit_plan_review",
                        "answer": "",
                    }
                )
                return None, questions, True
            if kind == "mode_switch":
                questions.append(
                    {
                        "question": "Cassette requested a mode switch.",
                        "requires_user": False,
                        "reason": "routine_mode_switch",
                        "answer": "Auto-switched to auto mode.",
                    }
                )
                return {"action": "switch_mode", "selectedMode": "auto"}, questions, False
            if kind == "init_questions":
                questions.append(
                    {
                        "question": "Cassette asked initialization questions.",
                        "requires_user": False,
                        "reason": "routine_init_questions",
                        "answer": "Proceeded with defaults.",
                    }
                )
                return {}, questions, False
            if kind == "ask_user":
                text = str(value.get("prompt") or value.get("question") or "")
                # Classify like the browser path (classify_cassette_question): a *routine* ambiguity
                # is auto-answered with a safe default and the run continues; only a genuine user
                # choice or a missing-required-asset returns needs_user (carrying the specific reason).
                from . import prompt as _prompt

                classification = _prompt.classify_cassette_question(text)
                reason = classification.get("reason") or "cassette_agent_question"
                default_answer = classification.get("answer") or ""
                auto_reply = _env("CASSETTE_API_DEFAULT_ASK_USER_REPLY")
                if not classification.get("requires_user"):
                    reply = auto_reply or default_answer or "Please proceed using your best judgment."
                    questions.append(
                        {"question": text[:500], "requires_user": False, "reason": reason, "answer": reply}
                    )
                    return {"action": "respond", "userResponse": reply}, questions, False
                if auto_reply:  # operator opted into unattended auto-answering even real questions
                    questions.append(
                        {"question": text[:500], "requires_user": False, "reason": reason, "answer": auto_reply}
                    )
                    return {"action": "respond", "userResponse": auto_reply}, questions, False
                questions.append(
                    {"question": text[:500], "requires_user": True, "reason": reason, "answer": default_answer}
                )
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

        _, created = self._request(
            "POST", f"/api/export/projects/{quote(str(session_id), safe='')}/jobs", json_body={}, expect=202
        )
        export_job_id = created.get("jobId") if isinstance(created, dict) else None
        if not export_job_id:
            raise ApiTransportError("export_create_failed", "Export create returned no jobId")
        export_job_id = str(export_job_id)

        file_url: str | None = None
        while time.monotonic() < deadline:
            if self._cancelled(job_id):
                self._cancel_export(export_job_id)  # stop the server-side Lambda render, not just abandon it
                raise _JobCancelled()
            _, body = self._request("GET", f"/api/export/jobs/{export_job_id}", expect=200)
            status = str((body or {}).get("status") or "") if isinstance(body, dict) else ""
            if status == "done":
                file_url = (body or {}).get("fileUrl") or f"/api/export/jobs/{export_job_id}/file"
                break
            if status == "error":
                raise ApiTransportError("export_failed", str((body or {}).get("error") or "Export job failed"))
            pct = (body or {}).get("progressPercent") if isinstance(body, dict) else None
            self._tick(
                job_id,
                "Rendering the export (" + (status or "rendering") + (f" {pct}%" if pct is not None else "") + ")",
            )
            time.sleep(self._poll_interval())
        if not file_url:
            raise ApiTransportError("export_timeout", "Export job did not complete in time")

        target = _exports_dir(job_id) / f"{job_id}.mp4"
        self._download(f"/api/export/jobs/{export_job_id}/file", target)
        return [
            {
                "text": target.name,
                "href": file_url,
                "download": target.name,
                "local_path": str(target),
                "kind": "video",
            }
        ]

    def _download(self, path: str, target: Path, *, _retried: bool = False) -> None:
        request = Request(_api_base() + path, method="GET", headers=self._auth_headers({}))
        try:
            with (
                urlopen(request, timeout=max(120.0, self._http_timeout_for_upload(0))) as response,
                target.open("wb") as fh,
            ):
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
        except HTTPError as exc:
            if exc.code == 401 and not _retried:
                self._token = None
                self._authenticate()
                self._download(path, target, _retried=True)
                return
            raise ApiTransportError("export_download_failed", f"Export download failed (HTTP {exc.code})") from exc
        except URLError as exc:
            raise ApiTransportError("export_download_failed", f"Export download failed: {exc.reason}") from exc

    # ── progress telemetry (parity with the browser path's job-record updates) ──
    def _init_progress(self, job: dict) -> None:
        # Fresh per run_job (get_transport() returns a new instance per call).
        self._job = job
        self._stage_timings: dict[str, dict] = {}
        self._current_stage = ""
        now = time.monotonic()
        self._last_event = 0.0  # force an event on the first stage
        self._last_heartbeat = now  # first heartbeat waits one full interval
        self._run_started = now

    def _enter_stage(self, job_id: str, stage: str, summary: str) -> None:
        """Mark the start of a phase: finalize the previous stage timing, write current_stage +
        stage_timings + an immediate progress event, matching browser.begin_stage/finish_stage."""
        iso = self._now_iso()
        if self._current_stage and self._current_stage in self._stage_timings:
            prev = self._stage_timings[self._current_stage]
            prev.setdefault("status", "succeeded")
            prev["finished_at"] = iso
        self._current_stage = stage
        entry = self._stage_timings.get(stage) or {"attempts": 0, "started_at": iso, "started_mono": time.monotonic()}
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["status"] = "running"
        entry.setdefault("started_at", iso)
        entry.setdefault("started_mono", time.monotonic())
        entry["duration_sec"] = round(time.monotonic() - entry["started_mono"], 1)
        self._stage_timings[stage] = entry
        self._last_event = 0.0  # always emit an event at a stage boundary
        self._tick(job_id, summary, force_event=True)

    def _tick(
        self, job_id: str, summary: str, status: str = "running", outputs: list | None = None, force_event: bool = False
    ) -> None:
        """Called from phase boundaries and inside the poll loops. Appends a bounded progress_events
        entry on the event interval and sends a TEXT progress heartbeat on the snapshot interval (the
        api path has no browser, so the screenshot heartbeat becomes a text heartbeat)."""
        if not job_id:
            return
        now = time.monotonic()
        if self._current_stage in self._stage_timings:
            self._stage_timings[self._current_stage]["duration_sec"] = round(
                now - self._stage_timings[self._current_stage].get("started_mono", now), 1
            )
        if force_event or (now - self._last_event) >= self._event_interval():
            self._last_event = now
            self._append_event(job_id, summary, status, outputs)
        if (now - self._last_heartbeat) >= self._heartbeat_interval():
            self._last_heartbeat = now
            self._send_heartbeat(summary)

    def _append_event(self, job_id: str, summary: str, status: str, outputs: list | None) -> None:
        try:
            from . import jobs

            events = list(jobs.load_job(job_id).get("progress_events") or [])[-9:]
            events.append(
                {
                    "at": self._now_iso(),
                    "status": status,
                    "summary": str(summary)[:500],
                    "stage": self._current_stage,
                    "output_link_count": len(outputs or []),
                }
            )
            jobs.update_job(
                job_id,
                progress_events=events,
                current_stage=self._current_stage,
                stage_timings=self._public_stage_timings(),
            )
        except Exception:  # noqa: BLE001 — progress recording must never break the run
            pass

    def _send_heartbeat(self, summary: str) -> None:
        job = getattr(self, "_job", None)
        if not isinstance(job, dict):
            return
        delivery = job.get("delivery") or {}
        if not delivery.get("chat_id"):
            return
        try:
            from . import notifier

            elapsed = int(time.monotonic() - getattr(self, "_run_started", time.monotonic()))
            stage = self._current_stage or "running"
            message = f"Cassette job in progress — {stage} ({elapsed}s elapsed)."
            if summary:
                message += f"\n{str(summary)[:300]}"
            notifier.notify_gateway_text(delivery, message, reason="cassette_progress")
        except Exception:  # noqa: BLE001
            pass

    def _public_stage_timings(self) -> dict:
        return {k: {kk: vv for kk, vv in v.items() if kk != "started_mono"} for k, v in self._stage_timings.items()}

    def _notify_model_selection(self, job: dict, model_id: str, thinking: str) -> None:
        # Mirror browser.py: deliver the 'Cassette model selected' gateway notice and persist it.
        job_id = str(job.get("job_id") or "")
        selection = job.get("model_selection") or {}
        if not job_id or (selection.get("source") == "session_preference"):
            return
        try:
            from . import jobs, notifier

            enriched = dict(job)
            enriched["model_selection"] = {**selection, "resolved_model_id": model_id, "resolved_thinking": thinking}
            result = notifier.notify_model_selection(enriched)
            if result:
                jobs.update_job(job_id, model_selection_notification=result)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _now_iso() -> str:
        from . import jobs

        return jobs.now_iso()

    @staticmethod
    def _event_interval() -> float:
        return _env_num("CASSETTE_PROGRESS_INTERVAL_SEC", 30.0, 5.0, getter=os.getenv)

    @staticmethod
    def _heartbeat_interval() -> float:
        return _env_num("CASSETTE_PROGRESS_SNAPSHOT_SEC", 180.0, 30.0, getter=os.getenv)

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
                return self._request(
                    method,
                    path,
                    json_body=json_body,
                    headers=headers,
                    authed=authed,
                    expect=expect,
                    timeout=timeout,
                    _retried=True,
                )
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
            raise ApiTransportError(
                "http_error",
                f"{method} {path} -> HTTP {status}{f': {detail}' if detail else ''}",
                details={"status": status, "path": path},
            )
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
        # How long to wait for uploaded media to become fully ready. Generous — analysis + embeddings +
        # render-source take real time for longer clips. Prefer CASSETTE_API_MEDIA_READY_TIMEOUT_SEC,
        # then the browser path's CASSETTE_UPLOAD_TIMEOUT_SEC, then a default.
        raw = _env("CASSETTE_API_MEDIA_READY_TIMEOUT_SEC") or _env("CASSETTE_UPLOAD_TIMEOUT_SEC")
        try:
            return max(30.0, float(raw or "300"))
        except ValueError:
            return 300.0

    @staticmethod
    def _run_start_timeout() -> float:
        # How long a run may stay 'pending' before we declare the queue stalled. Generous by default
        # to tolerate cold starts; override with CASSETTE_API_RUN_START_TIMEOUT_SEC.
        return _env_num("CASSETTE_API_RUN_START_TIMEOUT_SEC", 120.0, 30.0)

    @staticmethod
    def _poll_interval() -> float:
        return _env_num("CASSETTE_API_POLL_INTERVAL_SEC", 3.0, 1.0)

    @staticmethod
    def _recursion_limit() -> int:
        return _env_num("CASSETTE_API_RECURSION_LIMIT", 344, 25, cast=int)

    @staticmethod
    def _http_timeout_for_upload(num_bytes: int) -> float:
        # Allow large uploads/downloads more time than ordinary JSON calls.
        return max(60.0, _http_timeout(), num_bytes / (256 * 1024))
