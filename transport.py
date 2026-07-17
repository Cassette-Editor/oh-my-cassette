"""Cassette job transport seam.

The plugin can reach Cassette two ways:

* ``api`` (default) — call the Cassette server APIs directly (auth + media upload +
  LangGraph agent run + render-from-stored-project export), avoiding brittle DOM scraping.
* ``browser`` — drive the Cassette web UI with Playwright. The original, battle-tested
  path; ``browser.py`` is unchanged and ``BrowserTransport`` is a pure pass-through, so
  selecting it is byte-identical to the pre-seam behavior.

Selection is by the ``CASSETTE_TRANSPORT`` env var (``api`` | ``browser``), default
``api``. Set ``CASSETTE_TRANSPORT=browser`` to use the Playwright path instead. The env is
re-read on every ``get_transport()`` call so tests and runtime re-config take effect without
import-time caching. Both transports return the IDENTICAL job-result dict shape
(status / outputs / questions / errors / quality / final_screenshot) so everything downstream
(jobs.save_job, notifier, _scrub_job, _job_report) is unaffected.
"""
from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

TRANSPORT_ENV = "CASSETTE_TRANSPORT"
TRANSPORT_BROWSER = "browser"
TRANSPORT_API = "api"


@runtime_checkable
class Transport(Protocol):
    """Operation surface the cassette tools depend on, regardless of transport."""

    def run_job(self, job: dict) -> dict:
        """Run a Cassette edit job to a terminal state and return the result dict."""
        ...

    def export(self, job: dict, decision: dict[str, Any] | None = None) -> dict:
        """Re-drive/collect the export for an ambiguous-completion review job."""
        ...

    def resume(self, job: dict, response: str) -> dict:
        """Resume an interrupted job using validated user input."""
        ...

    def close_sessions(self, session_key: str | None = None) -> None:
        """Tear down any live session(s) for the given key (or all when None)."""
        ...

    def check_available(self) -> bool:
        """Whether this transport can run in the current environment/config."""
        ...


def _read_env(name: str) -> str:
    # MCP reads the host-neutral protected config; the web demo reads process env only. Hermes keeps
    # its historical ~/.hermes/.env resolution.
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


def selected_transport() -> str:
    # Default: api. Only an explicit CASSETTE_TRANSPORT=browser selects the Playwright path.
    raw = _read_env(TRANSPORT_ENV).lower()
    return TRANSPORT_BROWSER if raw == TRANSPORT_BROWSER else TRANSPORT_API


class BrowserTransport:
    """Pass-through adapter over the existing Playwright ``browser.*`` entrypoints.

    Intentionally a thin delegate: selecting ``browser`` must behave exactly as before the seam.
    ``browser`` is imported lazily so the default API transport never requires Playwright.
    """

    def run_job(self, job: dict) -> dict:
        from . import browser
        return browser.run_cassette_browser_job_threaded(job)

    def export(self, job: dict, decision: dict[str, Any] | None = None) -> dict:
        from . import browser
        return browser.export_reviewed_completion_job_threaded(job, decision)

    def resume(self, job: dict, response: str) -> dict:
        from . import browser
        return browser.resume_cassette_browser_job_threaded(job, response)

    def close_sessions(self, session_key: str | None = None) -> None:
        from . import browser
        browser.close_browser_sessions_threaded(session_key)

    def check_available(self) -> bool:
        from . import browser
        return browser.check_playwright()


def get_transport() -> Transport:
    """Return the transport selected by ``CASSETTE_TRANSPORT`` (default api)."""
    if selected_transport() == TRANSPORT_API:
        from . import api_transport
        return api_transport.ApiTransport()
    return BrowserTransport()
