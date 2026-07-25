from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse, urlunparse
from urllib.request import Request, urlopen

from . import jobs, notifier
from .manifest import get_asset_root
from .prompt import ROUTINE_PLAN_APPROVAL, classify_cassette_question


class ExportError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BrowserLaunchError(RuntimeError):
    pass


class BrowserPageLoadError(RuntimeError):
    pass


class BrowserUIReadyError(BrowserPageLoadError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BrowserConnectivityError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BrowserAuthError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BrowserJobCancelled(RuntimeError):
    pass


class BrowserUploadTimeoutError(RuntimeError):
    pass


DEFAULT_CHAT_SELECTOR = "[data-testid^='chat-input-textarea-'],textarea[placeholder*='Describe'],textarea"
DEFAULT_SEND_SELECTOR = "[data-testid^='chat-input-send-'],button[type='submit']"


_PLAYWRIGHT: Any = None
_PLAYWRIGHT_THREAD_ID: int | None = None
_BROWSER_SESSIONS: dict[str, dict[str, Any]] = {}
_BROWSER_WORKER: ThreadPoolExecutor | None = None
_BROWSER_WORKER_LOCK = threading.Lock()
_BROWSER_WORKER_THREAD_ID: int | None = None
_MODEL_OPTIONS_WORKER: ThreadPoolExecutor | None = None
_MODEL_OPTIONS_WORKER_LOCK = threading.Lock()
_MODEL_OPTIONS_WORKER_THREAD_ID: int | None = None
_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "needs_user", "timed_out", "cancelled"}


def _browser_reuse_enabled() -> bool:
    return os.getenv("CASSETTE_BROWSER_SESSION_REUSE", "true").lower() not in {"0", "false", "no", "off"}


def _browser_worker_enabled() -> bool:
    return os.getenv("CASSETTE_BROWSER_WORKER_THREAD", "true").lower() not in {"0", "false", "no", "off"}


def _model_options_worker_enabled() -> bool:
    return os.getenv("CASSETTE_MODEL_OPTIONS_WORKER_THREAD", "true").lower() not in {"0", "false", "no", "off"}


def _browser_session_key(job: dict) -> str:
    return str(job.get("cassette_session_id") or job.get("session_hash") or "default")


def _normalize_cassette_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    # The browser page is the session state. Query/hash variants such as
    # ?new=true must not let a tool caller accidentally discard that state.
    return urlunparse(parsed._replace(query="", fragment=""))


def check_cassette_connectivity(url: str | None = None, timeout_sec: float | None = None) -> dict[str, Any]:
    target = _normalize_cassette_url(url or _runtime_env("CASSETTE_URL") or "https://sg.trycassette.online/agent")
    parsed = urlparse(target)
    if parsed.scheme in {"", "file"}:
        return {"ok": True, "status": "skipped", "reason": "local_url"}
    timeout = timeout_sec
    if timeout is None:
        try:
            timeout = max(1.0, float(_runtime_env("CASSETTE_PING_TIMEOUT_SEC") or "10"))
        except ValueError:
            timeout = 10.0
    last_error = ""
    for method in ("HEAD", "GET"):
        try:
            req = Request(target, method=method, headers={"User-Agent": "oh-my-cassette/1.0"})
            with urlopen(req, timeout=timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
            if 200 <= status < 400 or status in {401, 403}:
                return {"ok": True, "status": "reachable", "http_status": status}
            return {"ok": False, "code": "cassette_http_unhealthy", "http_status": status}
        except HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            if status in {401, 403}:
                return {"ok": True, "status": "reachable", "http_status": status}
            if method == "HEAD" and status in {405, 501}:
                last_error = f"http_{status}"
                continue
            return {"ok": False, "code": "cassette_http_unhealthy", "http_status": status}
        except (TimeoutError, URLError, OSError) as exc:
            last_error = type(exc).__name__
            if method == "HEAD":
                continue
            return {"ok": False, "code": "cassette_unreachable", "details": {"type": last_error}}
    return {"ok": False, "code": "cassette_unreachable", "details": {"type": last_error or "unknown"}}


def _runtime_env(name: str) -> str:
    try:
        import runtime_config

        adapter = runtime_config.runtime_adapter()
        if adapter == runtime_config.MCP_ADAPTER:
            return runtime_config.mcp_env_value(name)
        if adapter == runtime_config.WEB_ADAPTER:
            return str(os.getenv(name, "") or "").strip()
    except Exception:  # noqa: BLE001 — retain the Hermes fallback below
        pass
    getter = getattr(notifier, "_runtime_env", None)
    if callable(getter):
        return str(getter(name) or "").strip()
    return str(os.getenv(name, "")).strip()


def _cassette_auth_credentials() -> tuple[str, str]:
    email = (
        _runtime_env("CASSETTE_AUTH_EMAIL") or _runtime_env("CASSETTE_AUTH_ACCOUNT") or _runtime_env("CASSETTE_EMAIL")
    )
    password = _runtime_env("CASSETTE_AUTH_PASSWORD") or _runtime_env("CASSETTE_PASSWORD")
    return email, password


def _first_visible_locator(page: Any, selectors: tuple[str, ...], timeout_ms: int = 5000) -> Any | None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for selector in selectors:
            try:
                locators = page.locator(selector)
                count = min(locators.count(), 20)
            except Exception:
                continue
            for index in range(count):
                locator = locators.nth(index)
                try:
                    if hasattr(locator, "is_visible") and not locator.is_visible():
                        continue
                    return locator
                except Exception:
                    continue
        time.sleep(0.1)
    return None


def _cassette_auth_element_state(page: Any) -> dict[str, Any]:
    try:
        return page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const signupEmail = document.querySelector("#agent-auth-email");
                const loginEmail = document.querySelector("#agent-auth-email-login");
                const password = document.querySelector("#agent-auth-password");
                return {
                    signup_email_visible: visible(signupEmail),
                    login_email_visible: visible(loginEmail),
                    password_visible: visible(password),
                };
            }"""
        )
    except Exception:
        return {}


def _page_requires_auth(page: Any) -> bool:
    state = _cassette_auth_element_state(page)
    return bool(
        state.get("signup_email_visible") or (state.get("login_email_visible") and state.get("password_visible"))
    )


def _switch_to_cassette_login_form(page: Any) -> None:
    state = _cassette_auth_element_state(page)
    if state.get("login_email_visible") and state.get("password_visible"):
        return
    if not state.get("signup_email_visible"):
        return
    try:
        page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const signupEmail = document.querySelector("#agent-auth-email");
                const form = signupEmail?.closest("form");
                const root = form?.parentElement || signupEmail?.closest("main,section") || document.body;
                const formRect = form?.getBoundingClientRect();
                const buttons = Array.from(root.querySelectorAll("button[type='button'],button:not([type])"))
                    .filter((button) => {
                        if (!visible(button) || button.disabled || button.getAttribute("aria-disabled") === "true") return false;
                        if (button.closest("form")) return false;
                        if (formRect) {
                            const rect = button.getBoundingClientRect();
                            if (rect.bottom > formRect.top + 2) return false;
                        }
                        return true;
                    });
                const labels = (button) => [
                    button.getAttribute("aria-label"),
                    button.getAttribute("title"),
                    button.getAttribute("data-value"),
                    button.getAttribute("value"),
                    button.id,
                    button.name,
                    button.innerText,
                    button.textContent,
                ].filter(Boolean).join(" ").replace(/\\s+/g, " ").trim().toLowerCase();
                const target = buttons.find((button) => {
                    const label = labels(button);
                    return /(^|\\b)(log in|login|sign in|signin)(\\b|$)/.test(label) || /登录|登入|登陆/.test(label);
                }) || (() => {
                    const wideButtons = buttons.filter((button) => {
                        const rect = button.getBoundingClientRect();
                        return rect.width * rect.height >= 1200;
                    });
                    const tabButtons = wideButtons.length >= 2 ? wideButtons : buttons;
                    return tabButtons.length >= 2 ? tabButtons[1] : null;
                })();
                if (target) target.click();
            }"""
        )
    except Exception:
        pass


def _ensure_cassette_authenticated(page: Any, timeout_ms: int = 30000) -> dict[str, Any]:
    requires_auth = _page_requires_auth(page)
    if not requires_auth:
        probe_state = _wait_for_initial_auth_or_agent_ui(page, timeout_ms=min(timeout_ms, 10000))
        requires_auth = probe_state == "requires_auth"
    if not requires_auth:
        return {"status": "skipped", "reason": "already_authenticated"}
    email, password = _cassette_auth_credentials()
    if not email or not password:
        raise BrowserAuthError(
            "cassette_auth_missing_credentials",
            "Cassette authentication is required but CASSETTE_AUTH_EMAIL/CASSETTE_AUTH_PASSWORD are not configured.",
        )
    _switch_to_cassette_login_form(page)
    login_deadline = time.monotonic() + 5
    while time.monotonic() < login_deadline:
        state = _cassette_auth_element_state(page)
        if state.get("login_email_visible") and state.get("password_visible"):
            break
        if state.get("signup_email_visible"):
            _switch_to_cassette_login_form(page)
        time.sleep(0.1)
    email_input = _first_visible_locator(
        page,
        (
            "#agent-auth-email-login",
            "input[type='email'][autocomplete='email']",
            "input[type='email']",
        ),
        timeout_ms=5000,
    )
    password_input = _first_visible_locator(
        page,
        ("#agent-auth-password", "input[type='password']"),
        timeout_ms=5000,
    )
    if not email_input or not password_input:
        raise BrowserAuthError("cassette_auth_form_missing", "Cassette authentication form was not found.")
    try:
        email_input.fill(email)
        password_input.fill(password)
        password_input.press("Enter")
    except Exception as exc:
        raise BrowserAuthError(
            "cassette_auth_input_failed", f"Cassette authentication input failed: {type(exc).__name__}"
        ) from exc
    deadline = time.monotonic() + max(5.0, timeout_ms / 1000)
    while time.monotonic() < deadline:
        if not _page_requires_auth(page):
            return {"status": "authenticated"}
        time.sleep(0.5)
    raise BrowserAuthError("cassette_auth_failed", "Cassette authentication did not complete before timeout.")


