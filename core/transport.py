"""Cassette job transport.

The plugin reaches Cassette one way: call the Cassette server APIs directly
(auth + media upload + LangGraph agent run + render-from-stored-project export).

A second transport used to exist — a Playwright path that drove the Cassette web UI —
kept on the theory that some accounts could get further through the browser than through
the API. They could not. The server authorizes by endpoint, not by how the request was
made, and the web UI's export button posts to the same endpoints this module calls. Both
paths get the identical answer, so the browser path bought nothing while costing a
Chromium dependency, a second job-result code path, and a restart-fragile session model.

``CASSETTE_TRANSPORT`` is therefore no longer a selector. A stale ``browser`` value is
reported once on stderr and ignored rather than failing every tool call, because the
API path is strictly better for everyone who used to set it.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Protocol, runtime_checkable

TRANSPORT_ENV = "CASSETTE_TRANSPORT"
TRANSPORT_API = "api"

_RETIRED_NOTICE_SHOWN = False


@runtime_checkable
class Transport(Protocol):
    """Operation surface the cassette tools depend on."""

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


def warn_if_browser_requested() -> bool:
    """Report a retired ``CASSETTE_TRANSPORT=browser`` once. Returns whether it was set.

    Stderr, never stdout: under MCP, stdout carries protocol frames only.
    """
    global _RETIRED_NOTICE_SHOWN
    if _read_env(TRANSPORT_ENV).lower() != "browser":
        return False
    if not _RETIRED_NOTICE_SHOWN:
        _RETIRED_NOTICE_SHOWN = True
        print(
            f"oh-my-cassette: {TRANSPORT_ENV}=browser is retired and ignored; "
            "the API transport is used instead. Remove the setting to silence this.",
            file=sys.stderr,
        )
    return True


def selected_transport() -> str:
    """Always the API transport. Retained so callers need not care that the choice is gone."""
    warn_if_browser_requested()
    return TRANSPORT_API


def get_transport() -> Transport:
    """Return the Cassette API transport."""
    warn_if_browser_requested()
    from . import api_transport

    return api_transport.ApiTransport()
