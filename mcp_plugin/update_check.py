"""Release-channel staleness check for the local MCP runtime.

Only Claude Code updates plugins on its own, and only once the user enables
auto-update for the marketplace. Codex refreshes its marketplace snapshot but
never materializes a new plugin version, and the OpenCode install is ours to
manage — so the runtime tells the host when a newer release exists and which
single command applies it.

Nothing here may raise or block: ``notice()`` reads a cache file, ``refresh()``
does one short HTTP GET at most once a day, and both swallow every failure.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import runtime_config

from .core_loader import PLUGIN_ROOT

LATEST_URL = "https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/release/version.txt"
TTL_SECONDS = 86_400
FETCH_TIMEOUT_SEC = 3.0
MAX_VERSION_BYTES = 64

# Keyed by CASSETTE_MCP_HOST, which every host config sets (.claude-plugin/mcp.json,
# .codex-plugin/mcp.json, opencode.json). Hermes never launches this server.
UPDATE_COMMANDS = {
    "claude": (
        "claude plugin marketplace update cassette-editor && claude plugin update oh-my-cassette@cassette-editor"
    ),
    "codex": "codex plugin add oh-my-cassette@cassette-editor",
    "opencode": (
        "curl -fsSL https://raw.githubusercontent.com/Cassette-Editor/oh-my-cassette/"
        "release/scripts/install_opencode.py | python3 -"
    ),
}
FALLBACK_COMMAND = "reinstall from the release channel: https://github.com/Cassette-Editor/oh-my-cassette#update"


def enabled() -> bool:
    return str(os.getenv("CASSETTE_UPDATE_CHECK", "") or "").strip() not in {"0", "false", "no"}


def installed_version() -> str:
    try:
        return (PLUGIN_ROOT / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def update_command(host: str | None = None) -> str:
    key = (host if host is not None else os.getenv("CASSETTE_MCP_HOST", "")).strip().lower()
    return UPDATE_COMMANDS.get(key, FALLBACK_COMMAND)


def _cache_path() -> Path:
    return runtime_config.data_root() / "update-check.json"


def _read_cache() -> dict[str, Any]:
    try:
        return runtime_config.read_protected_json(_cache_path())
    except Exception:  # noqa: BLE001 — a missing, unreadable, or hostile cache is not an error here
        return {}


def _parse(version: str) -> tuple[int, ...] | None:
    parts = version.strip().split(".")
    if not 1 <= len(parts) <= 4:
        return None
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def is_newer(latest: str, installed: str) -> bool:
    parsed_latest = _parse(latest)
    parsed_installed = _parse(installed)
    if parsed_latest is None or parsed_installed is None:
        return False
    return parsed_latest > parsed_installed


def notice(host: str | None = None) -> str:
    """One sentence for the MCP instructions, or "" when current. Never fetches."""
    if not enabled():
        return ""
    installed = installed_version()
    latest = str(_read_cache().get("latest") or "").strip()
    if not installed or not latest or not is_newer(latest, installed):
        return ""
    return (
        f" UPDATE AVAILABLE: Oh My Cassette {latest} was released; this install is {installed}. "
        "Mention it once per session, then offer to run this command for the user and run it only "
        f"if they agree: {update_command(host)}"
    )


def _fetch_latest() -> str:
    request = Request(LATEST_URL, headers={"User-Agent": "oh-my-cassette/1.0"})
    with urlopen(request, timeout=FETCH_TIMEOUT_SEC) as response:  # noqa: S310 — constant https URL
        return response.read(MAX_VERSION_BYTES).decode("utf-8", "replace").strip()


def refresh() -> None:
    """Refresh the cached release version at most once a day. Call off the hot path."""
    if not enabled():
        return
    try:
        cache = _read_cache()
        checked_at = float(cache.get("checked_at") or 0)
        if time.time() - checked_at < TTL_SECONDS:
            return
        # Normalize here, not only in the fetch: the cached string is pasted verbatim
        # into the MCP instructions, so a stray newline must never reach it.
        latest = _fetch_latest().strip()
        if _parse(latest) is None:
            return
        runtime_config.write_protected_json(_cache_path(), {"latest": latest, "checked_at": time.time()})
    except Exception:  # noqa: BLE001 — an update check must never break an edit
        return