def _agent_ui_ready_state(page: Any) -> dict[str, Any]:
    try:
        return page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const bodyText = (document.body?.innerText || "").replace(/\\s+/g, " ").trim();
                const authVisible = Array.from(document.querySelectorAll(
                    "#agent-auth-password,#agent-auth-email-login,#agent-auth-email,input[type='password']"
                )).some(visible);
                const selectors = [
                    "[data-testid='agent-upload-status']",
                    "[data-testid='agent-export-button']",
                    "[data-testid='agent-chat-input']",
                    "[data-testid='chat-input']",
                    "textarea[placeholder*='Describe']",
                    "textarea[placeholder*='描述']",
                    "textarea",
                    "[role='textbox']",
                    "[contenteditable='true']",
                    "button[title='AI 语言']",
                    "button[title='Agent language']",
                    "button[aria-label*='language' i]",
                    "button[aria-label*='语言']"
                ];
                const matches = selectors.filter((selector) => {
                    try {
                        return Array.from(document.querySelectorAll(selector)).some(visible);
                    } catch (_) {
                        return false;
                    }
                });
                const fileInputPresent = !!document.querySelector("[data-testid='agent-file-input'],input[type='file']");
                const hydrated = bodyText.length > 0 && !authVisible && matches.length > 0;
                return {
                    ready: hydrated,
                    body_length: bodyText.length,
                    auth_visible: authVisible,
                    matches,
                    file_input_present: fileInputPresent,
                };
            }"""
        )
    except Exception as exc:
        return {"ready": False, "error": type(exc).__name__}


def _wait_for_initial_auth_or_agent_ui(page: Any, timeout_ms: int = 10000) -> str:
    deadline = time.monotonic() + max(1.0, timeout_ms / 1000)
    while time.monotonic() < deadline:
        if _page_requires_auth(page):
            return "requires_auth"
        state = _agent_ui_ready_state(page)
        if state.get("auth_visible"):
            return "requires_auth"
        if state.get("ready"):
            return "ready"
        time.sleep(0.25)
    return "unknown"


def _wait_for_agent_ui_ready(page: Any, job_id: str = "", timeout_ms: int = 60000) -> dict[str, Any]:
    if str(getattr(page, "url", "") or "").startswith("file:"):
        timeout_ms = min(timeout_ms, 10000)
    start = time.monotonic()
    if job_id:
        _record_operation_progress(
            job_id,
            "ui_ready",
            "Cassette agent UI readiness check started.",
            operation_status="started",
            stage_elapsed_sec=0.0,
        )
    deadline = start + max(5.0, timeout_ms / 1000)
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=1000)
        except Exception:
            pass
        last_state = _agent_ui_ready_state(page)
        if last_state.get("ready"):
            elapsed = time.monotonic() - start
            if job_id:
                _record_operation_progress(
                    job_id,
                    "ui_ready",
                    "Cassette agent UI is ready for browser automation.",
                    operation_status="succeeded",
                    stage_elapsed_sec=elapsed,
                )
            return last_state
        time.sleep(0.25)
    if job_id:
        _record_operation_progress(
            job_id,
            "ui_ready",
            "Cassette page loaded but agent UI was not ready before timeout.",
            operation_status="failed",
            stage_elapsed_sec=time.monotonic() - start,
        )
    details = {
        "body_length": last_state.get("body_length"),
        "matches": last_state.get("matches") or [],
        "file_input_present": bool(last_state.get("file_input_present")),
        "auth_visible": bool(last_state.get("auth_visible")),
        "error": last_state.get("error"),
    }
    raise BrowserUIReadyError("cassette_ui_not_ready", f"Cassette agent UI was not ready before automation: {details}")


def _asset_fingerprint(asset_paths: list[str]) -> tuple[str, ...]:
    return tuple(_asset_file_fingerprint(path) for path in asset_paths)


def _asset_file_fingerprint(path: str) -> str:
    try:
        resolved = Path(path).resolve()
        stat = resolved.stat()
        return f"{resolved}:{stat.st_size}:{stat.st_mtime_ns}"
    except Exception:
        return str(path)


def _uploaded_asset_fingerprints(record: dict[str, Any]) -> set[str]:
    uploaded = set(str(item) for item in (record.get("uploaded_asset_fingerprints") or ()) if str(item))
    if not uploaded:
        uploaded.update(str(item) for item in (record.get("asset_fingerprint") or ()) if str(item))
    return uploaded


def _asset_paths_needing_upload(record: dict[str, Any], asset_paths: list[str]) -> list[str]:
    uploaded = _uploaded_asset_fingerprints(record)
    result: list[str] = []
    seen: set[str] = set()
    for path in asset_paths:
        fingerprint = _asset_file_fingerprint(path)
        if fingerprint in uploaded or fingerprint in seen:
            continue
        result.append(path)
        seen.add(fingerprint)
    return result


def _mark_uploaded_assets(record: dict[str, Any], upload_paths: list[str], all_asset_paths: list[str]) -> None:
    uploaded = _uploaded_asset_fingerprints(record)
    uploaded.update(_asset_fingerprint(upload_paths))
    record["uploaded_asset_fingerprints"] = tuple(sorted(uploaded))
    record["asset_fingerprint"] = _asset_fingerprint(all_asset_paths)


def _close_browser_record(record: dict[str, Any]) -> None:
    for key in ("context", "browser"):
        target = record.get(key)
        if target is None:
            continue
        try:
            target.close()
        except Exception:
            pass
    isolated_playwright = record.get("isolated_playwright")
    if isolated_playwright is not None:
        try:
            isolated_playwright.stop()
        except Exception:
            pass


def close_browser_sessions(session_key: str | None = None) -> bool:
    global _PLAYWRIGHT, _PLAYWRIGHT_THREAD_ID
    closed = False
    if session_key:
        record = _BROWSER_SESSIONS.pop(session_key, None)
        if record:
            _close_browser_record(record)
            closed = True
        return closed
    for key in list(_BROWSER_SESSIONS):
        record = _BROWSER_SESSIONS.pop(key, None)
        if record:
            _close_browser_record(record)
            closed = True
    if _PLAYWRIGHT is not None:
        try:
            _PLAYWRIGHT.stop()
        except Exception:
            pass
        _PLAYWRIGHT = None
        _PLAYWRIGHT_THREAD_ID = None
        closed = True
    return closed


atexit.register(close_browser_sessions)


def _init_browser_worker() -> None:
    global _BROWSER_WORKER_THREAD_ID
    _BROWSER_WORKER_THREAD_ID = threading.get_ident()


def _browser_worker() -> ThreadPoolExecutor:
    global _BROWSER_WORKER
    with _BROWSER_WORKER_LOCK:
        if _BROWSER_WORKER is None:
            _BROWSER_WORKER = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="cassette-browser",
                initializer=_init_browser_worker,
            )
        return _BROWSER_WORKER


def _init_model_options_worker() -> None:
    global _MODEL_OPTIONS_WORKER_THREAD_ID
    _MODEL_OPTIONS_WORKER_THREAD_ID = threading.get_ident()


def _model_options_worker() -> ThreadPoolExecutor:
    global _MODEL_OPTIONS_WORKER
    with _MODEL_OPTIONS_WORKER_LOCK:
        if _MODEL_OPTIONS_WORKER is None:
            _MODEL_OPTIONS_WORKER = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="cassette-model-options",
                initializer=_init_model_options_worker,
            )
        return _MODEL_OPTIONS_WORKER


def _in_browser_worker() -> bool:
    return _BROWSER_WORKER_THREAD_ID == threading.get_ident()


def _in_model_options_worker() -> bool:
    return _MODEL_OPTIONS_WORKER_THREAD_ID == threading.get_ident()


def run_cassette_browser_job_threaded(job: dict) -> dict:
    if not _browser_worker_enabled() or _in_browser_worker():
        return run_cassette_browser_job(job)
    return _browser_worker().submit(run_cassette_browser_job, job).result()


def resume_cassette_browser_job_threaded(job: dict, response: str) -> dict:
    if not _browser_worker_enabled() or _in_browser_worker():
        return resume_cassette_browser_job(job, response)
    return _browser_worker().submit(resume_cassette_browser_job, job, response).result()


def has_live_browser_session_threaded(job: dict) -> bool:
    if not _browser_worker_enabled() or _in_browser_worker():
        return has_live_browser_session(job)
    return bool(_browser_worker().submit(has_live_browser_session, job).result())


def has_live_browser_session(job: dict) -> bool:
    record = _BROWSER_SESSIONS.get(_browser_session_key(job))
    page = record.get("page") if isinstance(record, dict) else None
    return bool(page and not _page_is_closed(page))


def resume_cassette_browser_job(job: dict, response: str) -> dict:
    """Continue a question-paused browser job in the same process and browser session."""
    if not has_live_browser_session(job):
        return {
            "status": "failed",
            "outputs": job.get("outputs") or [],
            "questions": job.get("questions") or [],
            "errors": [
                {
                    "code": "browser_session_lost",
                    "message": (
                        "The browser transport can resume only while the same local MCP process "
                        "keeps its live browser session. Start a new browser job after a host restart."
                    ),
                }
            ],
            "quality": {
                **(job.get("quality") or {}),
                "completion_observed": False,
                "export_completed": False,
                "risk": "high",
            },
            "final_screenshot": None,
        }
    answer = str(response or "").strip()
    resumed = dict(job)
    resumed["prompt"] = answer
    resumed["chat_message"] = answer
    resumed["instruction"] = answer
    return run_cassette_browser_job(resumed)


def close_browser_sessions_threaded(session_key: str | None = None, timeout_sec: float | None = None) -> bool:
    if not _browser_worker_enabled() or _in_browser_worker() or _BROWSER_WORKER is None:
        return close_browser_sessions(session_key)
    future = _browser_worker().submit(close_browser_sessions, session_key)
    try:
        return bool(future.result(timeout=timeout_sec))
    except FuturesTimeoutError:
        future.cancel()
        return False


def _shutdown_browser_worker() -> None:
    global _BROWSER_WORKER, _BROWSER_WORKER_THREAD_ID
    executor = _BROWSER_WORKER
    if executor is None:
        return
    try:
        executor.submit(close_browser_sessions).result(timeout=10)
    except Exception:
        pass
    executor.shutdown(wait=False)
    _BROWSER_WORKER = None
    _BROWSER_WORKER_THREAD_ID = None


atexit.register(_shutdown_browser_worker)


def abandon_browser_worker() -> bool:
    global _BROWSER_WORKER, _BROWSER_WORKER_THREAD_ID
    with _BROWSER_WORKER_LOCK:
        executor = _BROWSER_WORKER
        if executor is None:
            return False
        _BROWSER_WORKER = None
        _BROWSER_WORKER_THREAD_ID = None
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        executor.shutdown(wait=False)
    return True


def _shutdown_model_options_worker() -> None:
    global _MODEL_OPTIONS_WORKER, _MODEL_OPTIONS_WORKER_THREAD_ID
    executor = _MODEL_OPTIONS_WORKER
    if executor is None:
        return
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        executor.shutdown(wait=False)
    _MODEL_OPTIONS_WORKER = None
    _MODEL_OPTIONS_WORKER_THREAD_ID = None


atexit.register(_shutdown_model_options_worker)


def _playwright() -> Any:
    global _PLAYWRIGHT, _PLAYWRIGHT_THREAD_ID
    current_thread_id = threading.get_ident()
    if _PLAYWRIGHT is not None and _PLAYWRIGHT_THREAD_ID != current_thread_id:
        close_browser_sessions()
    if _PLAYWRIGHT is None:
        from playwright.sync_api import sync_playwright

        _PLAYWRIGHT = sync_playwright().start()
        _PLAYWRIGHT_THREAD_ID = current_thread_id
    return _PLAYWRIGHT


def _page_is_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return True


def _connectivity_log_summary(connectivity: dict[str, Any]) -> str:
    if connectivity.get("ok"):
        status = str(connectivity.get("status") or "reachable")
        http_status = connectivity.get("http_status")
        if http_status:
            return f"Cassette connectivity check {status} (HTTP {http_status})."
        reason = connectivity.get("reason")
        if reason:
            return f"Cassette connectivity check {status}: {reason}."
        return f"Cassette connectivity check {status}."
    code = str(connectivity.get("code") or "cassette_unreachable")
    http_status = connectivity.get("http_status")
    if http_status:
        return f"Cassette connectivity check failed: {code} (HTTP {http_status})."
    detail_type = ((connectivity.get("details") or {}).get("type") or "").strip()
    if detail_type:
        return f"Cassette connectivity check failed: {code} ({detail_type})."
    return f"Cassette connectivity check failed: {code}."


def _auth_log_summary(auth: dict[str, Any]) -> str:
    status = str(auth.get("status") or "unknown")
    if status == "authenticated":
        return "Cassette authentication completed."
    if status == "skipped":
        reason = str(auth.get("reason") or "not_required")
        return f"Cassette authentication skipped: {reason}."
    return f"Cassette authentication status: {status}."


def _record_operation_progress(
    job_id: str,
    stage: str,
    summary: str,
    *,
    operation_status: str,
    stage_elapsed_sec: float | None = None,
) -> None:
    try:
        job = jobs.load_job(job_id)
        browser_events = list(job.get("browser_events") or [])[-49:]
        event: dict[str, Any] = {
            "at": jobs.now_iso(),
            "stage": stage,
            "operation_status": operation_status,
            "summary": _summarize_page_state(summary),
        }
        if stage_elapsed_sec is not None:
            event["stage_elapsed_sec"] = round(max(0.0, stage_elapsed_sec), 1)
        browser_events.append(event)
        jobs.update_job(job_id, browser_events=browser_events)
    except Exception:
        pass
    _record_stage_progress(
        job_id,
        summary,
        [],
        status="running",
        stage=stage,
        stage_elapsed_sec=stage_elapsed_sec,
        operation_status=operation_status,
    )


def _ensure_cassette_authenticated_with_progress(
    page: Any,
    job_id: str,
    timeout_ms: int,
    *,
    started_summary: str = "Cassette authentication check started.",
) -> dict[str, Any]:
    auth_start = time.monotonic()
    if job_id:
        _record_operation_progress(
            job_id,
            "authentication",
            started_summary,
            operation_status="started",
            stage_elapsed_sec=0.0,
        )
    try:
        auth = _ensure_cassette_authenticated(page, timeout_ms=timeout_ms)
    except BrowserAuthError as exc:
        if job_id:
            _record_operation_progress(
                job_id,
                "authentication",
                f"Cassette authentication failed: {exc.code}.",
                operation_status="failed",
                stage_elapsed_sec=time.monotonic() - auth_start,
            )
        raise
    if job_id:
        _record_operation_progress(
            job_id,
            "authentication",
            _auth_log_summary(auth),
            operation_status=str(auth.get("status") or "unknown"),
            stage_elapsed_sec=time.monotonic() - auth_start,
        )
    return auth


def _prepare_agent_page_for_automation(
    page: Any, job_id: str, timeout_ms: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    auth = _ensure_cassette_authenticated_with_progress(page, job_id, timeout_ms=min(timeout_ms, 30000))
    try:
        ui_ready = _wait_for_agent_ui_ready(page, job_id=job_id, timeout_ms=min(timeout_ms, 60000))
        return auth, ui_ready
    except BrowserUIReadyError:
        state = _agent_ui_ready_state(page)
        if not state.get("auth_visible"):
            raise
        auth = _ensure_cassette_authenticated_with_progress(
            page,
            job_id,
            timeout_ms=min(timeout_ms, 30000),
            started_summary="Cassette authentication recheck started after the access form appeared.",
        )
        ui_ready = _wait_for_agent_ui_ready(page, job_id=job_id, timeout_ms=min(timeout_ms, 60000))
        return auth, ui_ready


def _new_browser_record(
    job: dict,
    headless: bool,
    launch_args: list[str],
    url: str,
    timeout_ms: int,
    *,
    isolated_playwright: bool = False,
) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    connectivity_start = time.monotonic()
    if job_id:
        _record_operation_progress(
            job_id,
            "connectivity",
            "Cassette connectivity check started.",
            operation_status="started",
            stage_elapsed_sec=0.0,
        )
    connectivity = check_cassette_connectivity(url)
    connectivity_elapsed = time.monotonic() - connectivity_start
    if job_id:
        _record_operation_progress(
            job_id,
            "connectivity",
            _connectivity_log_summary(connectivity),
            operation_status=str(connectivity.get("status") or ("ok" if connectivity.get("ok") else "failed")),
            stage_elapsed_sec=connectivity_elapsed,
        )
    if not connectivity.get("ok"):
        code = str(connectivity.get("code") or "cassette_unreachable")
        raise BrowserConnectivityError(code, "Cassette is not reachable; check network settings.")
    if isolated_playwright:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
    else:
        pw = _playwright()
    browser = None
    try:
        browser = pw.chromium.launch(headless=headless, args=launch_args)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
    except Exception as exc:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if isolated_playwright:
            try:
                pw.stop()
            except Exception:
                pass
        raise BrowserLaunchError(str(exc)) from exc
    record = {
        "browser": browser,
        "context": context,
        "page": page,
        "url": url,
        "owner_thread_id": threading.get_ident(),
        "asset_fingerprint": (),
        "uploaded_asset_fingerprints": (),
        "console_messages": [],
        "connectivity": connectivity,
        "isolated_playwright": pw if isolated_playwright else None,
    }
    page.on("console", lambda msg: record["console_messages"].append(f"{msg.type}: {msg.text}"[:500]))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=min(timeout_ms, 60000))
        record["auth"], record["ui_ready"] = _prepare_agent_page_for_automation(page, job_id, timeout_ms)
    except Exception as exc:
        _close_browser_record(record)
        if isinstance(exc, (BrowserAuthError, BrowserUIReadyError)):
            raise
        raise BrowserPageLoadError(str(exc)) from exc
    return record


def _browser_record(
    job: dict, headless: bool, launch_args: list[str], url: str, timeout_ms: int
) -> tuple[dict[str, Any], bool]:
    if not _browser_reuse_enabled():
        return _new_browser_record(job, headless, launch_args, url, timeout_ms), True
    key = _browser_session_key(job)
    record = _BROWSER_SESSIONS.get(key)
    if record and record.get("owner_thread_id") != threading.get_ident():
        close_browser_sessions()
        record = None
    if record and (record.get("url") != url or _page_is_closed(record.get("page"))):
        close_browser_sessions(key)
        record = None
    if record is None:
        record = _new_browser_record(job, headless, launch_args, url, timeout_ms)
        _BROWSER_SESSIONS[key] = record
        return record, True
    record["console_messages"] = []
    return record, False


def check_playwright() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except Exception:
        return False


def _selector(job: dict, key: str, env: str, default: str) -> str:
    return (job.get("selectors") or {}).get(key) or os.getenv(env, default)


def _capture(page: Any, path: Path) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        return None


def _screenshot(page: Any, job_id: str) -> str | None:
    path = Path(os.getenv("CASSETTE_ASSET_ROOT", str(get_asset_root()))) / "screenshots" / f"{job_id}_final.png"
    return _capture(page, path)


def _progress_screenshot(page: Any, job_id: str) -> str | None:
    timestamp = jobs.now_iso().replace(":", "").replace("-", "")
    path = (
        Path(os.getenv("CASSETTE_ASSET_ROOT", str(get_asset_root())))
        / "screenshots"
        / f"{job_id}_progress_{timestamp}.png"
    )
    return _capture(page, path)


def _exports_dir(job_id: str) -> Path:
    path = Path(os.getenv("CASSETTE_ASSET_ROOT", str(get_asset_root()))) / "exports" / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(name: str | None, default: str = "cassette_export.mp4") -> str:
    value = (name or "").strip() or default
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(value).name).strip("._")
    if not value:
        value = default
    if "." not in value:
        value = f"{value}.mp4"
    return value


def _visible(page: Any, selector: str, timeout: int = 500) -> bool:
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def _selector_visible_variants(selector: str) -> list[str]:
    variants: list[str] = []
    for part in (selector or "").split(","):
        value = part.strip()
        if not value:
            continue
        variants.append(f"{value}:visible")
        variants.append(value)
    return variants


def _chat_input_candidates(selector: str) -> list[str]:
    candidates = _selector_visible_variants(selector)
    candidates.extend(
        [
            "[data-testid^='chat-input-textarea-']:visible",
            "[data-testid='agent-chat-input']:visible",
            "[data-testid='chat-input']:visible",
            "textarea[placeholder*='Describe']:visible",
            "textarea[placeholder*='描述']:visible",
            "textarea:visible",
            "[role='textbox']:visible",
            "[contenteditable='true']:visible",
            "input[type='text']:visible",
        ]
    )
    seen: set[str] = set()
    unique: list[str] = []
    for item in candidates:
        if item and item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def _set_chat_input_with_js(page: Any, prompt: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """(value) => {
                const selectors = [
                    "[data-testid^='chat-input-textarea-']",
                    "[data-testid='agent-chat-input']",
                    "[data-testid='chat-input']",
                    "textarea[placeholder*='Describe']",
                    "textarea[placeholder*='描述']",
                    "textarea",
                    "[role='textbox']",
                    "[contenteditable='true']",
                    "input[type='text']"
                ];
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const editable = selectors
                    .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
                    .find((el) => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true');
                if (!editable) return false;
                editable.focus();
                const tag = editable.tagName.toLowerCase();
                if (tag === 'textarea' || tag === 'input') {
                    const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(editable, value);
                    else editable.value = value;
                } else {
                    editable.textContent = value;
                }
                editable.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
                editable.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }""",
                prompt,
            )
        )
    except Exception:
        return False


def _fill_prompt(page: Any, selector: str, prompt: str) -> None:
    last_error: Exception | None = None
    for candidate in _chat_input_candidates(selector):
        try:
            loc = page.locator(candidate).first
            loc.wait_for(state="visible", timeout=2000)
            loc.fill(prompt, timeout=3000)
            return
        except Exception as exc:
            last_error = exc
        try:
            loc = page.locator(candidate).first
            loc.wait_for(state="visible", timeout=1000)
            loc.click(timeout=1000)
            page.keyboard.press("Control+A")
            page.keyboard.insert_text(prompt)
            return
        except Exception as exc:
            last_error = exc
    if _set_chat_input_with_js(page, prompt):
        return
    raise RuntimeError("Cassette chat input was not available") from last_error


def _collect_outputs(page: Any, selector: str) -> list[dict]:
    outputs: list[dict] = []
    try:
        links = page.locator(selector)
        count = min(links.count(), 20)
        for i in range(count):
            link = links.nth(i)
            href = link.get_attribute("href")
            if href:
                outputs.append(
                    {
                        "text": (link.inner_text(timeout=500) or "").strip()[:200],
                        "href": href,
                        "download": link.get_attribute("download") or "",
                    }
                )
    except Exception:
        pass
    return outputs


def _output_from_download(download: Any, job_id: str) -> dict:
    filename = _safe_filename(getattr(download, "suggested_filename", None))
    target = _exports_dir(job_id) / filename
    download.save_as(str(target))
    return {
        "text": filename,
        "href": getattr(download, "url", "") or "",
        "download": filename,
        "local_path": str(target),
        "kind": "video",
    }


def _output_from_response(page: Any, href: str, job_id: str, filename_hint: str | None = None) -> dict:
    response = page.context.request.get(href, timeout=60000)
    if not response.ok:
        raise ExportError("export_download_failed", f"Cassette export download returned HTTP {response.status}")
    parsed_name = Path(unquote(urlparse(href).path)).name
    filename = _safe_filename(filename_hint or parsed_name)
    target = _exports_dir(job_id) / filename
    target.write_bytes(response.body())
    return {
        "text": filename,
        "href": href,
        "download": filename,
        "local_path": str(target),
        "kind": "video",
    }


def _export_stage_text(page: Any) -> str:
    parts: list[str] = []
    for selector in ("[data-testid='export-progress']", "[data-testid='export-stage']"):
        try:
            text = page.locator(selector).first.inner_text(timeout=500).strip()
            if text:
                parts.append(text)
        except Exception:
            pass
    return " ".join(parts)


def _visible_control_states(page: Any) -> list[dict[str, Any]]:
    try:
        return page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                return Array.from(document.querySelectorAll('button,[role="button"],a[href]')).map((el, index) => ({
                    index,
                    tag: el.tagName.toLowerCase(),
                    testid: el.getAttribute('data-testid') || '',
                    role: el.getAttribute('role') || '',
                    aria: el.getAttribute('aria-label') || '',
                    title: el.getAttribute('title') || '',
                    text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 160),
                    href: el.getAttribute('href') || '',
                    visible: visible(el),
                    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
                })).filter((item) => item.visible);
            }"""
        )
    except Exception:
        return []


