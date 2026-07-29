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

import json
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
CLAUDE_MARKETPLACE = "cassette-editor"

# Keyed by CASSETTE_MCP_HOST, which every host config sets (.claude-plugin/mcp.json,
# .codex-plugin/mcp.json, opencode.json). Hermes never launches this server.
UPDATE_COMMANDS = {
    "claude": (
        f"claude plugin marketplace update {CLAUDE_MARKETPLACE} && "
        f"claude plugin update oh-my-cassette@{CLAUDE_MARKETPLACE}"
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


def _claude_settings_path() -> Path:
    base = str(os.getenv("CLAUDE_CONFIG_DIR", "") or "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".claude"
    return root / "settings.json"


def auto_update_notice(host: str | None = None) -> str:
    """One sentence when Claude Code could keep this install current but is not set to.

    `setup_local_mcp.py` offers the opt-in, but only during setup — an install that
    predates that prompt, or one where it was declined, never hears about it again and
    stays pinned to whatever version it was installed at. This is the only host with a
    per-marketplace auto-update toggle, and third-party marketplaces ship with it off.

    Reads Claude Code's own settings file, so `runtime_config.read_protected_json` is
    wrong here: that demands 0600, and this file is theirs (0644) not ours. Never
    raises; a missing, hostile, or unreadable file is silence.
    """
    if not enabled():
        return ""
    key = (host if host is not None else os.getenv("CASSETTE_MCP_HOST", "")).strip().lower()
    if key != "claude":
        return ""
    try:
        path = _claude_settings_path()
        if path.is_symlink() or not path.is_file():
            return ""
        settings = json.loads(path.read_text(encoding="utf-8"))
        marketplaces = settings.get("extraKnownMarketplaces") if isinstance(settings, dict) else None
        entry = marketplaces.get(CLAUDE_MARKETPLACE) if isinstance(marketplaces, dict) else None
        # No entry means Claude Code is not the host or the plugin arrived some other
        # way; the toggle we would be describing does not exist. Say nothing.
        if not isinstance(entry, dict) or entry.get("autoUpdate") is True:
            return ""
    except Exception:  # noqa: BLE001 — a settings file we do not own must never break an edit
        return ""
    return (
        " AUTO-UPDATE IS OFF: Claude Code can keep Oh My Cassette current on its own, but the "
        f"{CLAUDE_MARKETPLACE} marketplace has it disabled, so this install stays on its current "
        "version until updated by hand. Mention this at most once per session, and point the user "
        f"at /plugin > Marketplaces > {CLAUDE_MARKETPLACE} > Enable auto-update — it is a setting "
        "only they can toggle, so do not edit their Claude configuration yourself."
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