def _control_label(control: dict[str, Any]) -> str:
    return " ".join(str(control.get(key) or "") for key in ("testid", "text", "aria", "title", "href")).strip().lower()


def _label_has_term(label: str, term: str) -> bool:
    if not term:
        return False
    if re.search(r"[\u4e00-\u9fff]", term):
        return term in label
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", label))


def _is_routine_control(control: dict[str, Any]) -> bool:
    if control.get("disabled"):
        return False
    label = _control_label(control)
    if not label:
        return False
    excluded = (
        "export",
        "upload",
        "choose",
        "new chat",
        "chat history",
        "model",
        "mode",
        "prompt",
        "enhance",
        "tools",
        "more tools",
        "cancel",
        "delete",
        "stop",
        "导出",
        "上传",
        "选择",
        "新建",
        "新对话",
        "对话历史",
        "更多工具",
        "取消",
        "删除",
        "停止",
    )
    if any(_label_has_term(label, term) for term in excluded):
        return False
    allowed = (
        "approve",
        "approved",
        "proceed",
        "continue",
        "run plan",
        "run",
        "execute",
        "start",
        "accept",
        "apply",
        "ok",
        "批准",
        "确认",
        "继续",
        "执行",
        "开始",
        "同意",
        "应用",
    )
    return any(_label_has_term(label, term) for term in allowed)


def _export_control_state(controls: list[dict[str, Any]]) -> dict[str, Any]:
    for control in controls:
        label = _control_label(control)
        if control.get("testid") == "agent-export-button" or _label_has_term(label, "export") or "导出" in label:
            return {
                "visible": bool(control.get("visible")),
                "enabled": bool(control.get("visible")) and not bool(control.get("disabled")),
                "label": label[:200],
            }
    return {"visible": False, "enabled": False, "label": ""}


def _stop_control_state(controls: list[dict[str, Any]]) -> dict[str, Any]:
    stop_terms = ("stop", "abort", "停止", "终止", "中止")
    cancel_terms = ("cancel", "取消")
    excluded = ("export", "导出", "new chat", "新对话", "delete", "删除")
    for control in controls:
        if control.get("disabled"):
            continue
        label = _control_label(control)
        if not label or any(_label_has_term(label, term) for term in excluded):
            continue
        if any(_label_has_term(label, term) for term in (*stop_terms, *cancel_terms)):
            return {
                "visible": bool(control.get("visible")),
                "enabled": bool(control.get("visible")) and not bool(control.get("disabled")),
                "label": label[:200],
            }
    return {"visible": False, "enabled": False, "label": ""}


def _checklist_progress(text: str) -> dict[str, int] | None:
    matches = list(
        re.finditer(
            r"(?:task checklist|任务清单|任务列表|检查清单|执行清单|工作清单)\s+(\d+)\s*/\s*(\d+)",
            text or "",
            flags=re.IGNORECASE,
        )
    )
    if not matches:
        return None
    match = matches[-1]
    done = int(match.group(1))
    total = int(match.group(2))
    return {"done": done, "total": total}


def _routine_phrase(text: str) -> bool:
    value = (text or "").lower()
    terms = (
        "execution plan",
        "edit plan",
        "approve plan",
        "approval",
        "ready to proceed",
        "shall i proceed",
        "please confirm",
        "confirm to continue",
        "shall i continue",
        "continue stop",
        "执行方案",
        "执行计划",
        "编辑计划",
        "批准方案",
        "批准计划",
        "批准",
        "请批准",
        "请确认",
        "待确认",
        "需要确认",
        "确认后",
        "是否继续",
        "是否执行",
        "继续执行",
        "开始执行",
    )
    return any(term in value for term in terms)


def _completion_phrase(text: str) -> bool:
    value = (text or "").lower()
    terms = (
        "已完成",
        "任务完成",
        "剪辑完成",
        "编辑完成",
        "视频已完成",
        "可以导出",
        "可导出",
        "准备导出",
        "导出已准备",
        "已生成",
        "export ready",
        "ready to export",
        "completed",
        "done!",
        "the edit is complete",
        "finished",
    )
    return any(term in value for term in terms)


def _completion_denial_phrase(text: str) -> bool:
    value = " ".join((text or "").split())
    if not value:
        return False
    if _completion_phrase(value):
        strong_patterns = (
            r"\b(?:not complete|not completed|not finished|incomplete|no completed edit|no finished edit)\b",
            r"\b(?:cannot|can't|could not|couldn't|unable to|failed to)\b.{0,80}\b(?:complete|finish|export|render)\b",
            r"(?:剪辑|编辑|任务|视频)[^。；;.!！？]{0,20}(?:未完成|没有完成|尚未完成|还没完成|失败)",
            r"(?:无法|不能|未能)\s*完成(?:[。；;.!！？]|$)",
            r"(?:无法|不能|未能)[^。；;.!！？]{0,20}(?:完成(?:剪辑|编辑|任务|视频|处理)|导出|渲染)",
            r"(?:导出|渲染)[^。；;.!！？]{0,20}(?:失败|无法|不能|未能)",
        )
        return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in strong_patterns)
    patterns = (
        r"\b(?:cannot|can't|could not|couldn't|unable to|failed to|did not|didn't|have not|haven't)\b.{0,120}\b(?:complete|finish|edit|export|generate|create|process|render)\b",
        r"\b(?:not complete|not completed|not finished|incomplete|no completed edit|no finished edit)\b",
        r"(?:无法|不能|未能|没有|尚未|还没).{0,40}(?:完成|剪辑|编辑|导出|生成|处理)",
        r"(?:完成|剪辑|编辑|导出|生成|处理).{0,40}(?:失败|未完成|没有完成|无法完成|不能完成)",
    )
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


def _visible_diagnostic_text(page: Any) -> str:
    selector = (
        "[role='alert'],[role='status'],[aria-live='assertive'],[aria-live='polite'],"
        "[data-testid*='error'],[data-testid*='toast'],[data-testid*='notification'],"
        "[data-testid*='status']"
    )
    try:
        return (
            page.evaluate(
                """(selector) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const values = [];
                const seen = new Set();
                for (const el of Array.from(document.querySelectorAll(selector))) {
                    if (!visible(el)) continue;
                    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!text || seen.has(text)) continue;
                    seen.add(text);
                    values.push(text.slice(0, 400));
                }
                return values.join('\\n');
            }""",
                selector,
            )
            or ""
        )
    except Exception:
        return ""


_CASSETTE_HARD_ERROR_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\brequest failed\.?(?:\s+please try again\.?)?", "cassette_request_failed", "request_failed"),
    (r"\bfailed to fetch\b", "cassette_network_failed", "failed_to_fetch"),
    (r"\bnetwork request failed\b", "cassette_network_failed", "network_request_failed"),
    (r"\bnetwork error\b", "cassette_network_failed", "network_error"),
    (r"\binternal server error\b", "cassette_server_error", "internal_server_error"),
    (r"\bserver error\b", "cassette_server_error", "server_error"),
    (r"请求失败(?:，?请重试)?", "cassette_request_failed", "request_failed"),
    (r"网络请求失败|网络错误", "cassette_network_failed", "network_error"),
    (r"服务器错误|服务端错误", "cassette_server_error", "server_error"),
)


def _cassette_hard_error_match(text: str) -> dict[str, str] | None:
    value = text or ""
    if not value:
        return None
    messages = {
        "cassette_request_failed": "Cassette reported request failed; please retry after the page/session is reset.",
        "cassette_network_failed": "Cassette reported a network request failure; please retry after connectivity or service recovery.",
        "cassette_server_error": "Cassette reported a server error; please retry after the service recovers.",
    }
    for pattern, code, matched in _CASSETTE_HARD_ERROR_PATTERNS:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        return {"code": code, "message": messages.get(code, code), "matched": matched}
    return None


def _cassette_hard_error(page_state: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        page_state.get("diagnostic_text") or "",
        page_state.get("assistant_text") or "",
    ]
    body = page_state.get("body") or ""
    body_error = _cassette_hard_error_match(body)
    for text in candidates:
        error = _cassette_hard_error_match(text)
        if error:
            return {**error, "details": {"source": "cassette_page"}}
    body_context = body.lower()
    if body_error and (
        re.search(r"please try again|请重试", body, flags=re.IGNORECASE)
        or page_state.get("assistant_checklist")
        or page_state.get("page_checklist")
        or "task checklist" in body_context
        or "任务清单" in body
    ):
        return {**body_error, "details": {"source": "cassette_page"}}
    return None


def _cassette_page_state(
    page: Any,
    body: str,
    assistant_text: str,
    outputs: list[dict],
    *,
    assistant_is_current: bool = True,
    current_response_observed: bool = True,
    page_completion_allowed: bool = True,
) -> dict[str, Any]:
    controls = _visible_control_states(page)
    assistant_checklist = _checklist_progress(assistant_text)
    page_checklist = _checklist_progress(body)
    routine_controls = [control for control in controls if _is_routine_control(control)]
    return {
        "assistant_text": assistant_text or "",
        "body": body or "",
        "diagnostic_text": _visible_diagnostic_text(page),
        "outputs": outputs,
        "controls": controls,
        "routine_controls": routine_controls,
        "export_control": _export_control_state(controls),
        "stop_control": _stop_control_state(controls),
        "assistant_checklist": assistant_checklist,
        "page_checklist": page_checklist,
        "assistant_routine_phrase": _routine_phrase(assistant_text),
        "assistant_completion_phrase": _completion_phrase(assistant_text),
        "assistant_completion_denial": _completion_denial_phrase(assistant_text),
        "page_completion_phrase": bool(page_completion_allowed) and _completion_phrase(body),
        "assistant_is_current": bool(assistant_is_current),
        "current_response_observed": bool(current_response_observed),
        "page_completion_allowed": bool(page_completion_allowed),
    }


def _page_state_indicates_routine_interaction(state: dict[str, Any]) -> bool:
    if state.get("routine_controls"):
        return True
    assistant_checklist = state.get("assistant_checklist") or {}
    if assistant_checklist.get("total", 0) > 0 and assistant_checklist.get("done", 0) < assistant_checklist.get(
        "total", 0
    ):
        return True
    page_checklist = state.get("page_checklist") or {}
    if (
        state.get("assistant_routine_phrase")
        and page_checklist.get("total", 0) > 0
        and page_checklist.get("done", 0) == 0
    ):
        return True
    return bool(state.get("assistant_routine_phrase"))


def _page_state_checklist_complete(state: dict[str, Any]) -> bool:
    page_completion_allowed = bool(state.get("page_completion_allowed", True))
    assistant_checklist = state.get("assistant_checklist") or {}
    page_checklist = state.get("page_checklist") or {}
    return (
        assistant_checklist.get("total", 0) > 0 and assistant_checklist.get("done") >= assistant_checklist.get("total")
    ) or (
        page_completion_allowed
        and page_checklist.get("total", 0) > 0
        and page_checklist.get("done") >= page_checklist.get("total")
    )


def _page_state_reports_incomplete(state: dict[str, Any]) -> bool:
    return (
        bool(state.get("current_response_observed", True))
        and bool(state.get("assistant_is_current", True))
        and bool(state.get("assistant_completion_denial"))
    )


def _page_state_requires_completion_review(state: dict[str, Any], export_required: bool = True) -> bool:
    if not export_required:
        return False
    if not state.get("current_response_observed", True):
        return False
    if _page_state_indicates_routine_interaction(state):
        return False
    stop_enabled = bool((state.get("stop_control") or {}).get("enabled"))
    if stop_enabled:
        return False
    current_assistant = bool(state.get("assistant_is_current", True))
    assistant_text = str(state.get("assistant_text") or "").strip()
    if not current_assistant or not assistant_text:
        return False
    if _page_state_reports_incomplete(state):
        return True
    if bool(state.get("assistant_completion_phrase")) or _page_state_checklist_complete(state):
        return False
    return True


def _page_state_indicates_complete(state: dict[str, Any], export_required: bool = True) -> bool:
    if not state.get("current_response_observed", True):
        return False
    if _page_state_indicates_routine_interaction(state):
        return False
    if _page_state_reports_incomplete(state):
        return False
    outputs = state.get("outputs") or []
    current_assistant = bool(state.get("assistant_is_current", True))
    page_completion_allowed = bool(state.get("page_completion_allowed", True))
    checklist_complete = _page_state_checklist_complete(state)
    explicit_complete = bool(state.get("assistant_completion_phrase")) or checklist_complete
    if outputs and (current_assistant or page_completion_allowed):
        return True if not export_required else explicit_complete
    if export_required:
        export_enabled = bool((state.get("export_control") or {}).get("enabled"))
        return export_enabled and explicit_complete
    return explicit_complete


def _click_export(page: Any) -> None:
    export_selector = os.getenv(
        "CASSETTE_EXPORT_SELECTOR",
        "[data-testid='agent-export-button'],button[aria-label='Export'],button[title='Export'],button:has-text('Export'),button:has-text('导出')",
    )
    try:
        page.locator(export_selector).first.click(timeout=10000)
    except Exception as exc:
        raise ExportError(
            "export_control_missing", "Cassette completed the edit but no export control was available"
        ) from exc

    default_confirm_selectors = (
        "[role='alertdialog'] [data-testid='export-confirm-submit']",
        "[role='dialog'] [data-testid='export-confirm-submit']",
        "[data-testid='export-confirm-submit']",
        "[role='alertdialog'] button:has-text('Export')",
        "[role='dialog'] button:has-text('Export')",
        "[role='alertdialog'] button:has-text('导出')",
        "[role='dialog'] button:has-text('导出')",
    )
    raw_confirm_selector = os.getenv("CASSETTE_EXPORT_CONFIRM_SELECTOR")
    confirm_selectors = (
        [item.strip() for item in raw_confirm_selector.split(",") if item.strip()]
        if raw_confirm_selector
        else list(default_confirm_selectors)
    )
    try:
        _click_first_visible(page, confirm_selectors, timeout=3000)
    except Exception:
        pass


def _click_first_visible(page: Any, selectors: list[str], timeout: int = 5000) -> None:
    deadline = time.monotonic() + timeout / 1000
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        for selector in selectors:
            try:
                controls = page.locator(selector)
                count = min(controls.count(), 20)
            except Exception as exc:
                last_error = exc
                continue
            for index in range(count):
                control = controls.nth(index)
                try:
                    if hasattr(control, "is_visible") and not control.is_visible():
                        continue
                    if hasattr(control, "is_enabled") and not control.is_enabled():
                        continue
                    control.click(timeout=1000)
                    return
                except Exception as exc:
                    last_error = exc
                    continue
        time.sleep(0.1)
    raise RuntimeError("No visible matching control found") from last_error


def _click_cassette_stop_control(page: Any) -> dict[str, Any]:
    selector = os.getenv("CASSETTE_STOP_SELECTOR", "").strip()
    if not selector:
        try:
            result = page.evaluate(
                """() => {
                    const words = ["stop", "cancel", "abort", "停止", "取消", "终止", "中止"];
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        if (style.visibility === "hidden" || style.display === "none") return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const labelFor = (el) => [
                        el.innerText,
                        el.getAttribute("aria-label"),
                        el.getAttribute("title"),
                        el.getAttribute("data-testid")
                    ].filter(Boolean).join(" ").trim();
                    const hasStopWord = (label) => words.some((word) => label.toLowerCase().includes(word));
                    const scoreFor = (el, label) => {
                        let score = 0;
                        const testId = (el.getAttribute("data-testid") || "").toLowerCase();
                        const aria = (el.getAttribute("aria-label") || "").toLowerCase();
                        const title = (el.getAttribute("title") || "").toLowerCase();
                        if (testId.includes("stop") || testId.includes("cancel")) score += 8;
                        if (aria.includes("stop") || aria.includes("cancel") || title.includes("stop") || title.includes("cancel")) score += 6;
                        if (label.includes("停止") || label.includes("取消") || label.toLowerCase().includes("stop") || label.toLowerCase().includes("cancel")) score += 4;
                        const form = el.closest("form");
                        if (form && form.querySelector("textarea,[contenteditable='true']")) score += 6;
                        if (el.closest("[data-testid*='composer'],[class*='composer'],[data-testid*='chat'],[class*='chat']")) score += 3;
                        return score;
                    };
                    const buttons = Array.from(document.querySelectorAll("button,[role='button']"))
                        .filter((el) => visible(el) && !el.disabled && el.getAttribute("aria-disabled") !== "true")
                        .map((el) => ({el, label: labelFor(el)}))
                        .filter((item) => hasStopWord(item.label));
                    if (!buttons.length) {
                        return {clicked: false, reason: "not_visible"};
                    }
                    buttons.sort((a, b) => scoreFor(b.el, b.label) - scoreFor(a.el, a.label));
                    buttons[0].el.click();
                    return {clicked: true, label: buttons[0].label || "Stop"};
                }"""
            )
            if isinstance(result, dict) and result.get("clicked"):
                return result
        except Exception:
            pass
        try:
            result = page.evaluate(
                """() => {
                    const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (style.visibility === "hidden" || style.display === "none") return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const enabled = (el) => visible(el) && !el.disabled && el.getAttribute("aria-disabled") !== "true";
                    const bodyText = (document.body?.innerText || "").toLowerCase();
                    const busyTerms = [
                        "thinking", "working", "generating", "processing", "rendering", "analyzing",
                        "task checklist", "正在", "处理中", "进行中", "生成中", "思考", "工作中", "渲染中", "分析中", "任务清单"
                    ];
                    const graphStatus = Array.from(document.querySelectorAll("[data-testid='agent-graph-status']"))
                        .some((el) => visible(el) && (el.innerText || el.textContent || "").trim());
                    if (!graphStatus && !busyTerms.some((term) => bodyText.includes(term))) {
                        return {clicked: false, reason: "not_busy"};
                    }
                    const inputSelectors = [
                        "[data-testid='agent-chat-input']",
                        "[data-testid='chat-input']",
                        "textarea[placeholder*='Describe']",
                        "textarea[placeholder*='描述']",
                        "textarea",
                        "[role='textbox']",
                        "[contenteditable='true']",
                        "input[type='text']"
                    ];
                    const input = inputSelectors
                        .flatMap((item) => Array.from(document.querySelectorAll(item)))
                        .find((el) => visible(el));
                    if (!input) return {clicked: false, reason: "composer_missing"};
                    const labelFor = (el) => [
                        el.innerText || "",
                        el.textContent || "",
                        el.getAttribute("aria-label") || "",
                        el.getAttribute("title") || "",
                        el.getAttribute("data-testid") || ""
                    ].join(" ").replace(/\\s+/g, " ").trim().toLowerCase();
                    const excludedTerms = [
                        "export", "upload", "choose", "new chat", "history", "model", "language",
                        "settings", "tools", "tool", "attach", "attachment", "file", "add",
                        "导出", "上传", "选择", "新对话", "历史", "模型", "语言", "设置", "工具", "附件", "文件", "添加"
                    ];
                    const roots = [];
                    let cursor = input;
                    for (let depth = 0; cursor && depth < 8; depth += 1) {
                        roots.push(cursor);
                        if (cursor.tagName && cursor.tagName.toLowerCase() === "form") roots.unshift(cursor);
                        cursor = cursor.parentElement;
                    }
                    let best = null;
                    let bestScore = -9999;
                    const seen = new Set();
                    roots.forEach((root, rootIndex) => {
                        if (!root || !root.querySelectorAll) return;
                        const buttons = Array.from(root.querySelectorAll("button,[role='button']")).filter(enabled);
                        buttons.forEach((button, index) => {
                            if (seen.has(button)) return;
                            seen.add(button);
                            const label = labelFor(button);
                            if (label && excludedTerms.some((term) => label.includes(term))) return;
                            let score = index;
                            if (label.includes("stop") || label.includes("cancel") || label.includes("停止") || label.includes("取消")) score += 100;
                            if (!label || /^[^a-z0-9\\u4e00-\\u9fff]+$/i.test(label)) score += 20;
                            if (index === buttons.length - 1) score += 30;
                            score += Math.max(0, 12 - rootIndex);
                            if (score > bestScore) {
                                best = button;
                                bestScore = score;
                            }
                        });
                    });
                    if (!best) return {clicked: false, reason: "composer_stop_missing"};
                    const label = labelFor(best);
                    best.click();
                    const readableLabel = label && !/^[^a-z0-9\\u4e00-\\u9fff]+$/i.test(label) ? label : "composer icon stop control";
                    return {clicked: true, label: readableLabel};
                }"""
            )
            if isinstance(result, dict) and result.get("clicked"):
                return result
        except Exception:
            pass
        selector = (
            "[data-testid='agent-stop-button'],[data-testid='agent-cancel-button'],"
            "[data-testid='chat-stop-button'],[data-testid='chat-cancel-button'],"
            "form:has(textarea) button:has-text('Stop'),form:has(textarea) button:has-text('Cancel'),"
            "form:has(textarea) button:has-text('停止'),form:has(textarea) button:has-text('取消'),"
            "button[aria-label='Stop'],button[title='Stop'],button:has-text('Stop'),"
            "button[aria-label='停止'],button[title='停止'],button:has-text('停止'),"
            "button[aria-label='取消'],button[title='取消'],button:has-text('取消')"
        )
    try:
        controls = page.locator(selector)
        count = min(controls.count(), 20)
    except Exception as exc:
        return {"clicked": False, "reason": type(exc).__name__}
    last_error = ""
    for index in range(count):
        control = controls.nth(index)
        try:
            if hasattr(control, "is_visible") and not control.is_visible():
                continue
            if hasattr(control, "is_enabled") and not control.is_enabled():
                continue
            label = (
                control.inner_text(timeout=500)
                or control.get_attribute("aria-label")
                or control.get_attribute("title")
                or "Stop"
            ).strip()
            control.click(timeout=3000)
            return {"clicked": True, "label": label or "Stop"}
        except Exception as exc:
            last_error = type(exc).__name__
            continue
    return {"clicked": False, "reason": last_error or "not_visible"}


def _download_export(page: Any, job_id: str, output_selector: str, timeout_sec: int) -> list[dict]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    downloads: list[Any] = []
    page.on("download", lambda download: downloads.append(download))
    _click_export(page)
    start = time.monotonic()
    last_stage = ""
    last_progress_record = 0.0
    while time.monotonic() - start < timeout_sec:
        if jobs.is_cancel_requested(job_id):
            stop_result = _click_cassette_stop_control(page)
            _record_stage_progress(
                job_id,
                "Cassette export stop requested by /cut."
                + (f" Clicked stop control: {stop_result.get('label')}." if stop_result.get("clicked") else ""),
                [],
                status="running",
                stage="export",
                stage_elapsed_sec=time.monotonic() - start,
                operation_status="cancel_requested",
            )
            raise ExportError("export_cancelled", "Cassette export was cancelled")
        if downloads:
            return [_output_from_download(downloads.pop(0), job_id)]
        try:
            download = page.wait_for_event("download", timeout=1000)
            return [_output_from_download(download, job_id)]
        except PlaywrightTimeoutError:
            pass

        outputs = _collect_outputs(page, output_selector)
        downloadable = [
            item
            for item in outputs
            if item.get("href")
            and any(token in item["href"].lower() for token in ("download", "export", "r2.cloud", ".mp4"))
        ]
        if downloadable:
            href = downloadable[0]["href"]
            try:
                with page.expect_download(timeout=5000) as download_info:
                    page.locator(f'a[href="{href}"]').first.click(timeout=3000)
                return [_output_from_download(download_info.value, job_id)]
            except Exception as exc:
                try:
                    return [
                        _output_from_response(
                            page, href, job_id, downloadable[0].get("download") or downloadable[0].get("text")
                        )
                    ]
                except Exception as fetch_exc:
                    raise ExportError(
                        "export_download_failed",
                        f"Cassette export link could not be downloaded: {type(fetch_exc).__name__}",
                    ) from exc

        stage = _export_stage_text(page)
        if stage:
            now = time.monotonic()
            if stage != last_stage or now - last_progress_record >= int(
                os.getenv("CASSETTE_PROGRESS_INTERVAL_SEC", "30")
            ):
                _record_stage_progress(job_id, f"Export status: {stage}", outputs, status="running")
                last_progress_record = now
            last_stage = stage
        stage_lower = stage.lower()
        if any(term in stage_lower for term in ("failed", "error", "失败", "错误")):
            raise ExportError("export_failed", f"Cassette export failed: {stage}")
        time.sleep(1)
    detail = f" Last export stage: {last_stage}" if last_stage else ""
    raise ExportError("export_timeout", f"Timed out waiting for Cassette export download.{detail}")


def _upload_assets(page: Any, asset_paths: list[str], upload_selector: str) -> None:
    try:
        page.locator(upload_selector).first.set_input_files(asset_paths, timeout=10000)
        return
    except Exception:
        pass
    try:
        page.locator("input[type='file']").first.set_input_files(asset_paths, timeout=10000)
        return
    except Exception:
        pass
    raise RuntimeError("No programmatic file input found for Cassette asset upload")


def _upload_ready_expected_count(asset_paths: list[str]) -> int:
    # Cassette's ready counter reflects successfully processed files in the
    # latest upload batch, not the cumulative number of assets in the chat.
    return len([path for path in asset_paths if str(path or "").strip()])


def _upload_status_text(page: Any) -> str:
    try:
        return page.locator("[data-testid='agent-upload-status']").first.inner_text(timeout=500).strip()
    except Exception:
        return ""


def _int_upload_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _bool_upload_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "active"}:
        return True
    if normalized in {"0", "false", "no", "ready", "failed", "empty"}:
        return False
    return None


def _normalize_upload_state(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    total = _int_upload_value(raw.get("total"))
    ready = _int_upload_value(raw.get("completed"))
    if ready is None:
        ready = _int_upload_value(raw.get("ready"))
    failed = _int_upload_value(raw.get("failed"))
    active_count = _int_upload_value(raw.get("active"))
    active = _bool_upload_value(raw.get("isActive"))
    if active is None:
        active = active_count > 0 if active_count is not None else _bool_upload_value(raw.get("status"))
    if total is None and ready is None and failed is None and active is None:
        return None
    status_text = str(raw.get("statusText") or raw.get("text") or raw.get("status") or "").strip()
    return {
        "source": str(raw.get("source") or "unknown"),
        "total": total,
        "ready": ready,
        "failed": failed,
        "active": active,
        "status_text": status_text,
    }


def _structured_upload_state(page: Any) -> dict[str, Any] | None:
    try:
        raw = page.evaluate(
            """() => {
                const textOf = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\\s+/g, ' ').trim();
                const root = document.querySelector("[data-testid='agent-upload-strip']");
                if (root && root.dataset && root.dataset.cassetteUploadTotal !== undefined) {
                    return {
                        source: "upload-strip",
                        total: root.dataset.cassetteUploadTotal,
                        completed: root.dataset.cassetteUploadCompleted,
                        failed: root.dataset.cassetteUploadFailed,
                        active: root.dataset.cassetteUploadActive,
                        status: root.dataset.cassetteUploadState,
                        statusText: textOf(document.querySelector("[data-testid='agent-upload-status']")),
                    };
                }
                const bridge = window.__CASSETTE_E2E__;
                if (bridge && typeof bridge.getUploadState === "function") {
                    try {
                        const state = bridge.getUploadState();
                        if (state && typeof state === "object") return {...state, source: "bridge"};
                    } catch (_) {}
                }
                const progress = document.querySelector("[data-testid='agent-upload-progress']");
                if (progress) {
                    return {
                        source: "progressbar",
                        total: progress.getAttribute("aria-valuemax"),
                        completed: progress.getAttribute("aria-valuenow"),
                        statusText: textOf(document.querySelector("[data-testid='agent-upload-status']")),
                    };
                }
                return null;
            }"""
        )
    except Exception:
        return None
    return _normalize_upload_state(raw)


def _upload_timeout_sec(job: dict) -> int | None:
    raw = os.getenv("CASSETTE_UPLOAD_TIMEOUT_SEC")
    if raw:
        try:
            value = int(raw)
            return max(1, value) if value > 0 else None
        except ValueError:
            pass
    try:
        return max(1, int(job.get("timeout_sec") or 1800))
    except (TypeError, ValueError):
        return 1800


_UPLOAD_PROCESSING_TERMS = (
    "uploading",
    "processing",
    "analyzing",
    "transcoding",
    "active",
    "queued",
    "indexing",
    "上传中",
    "处理中",
    "分析中",
    "传输中",
    "转码中",
    "进行中",
)


def _upload_status_counts(text: str) -> tuple[int | None, int | None]:
    value = text or ""
    ready: int | None = None
    failed: int | None = None
    ready_patterns = (
        r"\b(\d+)\s+ready\b",
        r"(\d+)\s*个?\s*(?:已?就绪|准备就绪)",
        r"(?:ready|已?就绪|准备就绪)\s*[:：]\s*(\d+)\s*个?",
    )
    failed_patterns = (
        r"\b(\d+)\s+failed\b",
        r"(\d+)\s*个?\s*失败",
        r"(?:failed|失败)\s*[:：]\s*(\d+)\s*个?",
    )
    for pattern in ready_patterns:
        matches = re.findall(pattern, value, flags=re.IGNORECASE)
        if matches:
            ready = max(int(item) for item in matches)
    for pattern in failed_patterns:
        matches = re.findall(pattern, value, flags=re.IGNORECASE)
        if matches:
            failed = max(int(item) for item in matches)
    return ready, failed


def _upload_status_has_processing(text: str) -> bool:
    value = (text or "").lower()
    return any(term in value for term in _UPLOAD_PROCESSING_TERMS)


def _upload_status_has_failure(text: str) -> bool:
    ready, failed = _upload_status_counts(text)
    del ready
    if failed is not None:
        return failed > 0
    value = (text or "").lower()
    return any(term in value for term in ("failed", "error", "失败", "错误")) and not (
        "0 failed" in value or "0 个失败" in value
    )


def _upload_status_ready_for_expected(text: str, expected_count: int) -> bool:
    if expected_count <= 0:
        return False
    ready, failed = _upload_status_counts(text)
    if failed not in {None, 0}:
        return False
    if _upload_status_has_processing(text):
        return False
    if ready is not None:
        return ready >= expected_count
    return False


def _structured_upload_has_failure(state: dict[str, Any]) -> bool:
    failed = state.get("failed")
    if isinstance(failed, int):
        return failed > 0
    return _upload_status_has_failure(str(state.get("status_text") or ""))


def _structured_upload_ready_for_expected(state: dict[str, Any], expected_count: int) -> bool:
    if expected_count <= 0:
        return False
    if _structured_upload_has_failure(state):
        return False
    if state.get("active") is True:
        return False
    total = state.get("total")
    ready = state.get("ready")
    if isinstance(total, int) and total < expected_count:
        return False
    if isinstance(ready, int):
        return ready >= expected_count
    return False


def _agent_page_has_ready_assets(page: Any, expected_count: int) -> bool:
    if expected_count <= 0:
        return False
    structured = _structured_upload_state(page)
    if structured is not None:
        return _structured_upload_ready_for_expected(structured, expected_count)
    try:
        status = _upload_status_text(page)
    except Exception:
        return False
    if not status:
        return False
    if _upload_status_has_failure(status):
        return False
    return _upload_status_ready_for_expected(status, expected_count)


def _wait_for_agent_upload_ready(page: Any, job_id: str, expected_count: int, timeout_sec: int | None = None) -> str:
    start = time.monotonic()
    last_progress_at = 0.0
    last_body = ""
    last_status = ""
    while timeout_sec is None or time.monotonic() - start < timeout_sec:
        if jobs.is_cancel_requested(job_id):
            raise BrowserJobCancelled("Cassette job was cancelled while waiting for asset upload/analysis")
        body = page.locator("body").inner_text(timeout=1000)
        last_body = body
        structured = _structured_upload_state(page)
        status = _upload_status_text(page)
        status_or_state = str((structured or {}).get("status_text") or status)
        if status_or_state:
            last_status = status_or_state
        if structured is not None:
            if _structured_upload_has_failure(structured):
                raise RuntimeError(f"Cassette asset upload/analysis failed: {status_or_state or structured}")
            if _structured_upload_ready_for_expected(structured, expected_count):
                return body
        elif status:
            if _upload_status_has_failure(status):
                raise RuntimeError(f"Cassette asset upload/analysis failed: {status}")
            if _upload_status_ready_for_expected(status, expected_count):
                return body
        now = time.monotonic()
        if now - last_progress_at >= int(os.getenv("CASSETTE_PROGRESS_INTERVAL_SEC", "30")):
            _record_stage_progress(
                job_id,
                status_or_state or body,
                [],
                stage="upload",
                stage_elapsed_sec=now - start,
            )
            last_progress_at = now
        time.sleep(1)
    detail = _compact_summary_text(last_status or last_body, 300)
    message = "Timed out waiting for Cassette asset upload/analysis"
    if detail:
        message = f"{message}; last upload status: {detail}"
    if expected_count > 1:
        message = (
            f"{message}. Try retrying after refreshing Cassette, or upload fewer/lower-resolution assets in one batch."
        )
    raise BrowserUploadTimeoutError(message)


def _assistant_message_text(page: Any) -> str:
    try:
        messages = page.locator("[data-testid='chat-assistant-message']")
        count = messages.count()
        if count == 0:
            return ""
        return messages.nth(count - 1).inner_text(timeout=500).strip()
    except Exception:
        return ""


def _assistant_message_count(page: Any) -> int:
    try:
        return int(page.locator("[data-testid='chat-assistant-message']").count())
    except Exception:
        return 0


def _current_assistant_message_text(page: Any, baseline_count: int, baseline_text: str) -> tuple[str, bool]:
    try:
        messages = page.locator("[data-testid='chat-assistant-message']")
        count = messages.count()
        if count == 0:
            return "", False
        last_text = messages.nth(count - 1).inner_text(timeout=500).strip()
        if count > baseline_count:
            return last_text, True
        if last_text and last_text != baseline_text:
            return last_text, True
        return "", False
    except Exception:
        return "", False


def _chat_message_for_job(job: dict) -> str:
    return (job.get("chat_message") or job.get("instruction") or job.get("prompt") or "").strip()


def _compact_summary_text(raw: str, max_chars: int = 700) -> str:
    text = " ".join((raw or "").split())
    if len(text) <= max_chars:
        return text
    parts = re.split(r"(?<=[。.!?！？])\s+|\s+(?=✅|[-•]\s)", text)
    chunks: list[str] = []
    seen: set[str] = set()
    total = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        key = re.sub(r"[\W_]+", "", part.lower())[:120]
        if key in seen:
            continue
        seen.add(key)
        next_total = total + len(part) + (1 if chunks else 0)
        if next_total > max_chars:
            break
        chunks.append(part)
        total = next_total
    if chunks:
        return " ".join(chunks)
    return text[: max_chars - 1].rstrip() + "…"


def _summarize_page_state(body: str) -> str:
    text = " ".join((body or "").split())
    if not text:
        return "Cassette page loaded; no readable chat/status text yet."

    page_chrome = (
        "Choose files",
        "New chat",
        "Cassette AI",
        "Your creative assistant",
        "选择文件",
        "新对话",
        "你的创意助手",
    )
    if not any(marker in text for marker in page_chrome):
        return _compact_summary_text(text)

    upload_match = re.search(r"\b\d+\s+ready,\s*\d+\s+failed\b", text, flags=re.IGNORECASE)
    if upload_match:
        return f"Cassette upload status: {upload_match.group(0)}."
    zh_upload_match = re.search(r"\d+\s*个?\s*就绪[，,]\s*\d+\s*个?\s*失败", text)
    if zh_upload_match:
        return f"Cassette upload status: {zh_upload_match.group(0)}."
    for marker in ("Processing", "Uploading", "处理中", "上传中", "进行中", "分析中"):
        idx = text.lower().find(marker.lower())
        if idx >= 0:
            return _compact_summary_text(text[max(0, idx - 120) : idx + 700])
    for marker in (
        "Task Checklist",
        "任务清单",
        "任务列表",
        "Thinking",
        "思考",
        "Done",
        "已完成",
        "剪辑完成",
        "Export ready",
        "导出中",
        "导出完成",
        "Download",
        "下载",
        "Rendering",
        "渲染",
    ):
        idx = text.lower().find(marker.lower())
        if idx >= 0:
            return _compact_summary_text(text[max(0, idx - 120) : idx + 700])
    return _compact_summary_text(text[-700:])


def _record_stage_progress(
    job_id: str,
    body: str,
    outputs: list[dict],
    status: str = "running",
    stage: str | None = None,
    stage_elapsed_sec: float | None = None,
    attempt: int | None = None,
    operation_status: str | None = None,
) -> None:
    try:
        job = jobs.load_job(job_id)
        persisted_status = str(job.get("status") or "")
        if persisted_status in _TERMINAL_JOB_STATUSES and status == "running":
            event_status = persisted_status
        else:
            event_status = (
                "cancel_requested" if persisted_status == "cancel_requested" and status == "running" else status
            )
        events = list(job.get("progress_events") or [])[-9:]
        summary = _summarize_page_state(body)
        output_count = len(outputs)
        if events:
            last = events[-1]
            if (
                isinstance(last, dict)
                and last.get("summary") == summary
                and last.get("status") == event_status
                and last.get("operation_status") == operation_status
                and last.get("output_link_count") == output_count
            ):
                if stage_elapsed_sec is None:
                    return
                last_elapsed = last.get("stage_elapsed_sec")
                if isinstance(last_elapsed, (int, float)) and stage_elapsed_sec - float(last_elapsed) < 60:
                    return
        event = {
            "at": jobs.now_iso(),
            "status": event_status,
            "summary": summary,
            "output_link_count": output_count,
        }
        if stage:
            event["stage"] = stage
        if stage_elapsed_sec is not None:
            event["stage_elapsed_sec"] = round(max(0.0, stage_elapsed_sec), 1)
        if attempt is not None:
            event["attempt"] = attempt
        if operation_status:
            event["operation_status"] = operation_status
        events.append(event)
        fields: dict[str, Any] = {"progress_events": events}
        if persisted_status not in _TERMINAL_JOB_STATUSES or event_status in _TERMINAL_JOB_STATUSES:
            fields["status"] = event_status
        if stage:
            fields["current_stage"] = stage
        jobs.update_job(job_id, **fields)
    except Exception:
        pass


def _chat_indicates_complete(body: str, assistant_text: str = "") -> bool:
    value = (assistant_text or body or "").lower()
    if _page_suggests_routine_interaction(body, assistant_text):
        return False
    if re.search(
        r"(?:task checklist|任务清单|任务列表|检查清单|执行清单|工作清单)\s+0\s*/\s*\d+",
        value,
        flags=re.IGNORECASE,
    ):
        return False
    if any(
        term in value
        for term in (
            "shall i continue",
            "please approve",
            "please confirm",
            "continue stop",
            "确认后",
            "请确认",
            "待确认",
            "需要确认",
        )
    ):
        return False
    return _completion_phrase(value)


def _chat_indicates_missing_assets(body: str, assistant_text: str = "") -> bool:
    value = (assistant_text or body or "").lower()
    missing_asset_terms = (
        "workspace has zero media",
        "zero media files",
        "without source media",
        "no source media",
        "no video, audio, or images",
        "0 media files",
        "0 items",
        "no uploaded assets",
        "缺少素材",
        "没有素材",
        "未找到素材",
        "没有上传素材",
        "未上传素材",
        "请上传素材",
        "请添加素材",
        "工作区没有素材",
        "没有媒体文件",
    )
    if any(term in value for term in missing_asset_terms):
        return True
    return bool(
        re.search(
            r"\b(?:please upload|please provide|need you to upload|still need|missing required)\b.{0,120}\b(?:asset|media|file|video|audio|image)\b",
            value,
            flags=re.IGNORECASE,
        )
    )


def _page_suggests_routine_interaction(body: str, assistant_text: str = "") -> bool:
    value = f"{assistant_text or ''}\n{body or ''}".lower()
    if not value:
        return False
    checklist = _checklist_progress(value)
    checklist_waiting = False
    if checklist:
        done = checklist["done"]
        total = checklist["total"]
        checklist_waiting = total > 0 and done == 0
        if done >= total and total > 0:
            checklist_waiting = False
    routine_text = _routine_phrase(value)
    if not (checklist_waiting or routine_text):
        return False
    blocking_terms = (
        "missing asset",
        "upload failed",
        "render failed",
        "export failed",
        "no source media",
        "缺少素材",
        "请上传",
        "请添加素材",
        "上传失败",
        "渲染失败",
        "导出失败",
    )
    return not any(term in value for term in blocking_terms)


def _routine_interaction_key(body: str, assistant_text: str = "") -> str:
    text = " ".join((assistant_text or body or "").split())
    text = text.replace(ROUTINE_PLAN_APPROVAL, "").replace(
        "Please continue with the safest, highest-quality default option. Do not wait for confirmation.", ""
    )
    match = re.search(
        r"(Task Checklist|任务清单|任务列表|Execution Plan|执行方案|执行计划|编辑计划).{0,500}",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(0)[:500]
    return text[:500]


def _click_routine_interaction_control(page: Any) -> str | None:
    selector = os.getenv(
        "CASSETTE_ROUTINE_ACTION_SELECTOR",
        (
            "[data-testid='agent-approve-plan'],[data-testid='agent-confirm-plan'],"
            "[data-testid='agent-continue'],[data-testid='agent-run-plan'],[data-testid='agent-proceed'],"
            "[data-testid*='approve'],[data-testid*='confirm'],[data-testid*='continue'],"
            "[data-testid*='execute'],[data-testid*='run'],[data-testid*='proceed'],"
            "button:has-text('Approve'),button:has-text('Proceed'),button:has-text('Continue'),"
            "button:has-text('Run plan'),button:has-text('Run'),button:has-text('Execute'),button:has-text('Start'),"
            "button:has-text('Accept'),button:has-text('Apply'),button:has-text('OK'),"
            "button:has-text('批准'),button:has-text('确认'),button:has-text('继续'),"
            "button:has-text('执行'),button:has-text('开始'),button:has-text('同意'),button:has-text('应用')"
        ),
    )
    excluded_terms = (
        "export",
        "upload",
        "choose",
        "new chat",
        "chat history",
        "more tools",
        "cancel",
        "delete",
        "stop",
        "导出",
        "上传",
        "选择",
        "新对话",
        "对话历史",
        "更多工具",
        "取消",
        "删除",
        "停止",
    )
    try:
        controls = page.locator(selector)
        count = min(controls.count(), 12)
        for index in range(count):
            control = controls.nth(index)
            try:
                text = (
                    control.inner_text(timeout=500)
                    or control.get_attribute("aria-label")
                    or control.get_attribute("title")
                    or ""
                ).strip()
                if any(term in text.lower() for term in excluded_terms):
                    continue
                if hasattr(control, "is_visible") and not control.is_visible():
                    continue
                if hasattr(control, "is_enabled") and not control.is_enabled():
                    continue
                control.click(timeout=3000)
                return text or "routine action control"
            except Exception:
                continue
    except Exception:
        return None
    try:
        return page.evaluate(
            """(excludedTerms) => {
                const allow = [
                    'approve', 'approved', 'proceed', 'continue', 'run plan', 'run',
                    'execute', 'start', 'accept', 'apply', 'ok',
                    '批准', '确认', '继续', '执行', '开始', '同意', '应用'
                ];
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                for (const el of Array.from(document.querySelectorAll('button,[role="button"]'))) {
                    if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                    const text = [
                        el.innerText || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '',
                        el.getAttribute('data-testid') || ''
                    ].join(' ').trim();
                    const lower = text.toLowerCase();
                    if (!text || excludedTerms.some(term => lower.includes(term))) continue;
                    if (allow.some(term => lower.includes(term))) {
                        el.click();
                        return text.slice(0, 120) || 'routine action control';
                    }
                }
                return null;
            }""",
            list(excluded_terms),
        )
    except Exception:
        return None


def _click_chat_send_control_with_js(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                const inputSelectors = [
                    "[data-testid^='chat-input-textarea-']",
                    "[data-testid='agent-chat-input']",
                    "[data-testid='chat-input']",
                    "textarea[placeholder*='Describe']",
                    "textarea[placeholder*='描述']",
                    "textarea",
                    "[role='textbox']",
                    "[contenteditable='true']",
                    "input[type='text']"
                ];
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const enabled = (el) => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                const labelFor = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                    el.getAttribute('data-testid') || ''
                ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                const input = inputSelectors
                    .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
                    .find((el) => enabled(el));
                if (!input) return false;

                const roots = [];
                let cursor = input;
                for (let depth = 0; cursor && depth < 8; depth += 1) {
                    roots.push(cursor);
                    if (cursor.tagName && cursor.tagName.toLowerCase() === 'form') roots.unshift(cursor);
                    cursor = cursor.parentElement;
                }
                const sendTerms = ['send', 'submit', '发送', '提交', '送出'];
                const excludedTerms = [
                    'export', 'upload', 'choose', 'new chat', 'history', 'model', 'language',
                    'settings', 'tools', 'tool', 'attach', 'attachment', 'file', 'add',
                    '导出', '上传', '选择', '新对话', '历史', '模型', '语言', '设置', '工具', '附件', '文件', '添加'
                ];
                let best = null;
                let bestScore = -9999;
                const seen = new Set();
                roots.forEach((root, rootIndex) => {
                    if (!root || !root.querySelectorAll) return;
                    const buttons = Array.from(root.querySelectorAll('button,[role="button"]')).filter(enabled);
                    buttons.forEach((button, index) => {
                        if (seen.has(button)) return;
                        seen.add(button);
                        const label = labelFor(button);
                        if (label && excludedTerms.some((term) => label.includes(term))) return;
                        let score = index;
                        if (sendTerms.some((term) => label.includes(term))) score += 100;
                        if (!label) score += 10;
                        if (index === buttons.length - 1) score += 25;
                        score += Math.max(0, 12 - rootIndex);
                        if (score > bestScore) {
                            best = button;
                            bestScore = score;
                        }
                    });
                });
                if (!best) return false;
                best.click();
                return true;
            }"""
            )
        )
    except Exception:
        return False


def _click_chat_send_control(page: Any, send_selector: str) -> None:
    selectors = _selector_visible_variants(send_selector)
    selectors.extend(
        [
            "[data-testid^='chat-input-send-']:visible",
            "[data-testid='agent-send-button']:visible",
            "[data-testid='chat-send-button']:visible",
            "button[aria-label*='Send']:visible",
            "button[title*='Send']:visible",
            "button:has-text('Send'):visible",
            "button:has-text('发送'):visible",
            "button[type='submit']:visible",
        ]
    )
    try:
        _click_first_visible(page, selectors, timeout=3000)
        return
    except Exception:
        pass
    if _click_chat_send_control_with_js(page):
        return
    raise RuntimeError("Cassette chat send control was not available")


def _send_chat_message(page: Any, chat_selector: str, send_selector: str, message: str) -> None:
    try:
        _fill_prompt(page, chat_selector, message)
    except Exception:
        _fill_prompt(page, "textarea, [contenteditable='true'], input[type='text']", message)

    try:
        _click_chat_send_control(page, send_selector)
    except Exception:
        page.keyboard.press("Enter")


def _chat_submission_observed(page: Any, baseline_body: str, baseline_count: int, baseline_text: str) -> bool:
    assistant_text, assistant_is_current = _current_assistant_message_text(page, baseline_count, baseline_text)
    if assistant_is_current and assistant_text:
        return True
    try:
        body = page.locator("body").inner_text(timeout=800)
    except Exception:
        body = ""
    if body and body != baseline_body:
        return True
    if _agent_is_busy(page, body):
        return True
    try:
        controls = _visible_control_states(page)
        if (_stop_control_state(controls) or {}).get("enabled"):
            return True
    except Exception:
        pass
    return False


def _wait_for_chat_submission_activity(
    page: Any,
    baseline_body: str,
    baseline_count: int,
    baseline_text: str,
    timeout_sec: int | None = None,
) -> bool:
    deadline = time.monotonic() + max(
        1, timeout_sec if timeout_sec is not None else _int_env("CASSETTE_CHAT_SUBMIT_VERIFY_SEC", 30)
    )
    while time.monotonic() < deadline:
        if _chat_submission_observed(page, baseline_body, baseline_count, baseline_text):
            return True
        time.sleep(0.5)
    return _chat_submission_observed(page, baseline_body, baseline_count, baseline_text)


def _click_new_chat_control(page: Any) -> str | None:
    selector = os.getenv(
        "CASSETTE_NEW_CHAT_SELECTOR",
        (
            "button[title='New Chat'],button[aria-label='New Chat'],button:has-text('New Chat'),"
            "button[title='新对话'],button[aria-label='新对话'],button:has-text('新对话'),"
            "button[title='新聊天'],button[aria-label='新聊天'],button:has-text('新聊天')"
        ),
    )
    try:
        controls = page.locator(selector)
        count = min(controls.count(), 8)
        for index in range(count):
            control = controls.nth(index)
            try:
                if hasattr(control, "is_visible") and not control.is_visible():
                    continue
                if hasattr(control, "is_enabled") and not control.is_enabled():
                    continue
                label = (
                    control.inner_text(timeout=300)
                    or control.get_attribute("aria-label")
                    or control.get_attribute("title")
                    or "New Chat"
                ).strip()
                control.click(timeout=5000)
                return label or "New Chat"
            except Exception:
                continue
    except Exception:
        return None
    return None


def _confirm_new_chat_if_needed(page: Any) -> None:
    selector = os.getenv(
        "CASSETTE_NEW_CHAT_CONFIRM_SELECTOR",
        (
            "[role='dialog'] button:has-text('New Chat'),[role='dialog'] button:has-text('Start new'),"
            "[role='dialog'] button:has-text('Confirm'),[role='dialog'] button:has-text('确认'),"
            "[role='dialog'] button:has-text('新对话'),[role='dialog'] button:has-text('开始新对话')"
        ),
    )
    try:
        dialog = page.locator("[role='dialog']").last
        dialog.wait_for(state="visible", timeout=800)
    except Exception:
        return
    try:
        page.locator(selector).first.click(timeout=2000)
    except Exception:
        pass


def _wait_for_ready_assets_after_new_chat(page: Any, expected_count: int) -> bool:
    if expected_count <= 0:
        return True
    deadline = time.monotonic() + max(1, _int_env("CASSETTE_NEW_CHAT_READY_TIMEOUT_SEC", 10))
    while time.monotonic() < deadline:
        if _agent_page_has_ready_assets(page, expected_count):
            return True
        time.sleep(0.5)
    return False


def _reset_to_new_chat_preserving_assets(
    page: Any,
    job_id: str,
    expected_asset_count: int,
    outputs: list[dict],
    attempt: int,
    stage_elapsed_sec: float,
    reason_code: str,
    stage: str = "agent",
    require_ready_assets: bool = True,
) -> bool:
    clicked = _click_new_chat_control(page)
    if not clicked:
        return False
    _confirm_new_chat_if_needed(page)
    try:
        page.wait_for_timeout(500)
    except Exception:
        pass
    ready = _wait_for_ready_assets_after_new_chat(page, expected_asset_count)
    if require_ready_assets and not ready:
        return False
    suffix = "without reupload" if ready else "without reupload; asset readiness will be verified by Cassette chat"
    _record_stage_progress(
        job_id,
        f"Cassette reset reason {reason_code}; started a new chat {suffix}.",
        outputs,
        stage=stage,
        stage_elapsed_sec=stage_elapsed_sec,
        attempt=attempt,
    )
    return True


def _soft_retry_with_new_chat(
    page: Any,
    job: dict,
    chat_selector: str,
    send_selector: str,
    expected_asset_count: int,
    outputs: list[dict],
    attempt: int,
    stage_elapsed_sec: float,
    reason_code: str,
) -> bool:
    job_id = job["job_id"]
    if not _reset_to_new_chat_preserving_assets(
        page,
        job_id,
        expected_asset_count,
        outputs,
        attempt,
        stage_elapsed_sec,
        reason_code,
    ):
        return False
    try:
        _send_chat_message(page, chat_selector, send_selector, _chat_message_for_job(job))
    except Exception:
        return False
    _record_stage_progress(
        job_id,
        f"Cassette soft retry attempt {attempt} submitted in a new chat.",
        outputs,
        stage="agent",
        stage_elapsed_sec=stage_elapsed_sec,
        attempt=attempt,
    )
    return True


def _agent_is_busy(page: Any, body: str) -> bool:
    try:
        graph_status = page.locator("[data-testid='agent-graph-status']").first.inner_text(timeout=500).strip().lower()
        if graph_status:
            return True
    except Exception:
        pass
    value = (body or "").lower()
    busy_terms = (
        "thinking",
        "working",
        "generating",
        "processing",
        "rendering",
        "analyzing",
        "exporting",
        "正在",
        "处理中",
        "进行中",
        "生成中",
        "思考",
        "工作中",
        "渲染中",
        "分析中",
        "导出中",
    )
    return any(term in value for term in busy_terms)


def _export_on_complete(job: dict) -> bool:
    raw = os.getenv("CASSETTE_EXPORT_ON_COMPLETE", str(job.get("export_on_complete", "true")))
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _progress_snapshot_interval_sec() -> int:
    raw = os.getenv("CASSETTE_PROGRESS_SNAPSHOT_SEC", "180")
    try:
        return max(0, int(raw))
    except ValueError:
        return 180


def _agent_soft_retry_limit() -> int:
    return max(0, _int_env("CASSETTE_AGENT_SOFT_RETRY_LIMIT", 1))


def _public_stage_timings(
    stage_timings: dict[str, dict[str, Any]], include_running_elapsed: bool = False
) -> dict[str, dict[str, Any]]:
    public: dict[str, dict[str, Any]] = {}
    for stage, data in stage_timings.items():
        item = {key: value for key, value in data.items() if not key.startswith("_")}
        if include_running_elapsed:
            started = data.get("_started_monotonic")
            if isinstance(started, (int, float)):
                item["duration_sec"] = round(
                    float(item.get("duration_sec") or 0.0) + max(0.0, time.monotonic() - float(started)), 1
                )
        public[stage] = item
    return public


def _model_selection_for_job(job: dict) -> dict:
    selection = dict(job.get("model_selection") or {})
    return {
        "model": str(selection.get("model") or "").strip(),
        "thinking_level": str(
            selection.get("thinking_level") or os.getenv("CASSETTE_DEFAULT_THINKING_LEVEL", "Low")
        ).strip(),
    }


def _thinking_level_ui_candidates(thinking: str) -> list[str]:
    value = str(thinking or "").strip()
    normalized = value.casefold()
    aliases = {
        "low": ["Low", "低", "轻量推理"],
        "medium": ["Medium", "中", "平衡"],
        "high": ["High", "高", "深度推理"],
        "低": ["低", "Low", "轻量推理"],
        "中": ["中", "Medium", "平衡"],
        "高": ["高", "High", "深度推理"],
    }
    candidates = [value] if value else []
    candidates.extend(aliases.get(normalized, aliases.get(value, [])))
    seen: set[str] = set()
    return [item for item in candidates if item and not (item in seen or seen.add(item))]


def _normalize_cassette_language(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if raw in {"zh", "zh-cn", "cn", "chinese", "mandarin", "中文", "汉语", "简体中文"}:
        return "zh"
    if raw in {"en", "en-us", "en-gb", "english", "英文", "英语"}:
        return "en"
    return ""


def _language_for_job(job: dict) -> str:
    return _normalize_cassette_language(job.get("cassette_language") or job.get("language")) or "zh"


def _current_cassette_language(page: Any) -> str:
    try:
        controls = _visible_control_states(page)
    except Exception:
        controls = []
    for control in controls:
        label = _control_label(control)
        text = str(control.get("text") or "").strip().casefold()
        title = str(control.get("title") or "").strip().casefold()
        aria = str(control.get("aria") or "").strip().casefold()
        if "agent language" in title or "ai 语言" in title or "language" in aria or "语言" in aria:
            if text in {"en", "english"}:
                return "en"
            if text in {"中文", "chinese"}:
                return "zh"
        if text in {"en", "english"} and ("language" in label or "语言" in label):
            return "en"
        if text in {"中文", "chinese"} and ("language" in label or "语言" in label):
            return "zh"
    try:
        html_lang = str(page.evaluate("document.documentElement.lang || ''") or "").strip().lower()
    except Exception:
        html_lang = ""
    if html_lang.startswith("zh"):
        return "zh"
    if html_lang.startswith("en"):
        return "en"
    return ""


def _click_language_trigger(page: Any) -> bool:
    selectors = (
        "button[title='AI 语言']",
        "button[title='Agent language']",
        "button[aria-label*='language' i]",
        "button[aria-label*='语言']",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if hasattr(locator, "is_visible") and not locator.is_visible():
                continue
            if hasattr(locator, "is_enabled") and not locator.is_enabled():
                continue
            locator.click(timeout=3000)
            return True
        except Exception:
            continue
    try:
        return bool(
            page.evaluate(
                """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const candidates = Array.from(document.querySelectorAll('button')).filter((el) => {
                    if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
                    const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                    const title = (el.getAttribute('title') || '').toLowerCase();
                    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    return title.includes('language') || title.includes('语言') || aria.includes('language') || aria.includes('语言') || text === 'EN' || text === '中文';
                });
                if (!candidates.length) return false;
                candidates[0].click();
                return true;
            }"""
            )
        )
    except Exception:
        return False


def _click_language_option(page: Any, language: str) -> None:
    labels = ("EN", "English") if language == "en" else ("中文", "Chinese")
    selectors: list[str] = []
    for label in labels:
        selectors.extend(
            [
                f"[role='menuitemradio']:has-text('{label}')",
                f"[role='menuitem']:has-text('{label}')",
                f"[role='option']:has-text('{label}')",
                f"button:has-text('{label}')",
            ]
        )
    _click_first_visible(page, selectors, timeout=5000)


def _select_cassette_language(page: Any, job: dict) -> dict[str, Any]:
    target = _language_for_job(job)
    if page.url.startswith("file:"):
        return {"status": "skipped", "reason": "fixture_page", "language": target}
    timeout_sec = max(1.0, min(15.0, float(job.get("timeout_sec") or 15)))
    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    try:
        while time.monotonic() < deadline:
            current = _current_cassette_language(page)
            if current == target:
                return {"status": "selected", "reason": "already_selected", "language": target}
            try:
                if not _click_language_trigger(page):
                    raise RuntimeError("language_trigger_missing")
                _click_language_option(page, target)
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                current = _current_cassette_language(page)
                if current and current != target:
                    raise RuntimeError(f"language_mismatch:{current}")
                return {"status": "selected", "language": target}
            except Exception as exc:
                last_error = exc
                try:
                    page.wait_for_timeout(750)
                except Exception:
                    time.sleep(0.75)
        if last_error:
            raise last_error
        raise RuntimeError("language_selection_timeout")
    except Exception as exc:
        if os.getenv("CASSETTE_REQUIRE_LANGUAGE_SELECTION", "true").lower() in {"0", "false", "no", "off"}:
            return {"status": "skipped", "reason": type(exc).__name__, "language": target}
        raise RuntimeError(f"Failed to select Cassette language {target}: {type(exc).__name__}") from exc


def _button_label_values(button: Any) -> list[str]:
    values: list[str] = []
    for getter in (
        lambda: button.inner_text(timeout=300),
        lambda: button.get_attribute("aria-label"),
        lambda: button.get_attribute("title"),
    ):
        try:
            value = getter()
        except Exception:
            value = ""
        if value:
            values.append(str(value).strip())
    return values


def _dialog_label_matches(label: str, candidate: str) -> bool:
    label_norm = label.strip().casefold()
    candidate_norm = candidate.strip().casefold()
    if not label_norm or not candidate_norm:
        return False
    if label_norm == candidate_norm:
        return True
    # Cassette model buttons often render as "Model name\nshort description"
    # or "Model name short description". Allow prefix matches for full model
    # names, but keep short labels like "高" exact to avoid matching unrelated text.
    if len(candidate_norm) > 2 and label_norm.startswith(candidate_norm):
        next_char = label_norm[len(candidate_norm) : len(candidate_norm) + 1]
        return next_char in {"", " ", "\n", "\t", "·", "-", "，", ","}
    return False


def _click_dialog_button(dialog: Any, candidates: list[str], timeout: int = 5000) -> None:
    deadline = time.monotonic() + timeout / 1000
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            buttons = dialog.locator("button")
            count = min(buttons.count(), 80)
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
            continue
        normalized_candidates = [candidate.strip().casefold() for candidate in candidates if candidate.strip()]
        for index in range(count):
            button = buttons.nth(index)
            try:
                if hasattr(button, "is_visible") and not button.is_visible():
                    continue
                if hasattr(button, "is_enabled") and not button.is_enabled():
                    continue
                labels = _button_label_values(button)
                label_candidates = []
                for label in labels:
                    label_candidates.append(label)
                    first_line = label.splitlines()[0].strip() if label.splitlines() else ""
                    if first_line:
                        label_candidates.append(first_line)
                normalized_labels = {label.strip().casefold() for label in label_candidates if label.strip()}
                if any(
                    _dialog_label_matches(label, candidate)
                    for label in normalized_labels
                    for candidate in normalized_candidates
                ):
                    button.click(timeout=3000)
                    return
            except Exception as exc:
                last_error = exc
                continue
        time.sleep(0.2)
    raise RuntimeError(f"Cassette dialog button not found for: {', '.join(candidates)}") from last_error


def _click_model_trigger(page: Any) -> None:
    selector = os.getenv(
        "CASSETTE_MODEL_TRIGGER_SELECTOR",
        "button[title*='·'],button[title*='DeepSeek'],button[title*='Kimi'],button[title*='GPT'],button[title*='Gemini']",
    )
    try:
        page.locator(selector).first.click(timeout=5000)
        return
    except Exception:
        pass
    clicked = page.evaluate(
        """() => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const terms = ['model', '模型', 'deepseek', 'kimi', 'gpt', 'gemini', 'mimo', '思考'];
            const buttons = Array.from(document.querySelectorAll('button,[role="button"]')).filter((el) => {
                if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
                const label = `${el.innerText || ''} ${el.getAttribute('title') || ''} ${el.getAttribute('aria-label') || ''}`.toLowerCase();
                return terms.some((term) => label.includes(term));
            });
            if (!buttons.length) return false;
            buttons[0].click();
            return true;
        }"""
    )
    if not clicked:
        raise RuntimeError("cassette_model_trigger_missing")


def _clean_model_label(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) > 1:
        return lines[0]
    value = lines[0] if lines else str(text or "").strip()
    for marker in (
        " 高效",
        " 旗舰",
        " 快速",
        " 长上下文",
        " 小米",
        " Moonshot",
        " OpenAI",
        " efficient",
        " flagship",
        " fast",
        " long context",
    ):
        if marker in value:
            return value.split(marker, 1)[0].strip()
    return value


def _thinking_value_from_label(label: str, title: str = "") -> str:
    label_norm = str(label or "").strip().casefold()
    title_norm = str(title or "").strip().casefold()
    if label_norm in {"低", "low"} or "轻量" in title_norm or "low" in title_norm:
        return "Low"
    if label_norm in {"中", "medium"} or "平衡" in title_norm or "medium" in title_norm or "balanced" in title_norm:
        return "Medium"
    if label_norm in {"高", "high"} or "深度" in title_norm or "high" in title_norm or "deep" in title_norm:
        return "High"
    return ""


def _model_dialog_options(dialog: Any) -> dict[str, Any]:
    items = dialog.locator("button")
    models: list[dict[str, str]] = []
    thinking_levels: list[dict[str, str]] = []
    try:
        count = min(items.count(), 120)
    except Exception:
        count = 0
    for index in range(count):
        item = items.nth(index)
        try:
            if hasattr(item, "is_visible") and not item.is_visible():
                continue
            text = (item.inner_text(timeout=500) or "").strip()
            title = (item.get_attribute("title") or "").strip()
        except Exception:
            continue
        if not text and not title:
            continue
        thinking_value = _thinking_value_from_label(text, title)
        if thinking_value:
            label = text or title or thinking_value
            thinking_levels.append({"label": label, "value": thinking_value, "title": title})
            continue
        label = _clean_model_label(text)
        if label:
            models.append({"label": label})
    seen_models: set[str] = set()
    deduped_models = []
    for item in models:
        key = item["label"].casefold()
        if key in seen_models:
            continue
        seen_models.add(key)
        deduped_models.append(item)
    seen_thinking: set[str] = set()
    deduped_thinking = []
    for item in thinking_levels:
        key = item["value"]
        if key in seen_thinking:
            continue
        seen_thinking.add(key)
        deduped_thinking.append(item)
    return {"models": deduped_models, "thinking_levels": deduped_thinking}


def _fetch_cassette_model_options_direct(url: str | None = None, language: str = "zh") -> dict[str, Any]:
    if not check_playwright():
        raise RuntimeError("playwright_not_installed")
    timeout_ms = int(float(os.getenv("CASSETTE_MODEL_OPTIONS_TIMEOUT_SEC", "30")) * 1000)
    target_url = _normalize_cassette_url(url or _runtime_env("CASSETTE_URL") or "https://sg.trycassette.online/agent")
    headless = os.getenv("CASSETTE_HEADLESS", "true").lower() not in {"0", "false", "no"}
    launch_args = []
    if os.getenv("CASSETTE_NO_SANDBOX", "false").lower() in {"1", "true", "yes"}:
        launch_args.append("--no-sandbox")
    record = _new_browser_record(
        {"job_id": ""}, headless, launch_args, target_url, timeout_ms, isolated_playwright=True
    )
    try:
        page = record["page"]
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
        except Exception:
            pass
        target_language = _normalize_cassette_language(language) or "zh"
        try:
            _select_cassette_language(page, {"cassette_language": target_language})
        except Exception:
            pass
        _click_model_trigger(page)
        dialog = page.locator("[role='dialog']").last
        dialog.wait_for(state="visible", timeout=min(timeout_ms, 10000))
        options = _model_dialog_options(dialog)
        if not options.get("models"):
            raise RuntimeError("cassette_model_options_empty")
        if not options.get("thinking_levels"):
            options["thinking_levels"] = [
                {"label": "低" if target_language == "zh" else "Low", "value": "Low", "title": "轻量推理"},
                {"label": "中" if target_language == "zh" else "Medium", "value": "Medium", "title": "平衡"},
                {"label": "高" if target_language == "zh" else "High", "value": "High", "title": "深度推理"},
            ]
        return {
            **options,
            "source": "cassette_agent_page",
            "language": target_language,
        }
    finally:
        _close_browser_record(record)


def _model_options_result_timeout_sec() -> float:
    configured = os.getenv("CASSETTE_MODEL_OPTIONS_WORKER_TIMEOUT_SEC", "").strip()
    if configured:
        try:
            return max(1.0, float(configured))
        except ValueError:
            pass
    try:
        return max(1.0, float(os.getenv("CASSETTE_MODEL_OPTIONS_TIMEOUT_SEC", "30")) + 5.0)
    except ValueError:
        return 35.0


def fetch_cassette_model_options(url: str | None = None, language: str = "zh") -> dict[str, Any]:
    if not _model_options_worker_enabled() or _in_model_options_worker():
        return _fetch_cassette_model_options_direct(url=url, language=language)
    future = _model_options_worker().submit(_fetch_cassette_model_options_direct, url, language)
    try:
        return future.result(timeout=_model_options_result_timeout_sec())
    except FuturesTimeoutError as exc:
        future.cancel()
        raise RuntimeError("cassette_model_options_timeout") from exc


def _select_cassette_model(page: Any, job: dict) -> dict:
    selection = _model_selection_for_job(job)
    model = selection["model"]
    thinking = selection["thinking_level"]
    if not model:
        return {**selection, "status": "skipped", "reason": "model_not_selected"}
    if page.url.startswith("file:"):
        return {**selection, "status": "skipped", "reason": "fixture_page"}
    try:
        _click_model_trigger(page)
        dialog = page.locator("[role='dialog']").last
        dialog.wait_for(state="visible", timeout=5000)
        _click_dialog_button(dialog, [model], timeout=5000)
        try:
            dialog = page.locator("[role='dialog']").last
            dialog.wait_for(state="visible", timeout=3000)
        except Exception:
            _click_model_trigger(page)
            dialog = page.locator("[role='dialog']").last
            dialog.wait_for(state="visible", timeout=5000)
        _click_dialog_button(dialog, _thinking_level_ui_candidates(thinking), timeout=5000)
        return {**selection, "status": "selected"}
    except Exception as exc:
        if os.getenv("CASSETTE_REQUIRE_MODEL_SELECTION", "true").lower() in {"0", "false", "no", "off"}:
            return {**selection, "status": "skipped", "reason": type(exc).__name__}
        raise RuntimeError(f"Failed to select Cassette model {model} / {thinking}: {type(exc).__name__}") from exc


def _record_has_model_selection(record: dict[str, Any]) -> bool:
    selection = record.get("model_selection")
    if not isinstance(selection, dict):
        return False
    return str(selection.get("status") or "") in {"selected", "skipped"}


def _model_value(value: Any) -> str:
    return str(value or "").strip().casefold()


def _record_model_selection_matches(record: dict[str, Any], job: dict) -> bool:
    current = dict(record.get("model_selection") or {})
    if not current:
        return False
    if current.get("status") == "skipped" and current.get("reason") == "fixture_page":
        return True
    requested = _model_selection_for_job(job)
    return _model_value(current.get("model")) == _model_value(requested["model"]) and _model_value(
        current.get("thinking_level")
    ) == _model_value(requested["thinking_level"])


def _reuse_model_selection(record: dict[str, Any], job: dict) -> dict:
    current = dict(record.get("model_selection") or {})
    requested = _model_selection_for_job(job)
    result = {
        "model": str(current.get("model") or requested["model"]),
        "thinking_level": str(current.get("thinking_level") or requested["thinking_level"]),
        "status": "skipped",
        "reason": "session_reuse",
    }
    if requested["model"] != result["model"]:
        result["requested_model"] = requested["model"]
    if requested["thinking_level"] != result["thinking_level"]:
        result["requested_thinking_level"] = requested["thinking_level"]
    return result


def _success_result(
    page: Any,
    job: dict,
    outputs: list[dict],
    questions: list[dict],
    errors: list[dict],
    quality: dict,
    output_selector: str,
    stage_control: dict[str, Any] | None = None,
) -> dict:
    job_id = job["job_id"]
    if _export_on_complete(job):
        if stage_control:
            stage_control["begin"]("export")
        try:
            timeout_sec = int(os.getenv("CASSETTE_EXPORT_TIMEOUT_SEC", str(job.get("timeout_sec") or 1800)))
            attempts = max(1, _int_env("CASSETTE_EXPORT_RETRY_LIMIT", 1) + 1)
            exported: list[dict] = []
            for attempt in range(1, attempts + 1):
                if stage_control:
                    stage_control["attempt"]("export", attempt)
                try:
                    exported = _download_export(page, job_id, output_selector, timeout_sec)
                    break
                except ExportError as exc:
                    _record_stage_progress(
                        job_id,
                        f"Export attempt {attempt} failed: {exc.code}",
                        outputs,
                        stage="export",
                        stage_elapsed_sec=stage_control["elapsed"]("export") if stage_control else None,
                        attempt=attempt,
                    )
                    if attempt < attempts:
                        time.sleep(2)
                        continue
                    raise
            if exported:
                outputs = exported
        except ExportError as exc:
            if stage_control:
                stage_control["finish"]("export", "cancelled" if exc.code == "export_cancelled" else "failed")
            errors.append({"code": exc.code, "message": str(exc)})
            if exc.code == "export_cancelled":
                return {
                    "status": "cancelled",
                    "outputs": outputs,
                    "questions": questions,
                    "errors": errors,
                    "quality": {
                        **quality,
                        "completion_observed": True,
                        "export_completed": False,
                        "progress_summary": "Cassette export was cancelled by /cut; browser state is preserved.",
                        "risk": "medium",
                    },
                    "final_screenshot": _screenshot(page, job_id),
                }
            return {
                "status": "failed",
                "outputs": outputs,
                "questions": questions,
                "errors": errors,
                "quality": {**quality, "completion_observed": True, "export_completed": False, "risk": "high"},
                "final_screenshot": _screenshot(page, job_id),
            }
        if stage_control:
            stage_control["finish"]("export", "succeeded")
    screenshot = _screenshot(page, job_id)
    has_local_output = any(output.get("local_path") for output in outputs if isinstance(output, dict))
    return {
        "status": "succeeded",
        "outputs": outputs,
        "questions": questions,
        "errors": errors,
        "quality": {
            **quality,
            "completion_observed": True,
            "export_completed": bool(outputs),
            "export_pending": False if outputs else not _export_on_complete(job),
            "output_link_count": len(outputs),
            "local_output_count": sum(1 for output in outputs if isinstance(output, dict) and output.get("local_path")),
            "risk": "low" if has_local_output else "medium",
        },
        "final_screenshot": screenshot,
    }


def export_reviewed_completion_job_threaded(job: dict, decision: dict[str, Any] | None = None) -> dict:
    if _browser_worker_enabled() and threading.get_ident() != _BROWSER_WORKER_THREAD_ID:
        return _browser_worker().submit(export_reviewed_completion_job, job, decision or {}).result()
    return export_reviewed_completion_job(job, decision or {})


def export_reviewed_completion_job(job: dict, decision: dict[str, Any] | None = None) -> dict:
    if not check_playwright():
        return {
            "status": "failed",
            "outputs": [],
            "questions": job.get("questions") or [],
            "errors": [{"code": "playwright_not_installed", "message": "Python Playwright is not installed"}],
            "quality": {"completion_observed": False, "output_link_count": 0, "risk": "high"},
            "final_screenshot": None,
        }
    job_id = str(job.get("job_id") or "")
    record = _BROWSER_SESSIONS.get(_browser_session_key(job))
    page = record.get("page") if isinstance(record, dict) else None
    if not page or _page_is_closed(page):
        return {
            "status": "failed",
            "outputs": job.get("outputs") or [],
            "questions": job.get("questions") or [],
            "errors": [
                {
                    "code": "cassette_browser_session_missing",
                    "message": "Hermes approved export, but the live Cassette browser session was not available.",
                }
            ],
            "quality": {
                **(job.get("quality") or {}),
                "completion_observed": False,
                "export_completed": False,
                "risk": "high",
            },
            "final_screenshot": None,
        }
    output_selector = _selector(
        job, "output", "CASSETTE_OUTPUT_SELECTOR", "[data-testid='export-link'],[data-testid='download-link'],a[href]"
    )
    outputs = _collect_outputs(page, output_selector)
    questions = list(job.get("questions") or [])
    errors = list(job.get("errors") or [])
    quality = {
        **(job.get("quality") or {}),
        "completion_source": "hermes_completion_review",
        "completion_review": {
            "decision": str((decision or {}).get("decision") or "export"),
            "reason": _compact_summary_text(str((decision or {}).get("reason") or ""), 500),
        },
        "progress_summary": _compact_summary_text(
            str((decision or {}).get("summary") or (job.get("quality") or {}).get("progress_summary") or ""), 700
        ),
    }
    _record_stage_progress(job_id, "Hermes completion review approved export; starting Cassette export.", outputs)
    return _success_result(page, job, outputs, questions, errors, quality, output_selector, stage_control=None)


def run_cassette_browser_job(job: dict) -> dict:
    if not check_playwright():
        return {
            "status": "failed",
            "outputs": [],
            "questions": [],
            "errors": [{"code": "playwright_not_installed", "message": "Python Playwright is not installed"}],
            "quality": {"completion_observed": False, "output_link_count": 0, "risk": "high"},
            "final_screenshot": None,
        }

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    job_id = job["job_id"]
    timeout_ms = int(job.get("timeout_sec") or 1800) * 1000
    url = _normalize_cassette_url(
        job.get("url") or _runtime_env("CASSETTE_URL") or "https://sg.trycassette.online/agent"
    )
    headless = os.getenv("CASSETTE_HEADLESS", "true").lower() not in {"0", "false", "no"}
    upload_selector = _selector(job, "upload", "CASSETTE_UPLOAD_SELECTOR", "[data-testid='agent-file-input']")
    chat_selector = _selector(job, "chat", "CASSETTE_CHAT_SELECTOR", DEFAULT_CHAT_SELECTOR)
    send_selector = _selector(job, "send", "CASSETTE_SEND_SELECTOR", DEFAULT_SEND_SELECTOR)
    done_selector = _selector(job, "done", "CASSETTE_DONE_SELECTOR", "[data-testid='job-done']")
    output_selector = _selector(
        job, "output", "CASSETTE_OUTPUT_SELECTOR", "[data-testid='export-link'],[data-testid='download-link'],a[href]"
    )
    errors: list[dict] = []
    questions: list[dict] = []
    outputs: list[dict] = []
    stage_timings: dict[str, dict[str, Any]] = {}
    current_stage = "queued"

    def begin_stage(stage: str) -> None:
        nonlocal current_stage
        current_stage = stage
        item = stage_timings.setdefault(stage, {"attempts": 0, "duration_sec": 0.0})
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["_started_monotonic"] = time.monotonic()
        item["started_at"] = item.get("started_at") or jobs.now_iso()
        item["status"] = "running"
        try:
            jobs.update_job(job_id, current_stage=stage, stage_timings=_public_stage_timings(stage_timings))
        except Exception:
            pass

    def mark_stage_attempt(stage: str, attempt: int) -> None:
        item = stage_timings.setdefault(stage, {"attempts": 0, "duration_sec": 0.0})
        item["attempts"] = max(int(item.get("attempts") or 0), attempt)
        try:
            jobs.update_job(job_id, current_stage=stage, stage_timings=_public_stage_timings(stage_timings))
        except Exception:
            pass

    def stage_elapsed(stage: str) -> float:
        item = stage_timings.get(stage) or {}
        started = item.get("_started_monotonic")
        if isinstance(started, (int, float)):
            return time.monotonic() - float(started)
        return float(item.get("duration_sec") or 0.0)

    def finish_stage(stage: str, status: str) -> None:
        item = stage_timings.setdefault(stage, {"attempts": 0, "duration_sec": 0.0})
        started = item.pop("_started_monotonic", None)
        if isinstance(started, (int, float)):
            item["duration_sec"] = round(
                float(item.get("duration_sec") or 0.0) + max(0.0, time.monotonic() - float(started)), 1
            )
        item["status"] = status
        item["finished_at"] = jobs.now_iso()
        try:
            jobs.update_job(job_id, current_stage=stage, stage_timings=_public_stage_timings(stage_timings))
        except Exception:
            pass

    stage_control = {
        "begin": begin_stage,
        "finish": finish_stage,
        "elapsed": stage_elapsed,
        "attempt": mark_stage_attempt,
    }

    def finalize(result: dict, terminal_stage: str | None = None, terminal_stage_status: str | None = None) -> dict:
        if terminal_stage:
            finish_stage(terminal_stage, terminal_stage_status or result.get("status") or "finished")
        quality = dict(result.get("quality") or {})
        quality["stage_timings"] = _public_stage_timings(stage_timings)
        quality["current_stage"] = current_stage
        result["quality"] = quality
        try:
            jobs.update_job(job_id, current_stage=current_stage, stage_timings=_public_stage_timings(stage_timings))
        except Exception:
            pass
        return result

    def record_snapshot_notification(result: dict) -> None:
        try:
            job_state = jobs.load_job(job_id)
            notifications = list(job_state.get("progress_snapshot_notifications") or [])[-9:]
            notifications.append({"at": jobs.now_iso(), **result})
            jobs.update_job(job_id, progress_snapshot_notifications=notifications)
        except Exception:
            pass

    def send_progress_snapshot(summary: str) -> None:
        if page is None:
            return
        screenshot_path = _progress_screenshot(page, job_id)
        if not screenshot_path:
            record_snapshot_notification({"status": "failed", "code": "screenshot_failed"})
            return
        try:
            job_state = jobs.load_job(job_id)
        except Exception:
            job_state = dict(job)
        job_state["current_stage"] = current_stage
        job_state["stage_timings"] = _public_stage_timings(stage_timings, include_running_elapsed=True)
        result = notifier.notify_progress_snapshot(job_state, screenshot_path, summary)
        record_snapshot_notification(result)

    launch_args = []
    if os.getenv("CASSETTE_NO_SANDBOX", "false").lower() in {"1", "true", "yes"}:
        launch_args.append("--no-sandbox")

    record: dict[str, Any] | None = None
    page = None
    created_for_one_shot = False

    def close_browser_on_terminal_requested() -> bool:
        if job.get("close_browser_on_terminal"):
            return True
        try:
            persisted = jobs.load_job(job_id)
        except Exception:
            return False
        return bool(persisted.get("close_browser_on_terminal"))

    def close_current_browser_session() -> None:
        if not record:
            return
        if _browser_reuse_enabled():
            if close_browser_sessions(_browser_session_key(job)):
                return
        _close_browser_record(record)

    def discard_reusable_browser_session() -> None:
        if record and _browser_reuse_enabled():
            close_browser_sessions(_browser_session_key(job))

    try:
        try:
            begin_stage("page_load")
            record, created_for_one_shot = _browser_record(job, headless, launch_args, url, timeout_ms)
            page = record["page"]
            if not created_for_one_shot:
                _record_operation_progress(
                    job_id,
                    "connectivity",
                    "Cassette connectivity check skipped: reusing live browser session.",
                    operation_status="skipped",
                    stage_elapsed_sec=0.0,
                )
                record["auth"], record["ui_ready"] = _prepare_agent_page_for_automation(page, job_id, timeout_ms)
            finish_stage("page_load", "succeeded")
        except Exception as exc:
            finish_stage("page_load", "failed")
            if isinstance(exc, BrowserConnectivityError):
                code = exc.code
            elif isinstance(exc, BrowserAuthError):
                code = exc.code
            elif isinstance(exc, BrowserUIReadyError):
                code = exc.code
            else:
                code = "cassette_page_load_failed" if isinstance(exc, BrowserPageLoadError) else "browser_launch_failed"
            errors.append({"code": code, "message": str(exc), "details": {"type": type(exc).__name__}})
            result = {
                "status": "failed",
                "outputs": outputs,
                "questions": questions,
                "errors": errors,
                "quality": {"completion_observed": False, "output_link_count": 0, "risk": "high"},
                "final_screenshot": _screenshot(page, job_id) if page else None,
            }
            if record and _browser_reuse_enabled():
                close_browser_sessions(_browser_session_key(job))
            return finalize(result)

        begin_stage("language_selection")
        try:
            language_selection = _select_cassette_language(page, job)
            record["language_selection"] = language_selection
            job["language_selection"] = language_selection
            job["cassette_language"] = language_selection.get("language") or _language_for_job(job)
            jobs.update_job(
                job_id,
                cassette_language=job["cassette_language"],
                language_selection=language_selection,
            )
            finish_stage("language_selection", str(language_selection.get("status") or "selected"))
        except Exception as exc:
            finish_stage("language_selection", "failed")
            errors.append(
                {"code": "language_selection_failed", "message": str(exc), "details": {"type": type(exc).__name__}}
            )
            return finalize(
                {
                    "status": "failed",
                    "outputs": outputs,
                    "questions": questions,
                    "errors": errors,
                    "quality": {"completion_observed": False, "output_link_count": len(outputs), "risk": "high"},
                    "final_screenshot": _screenshot(page, job_id),
                }
            )

        asset_paths = [p for p in job.get("asset_paths", []) if Path(p).exists()]
        if asset_paths:
            upload_paths = _asset_paths_needing_upload(record, asset_paths)
            upload_ready_expected_count = _upload_ready_expected_count(upload_paths)
            if not upload_paths:
                begin_stage("upload")
                record["asset_fingerprint"] = _asset_fingerprint(asset_paths)
                _record_stage_progress(
                    job_id,
                    f"Cassette session already has {len(asset_paths)} uploaded asset(s); skipping upload.",
                    outputs,
                    "running",
                    stage="upload",
                    stage_elapsed_sec=0.0,
                    attempt=0,
                )
                finish_stage("upload", "skipped")
            else:
                begin_stage("upload")
                try:
                    _upload_assets(page, upload_paths, upload_selector)
                except Exception as exc:
                    finish_stage("upload", "failed")
                    errors.append(
                        {"code": "asset_upload_failed", "message": str(exc), "details": {"type": type(exc).__name__}}
                    )
                    return finalize(
                        {
                            "status": "failed",
                            "outputs": outputs,
                            "questions": questions,
                            "errors": errors,
                            "quality": {"completion_observed": False, "output_link_count": 0, "risk": "high"},
                            "final_screenshot": _screenshot(page, job_id),
                        }
                    )
                try:
                    page.wait_for_load_state("networkidle", timeout=30000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    body = _wait_for_agent_upload_ready(
                        page, job_id, upload_ready_expected_count, _upload_timeout_sec(job)
                    )
                    _mark_uploaded_assets(record, upload_paths, asset_paths)
                    _record_stage_progress(
                        job_id,
                        body,
                        outputs,
                        "running",
                        stage="upload",
                        stage_elapsed_sec=stage_elapsed("upload"),
                        attempt=int(stage_timings["upload"].get("attempts") or 1),
                    )
                    finish_stage("upload", "succeeded")
                except BrowserJobCancelled as exc:
                    finish_stage("upload", "cancelled")
                    return finalize(
                        {
                            "status": "cancelled",
                            "outputs": outputs,
                            "questions": questions,
                            "errors": [],
                            "quality": {
                                "completion_observed": False,
                                "output_link_count": 0,
                                "progress_summary": str(exc),
                                "risk": "medium",
                            },
                            "final_screenshot": _screenshot(page, job_id),
                        }
                    )
                except BrowserUploadTimeoutError as exc:
                    finish_stage("upload", "timed_out")
                    errors.append(
                        {"code": "asset_upload_timeout", "message": str(exc), "details": {"type": type(exc).__name__}}
                    )
                    return finalize(
                        {
                            "status": "timed_out",
                            "outputs": outputs,
                            "questions": questions,
                            "errors": errors,
                            "quality": {
                                "completion_observed": False,
                                "output_link_count": 0,
                                "progress_summary": str(exc),
                                "risk": "medium",
                            },
                            "final_screenshot": _screenshot(page, job_id),
                        }
                    )
                except Exception as exc:
                    finish_stage("upload", "failed")
                    errors.append(
                        {"code": "asset_upload_failed", "message": str(exc), "details": {"type": type(exc).__name__}}
                    )
                    return finalize(
                        {
                            "status": "failed",
                            "outputs": outputs,
                            "questions": questions,
                            "errors": errors,
                            "quality": {},
                            "final_screenshot": _screenshot(page, job_id),
                        }
                    )

        begin_stage("model_selection")
        try:
            if _record_has_model_selection(record) and _record_model_selection_matches(record, job):
                model_selection_result = _reuse_model_selection(record, job)
            else:
                if _record_has_model_selection(record) and not _record_model_selection_matches(record, job):
                    if not _reset_to_new_chat_preserving_assets(
                        page,
                        job_id,
                        len(asset_paths),
                        outputs,
                        int(stage_timings["model_selection"].get("attempts") or 1),
                        stage_elapsed("model_selection"),
                        "model_selection_changed",
                        stage="model_selection",
                        require_ready_assets=False,
                    ):
                        raise RuntimeError("Failed to start a new Cassette chat before changing model")
                model_selection_result = _select_cassette_model(page, job)
                record["model_selection"] = model_selection_result
            job["model_selection"] = model_selection_result
            jobs.update_job(job_id, model_selection=model_selection_result)
            if model_selection_result.get("reason") != "session_reuse":
                notification = notifier.notify_model_selection(job)
                jobs.update_job(job_id, model_selection_notification=notification)
            finish_stage("model_selection", model_selection_result.get("status") or "selected")
        except Exception as exc:
            finish_stage("model_selection", "failed")
            errors.append(
                {"code": "model_selection_failed", "message": str(exc), "details": {"type": type(exc).__name__}}
            )
            return finalize(
                {
                    "status": "failed",
                    "outputs": outputs,
                    "questions": questions,
                    "errors": errors,
                    "quality": {"completion_observed": False, "output_link_count": len(outputs), "risk": "high"},
                    "final_screenshot": _screenshot(page, job_id),
                }
            )

        begin_stage("agent")
        baseline_assistant_count = _assistant_message_count(page)
        baseline_assistant_text = _assistant_message_text(page)
        try:
            baseline_body = page.locator("body").inner_text(timeout=1000)
        except Exception:
            baseline_body = ""
        try:
            _send_chat_message(page, chat_selector, send_selector, _chat_message_for_job(job))
        except Exception as exc:
            finish_stage("agent", "failed")
            errors.append({"code": "chat_input_failed", "message": str(exc), "details": {"type": type(exc).__name__}})
            result = {
                "status": "failed",
                "outputs": outputs,
                "questions": questions,
                "errors": errors,
                "quality": {"completion_observed": False, "output_link_count": len(outputs), "risk": "high"},
                "final_screenshot": _screenshot(page, job_id),
            }
            discard_reusable_browser_session()
            return finalize(result)
        if not _wait_for_chat_submission_activity(
            page, baseline_body, baseline_assistant_count, baseline_assistant_text
        ):
            _record_stage_progress(
                job_id,
                "Cassette chat submission showed no visible activity; retrying once.",
                outputs,
                stage="agent",
                stage_elapsed_sec=stage_elapsed("agent"),
                operation_status="chat_submit_retry",
            )
            try:
                _send_chat_message(page, chat_selector, send_selector, _chat_message_for_job(job))
            except Exception as exc:
                finish_stage("agent", "failed")
                errors.append(
                    {"code": "chat_input_failed", "message": str(exc), "details": {"type": type(exc).__name__}}
                )
                result = {
                    "status": "failed",
                    "outputs": outputs,
                    "questions": questions,
                    "errors": errors,
                    "quality": {"completion_observed": False, "output_link_count": len(outputs), "risk": "high"},
                    "final_screenshot": _screenshot(page, job_id),
                }
                discard_reusable_browser_session()
                return finalize(result)
            if not _wait_for_chat_submission_activity(
                page, baseline_body, baseline_assistant_count, baseline_assistant_text
            ):
                finish_stage("agent", "failed")
                errors.append(
                    {
                        "code": "chat_submit_failed",
                        "message": "Cassette chat message was filled but the page showed no agent activity after submit.",
                    }
                )
                try:
                    final_body = page.locator("body").inner_text(timeout=1000)
                except Exception:
                    final_body = ""
                result = {
                    "status": "failed",
                    "outputs": outputs,
                    "questions": questions,
                    "errors": errors,
                    "quality": {
                        "completion_observed": False,
                        "output_link_count": len(outputs),
                        "progress_summary": _summarize_page_state(final_body),
                        "risk": "high",
                    },
                    "final_screenshot": _screenshot(page, job_id),
                }
                return finalize(result, "agent", "failed")
        _record_stage_progress(
            job_id,
            "Cassette chat request submitted; waiting for agent response.",
            outputs,
            stage="agent",
            stage_elapsed_sec=stage_elapsed("agent"),
            operation_status="chat_submitted",
        )

        start = time.monotonic()
        seen_text = ""
        last_progress_at = 0.0
        snapshot_interval = _progress_snapshot_interval_sec()
        last_snapshot_at = time.monotonic()
        last_activity_summary = ""
        last_routine_interaction_key = ""
        last_routine_interaction_at = 0.0
        agent_retry_count = 0
        while (time.monotonic() - start) * 1000 < timeout_ms:
            if jobs.is_cancel_requested(job_id):
                stop_result = _click_cassette_stop_control(page)
                stop_summary = "Cassette agent stop requested by /cut." + (
                    f" Clicked stop control: {stop_result.get('label')}."
                    if stop_result.get("clicked")
                    else " No visible stop control was available."
                )
                _record_stage_progress(
                    job_id,
                    stop_summary,
                    outputs,
                    stage="agent",
                    stage_elapsed_sec=stage_elapsed("agent"),
                    operation_status="cancel_requested",
                )
                return finalize(
                    {
                        "status": "cancelled",
                        "outputs": outputs,
                        "questions": questions,
                        "errors": errors,
                        "quality": {
                            "completion_observed": False,
                            "output_link_count": len(outputs),
                            "progress_summary": stop_summary,
                            "risk": "medium",
                        },
                        "final_screenshot": _screenshot(page, job_id),
                    },
                    "agent",
                    "cancelled",
                )
            if _page_is_closed(page):
                errors.append(
                    {"code": "cassette_page_closed", "message": "Cassette browser page closed during agent stage"}
                )
                discard_reusable_browser_session()
                return finalize(
                    {
                        "status": "failed",
                        "outputs": outputs,
                        "questions": questions,
                        "errors": errors,
                        "quality": {"completion_observed": False, "output_link_count": len(outputs), "risk": "high"},
                        "final_screenshot": None,
                    },
                    "agent",
                    "failed",
                )
            assistant_text, assistant_is_current = _current_assistant_message_text(
                page,
                baseline_assistant_count,
                baseline_assistant_text,
            )
            body = page.locator("body").inner_text(timeout=1000)
            body_changed_after_send = body != baseline_body
            current_response_observed = assistant_is_current or (
                baseline_assistant_count == 0 and body_changed_after_send
            )
            page_completion_allowed = assistant_is_current or baseline_assistant_count == 0
            if current_response_observed and _visible(page, done_selector):
                outputs = _collect_outputs(page, output_selector)
                finish_stage("agent", "succeeded")
                return finalize(
                    _success_result(
                        page,
                        job,
                        outputs,
                        questions,
                        errors,
                        {
                            "visual_verification": "final screenshot captured; rendered media content is not semantically graded by this plugin",
                        },
                        output_selector,
                        stage_control,
                    )
                )
            outputs = _collect_outputs(page, output_selector)
            page_state = _cassette_page_state(
                page,
                body,
                assistant_text,
                outputs,
                assistant_is_current=assistant_is_current,
                current_response_observed=current_response_observed,
                page_completion_allowed=page_completion_allowed,
            )
            hard_error = _cassette_hard_error(page_state)
            if hard_error:
                if agent_retry_count < _agent_soft_retry_limit():
                    next_attempt = agent_retry_count + 2
                    if _soft_retry_with_new_chat(
                        page,
                        job,
                        chat_selector,
                        send_selector,
                        len(asset_paths),
                        outputs,
                        next_attempt,
                        stage_elapsed("agent"),
                        str(hard_error.get("code") or "cassette_error"),
                    ):
                        agent_retry_count += 1
                        mark_stage_attempt("agent", next_attempt)
                        seen_text = ""
                        last_progress_at = 0.0
                        last_activity_summary = ""
                        last_routine_interaction_key = ""
                        last_routine_interaction_at = 0.0
                        start = time.monotonic()
                        time.sleep(1)
                        continue
                errors.append(hard_error)
                result = {
                    "status": "failed",
                    "outputs": outputs,
                    "questions": questions,
                    "errors": errors,
                    "quality": {
                        "completion_observed": False,
                        "output_link_count": len(outputs),
                        "progress_summary": hard_error["message"],
                        "risk": "high",
                    },
                    "final_screenshot": _screenshot(page, job_id),
                }
                if not _reset_to_new_chat_preserving_assets(
                    page,
                    job_id,
                    len(asset_paths),
                    outputs,
                    agent_retry_count + 1,
                    stage_elapsed("agent"),
                    str(hard_error.get("code") or "cassette_error"),
                ):
                    discard_reusable_browser_session()
                return finalize(result, "agent", "failed")
            now = time.monotonic()
            activity_summary = _summarize_page_state(assistant_text or body)
            if activity_summary and activity_summary != last_activity_summary:
                last_activity_summary = activity_summary
            if now - last_progress_at >= int(os.getenv("CASSETTE_PROGRESS_INTERVAL_SEC", "30")):
                _record_stage_progress(
                    job_id,
                    body,
                    outputs,
                    stage="agent",
                    stage_elapsed_sec=stage_elapsed("agent"),
                    attempt=agent_retry_count + 1,
                )
                last_progress_at = now
            if snapshot_interval and now - last_snapshot_at >= snapshot_interval:
                send_progress_snapshot(activity_summary)
                last_snapshot_at = now
            if _chat_indicates_missing_assets(body, assistant_text):
                missing_asset = assistant_text or _summarize_page_state(body)
                questions.append(
                    {
                        "question": _compact_summary_text(missing_asset, 500),
                        "requires_user": True,
                        "reason": "missing_required_asset",
                        "answer": "Cassette needs uploaded media before it can continue.",
                    }
                )
                return finalize(
                    {
                        "status": "needs_user",
                        "outputs": outputs,
                        "questions": questions,
                        "errors": errors,
                        "quality": {
                            "completion_observed": False,
                            "output_link_count": len(outputs),
                            "progress_summary": _summarize_page_state(assistant_text or body),
                            "risk": "medium",
                        },
                        "final_screenshot": _screenshot(page, job_id),
                    },
                    "agent",
                    "needs_user",
                )
            if _page_state_indicates_routine_interaction(page_state):
                routine_key = _routine_interaction_key(body, assistant_text)
                if routine_key and (
                    routine_key != last_routine_interaction_key or now - last_routine_interaction_at >= 60
                ):
                    clicked = _click_routine_interaction_control(page)
                    if clicked:
                        questions.append(
                            {
                                "question": _compact_summary_text(assistant_text or body, 500),
                                "requires_user": False,
                                "reason": "routine_plan_approval",
                                "answer": f"Clicked Cassette routine action control: {clicked}",
                            }
                        )
                        _record_stage_progress(job_id, f"Cassette routine interaction handled: {clicked}", outputs)
                        last_routine_interaction_key = routine_key
                        last_routine_interaction_at = now
                        seen_text = body
                        time.sleep(1)
                        continue
            if _page_state_requires_completion_review(page_state, export_required=_export_on_complete(job)):
                assistant_summary = _compact_summary_text(assistant_text or body, 700)
                questions.append(
                    {
                        "question": assistant_summary,
                        "requires_user": False,
                        "reason": "completion_requires_hermes_review",
                        "answer": (
                            "The latest Cassette reply needs Hermes supervisor semantic review before deciding whether "
                            "to export, continue, fail, or ask the user."
                        ),
                    }
                )
                return finalize(
                    {
                        "status": "needs_user",
                        "outputs": outputs,
                        "questions": questions,
                        "errors": errors,
                        "quality": {
                            "completion_observed": False,
                            "output_link_count": len(outputs),
                            "progress_summary": assistant_summary,
                            "risk": "medium",
                            "completion_review_required": True,
                        },
                        "final_screenshot": _screenshot(page, job_id),
                    },
                    "agent",
                    "needs_user",
                )
            if outputs and _page_state_indicates_complete(page_state, export_required=_export_on_complete(job)):
                finish_stage("agent", "succeeded")
                return finalize(
                    _success_result(page, job, outputs, questions, errors, {}, output_selector, stage_control)
                )
            if _page_state_indicates_complete(page_state, export_required=_export_on_complete(job)) and (
                assistant_text or not _agent_is_busy(page, body)
            ):
                finish_stage("agent", "succeeded")
                return finalize(
                    _success_result(
                        page,
                        job,
                        outputs,
                        questions,
                        errors,
                        {
                            "completion_source": "cassette_chat_panel",
                            "progress_summary": _summarize_page_state(assistant_text or body),
                        },
                        output_selector,
                        stage_control,
                    )
                )
            question_text = assistant_text or body[-1000:]
            if body != seen_text and ("?" in question_text or "？" in question_text):
                classification = classify_cassette_question(question_text[-1000:], {"job_id": job_id})
                questions.append({"question": question_text[-1000:], **classification})
                if classification["requires_user"]:
                    return finalize(
                        {
                            "status": "needs_user",
                            "outputs": outputs,
                            "questions": questions,
                            "errors": errors,
                            "quality": {
                                "completion_observed": False,
                                "output_link_count": len(outputs),
                                "risk": "medium",
                            },
                            "final_screenshot": _screenshot(page, job_id),
                        },
                        "agent",
                        "needs_user",
                    )
                try:
                    _send_chat_message(page, chat_selector, send_selector, classification["answer"])
                except Exception:
                    pass
            seen_text = body
            time.sleep(2)
        errors.append({"code": "cassette_timeout", "message": "Timed out waiting for Cassette completion"})
        return finalize(
            {
                "status": "timed_out",
                "outputs": outputs,
                "questions": questions,
                "errors": errors,
                "quality": {
                    "completion_observed": False,
                    "output_link_count": len(outputs),
                    "progress_summary": _summarize_page_state(seen_text),
                    "risk": "medium" if seen_text else "high",
                },
                "final_screenshot": _screenshot(page, job_id),
            },
            "agent",
            "timed_out",
        )
    except Exception as exc:
        console_messages = list((record or {}).get("console_messages") or [])
        errors.append(
            {
                "code": "internal_error",
                "message": str(exc),
                "details": {"type": type(exc).__name__, "console": console_messages[-5:]},
            }
        )
        result = {
            "status": "failed",
            "outputs": outputs,
            "questions": questions,
            "errors": errors,
            "quality": {"completion_observed": False, "output_link_count": len(outputs), "risk": "high"},
            "final_screenshot": _screenshot(page, job_id) if page else None,
        }
        discard_reusable_browser_session()
        return finalize(result, current_stage, "failed")
    finally:
        if record and close_browser_on_terminal_requested():
            close_current_browser_session()
        elif record and not _browser_reuse_enabled() and created_for_one_shot:
            _close_browser_record(record)
