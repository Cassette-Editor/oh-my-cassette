#!/usr/bin/env python3
"""Credential-free diagnostics for the local Codex/Claude MCP installation."""

from __future__ import annotations

import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(PLUGIN_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import runtime_config  # noqa: E402
from local_mcp_bootstrap import BootstrapError, locked_version, select_python  # noqa: E402


def _mcp_profile() -> dict:
    """The protocol ceiling this installation can offer, without needing a live client.

    A host only ever negotiates down to this number, so a stale lock shows up here as a
    revision below what the host offered -- the failure that is otherwise invisible.
    """
    profile: dict[str, object] = {"locked": locked_version(PLUGIN_ROOT / "requirements-mcp.lock", "mcp")}
    try:
        import importlib.metadata

        from mcp import types

        profile["installed"] = importlib.metadata.version("mcp")
        profile["max_protocol"] = types.LATEST_PROTOCOL_VERSION
    except Exception:  # noqa: BLE001 — the host python need not carry the runtime deps
        profile["note"] = "run this with the plugin-managed runtime python to report the protocol ceiling"
    return profile


def diagnose() -> dict:
    checks: dict[str, object] = {
        "plugin_root": str(PLUGIN_ROOT),
        "platform": sys.platform,
        "config_root": str(runtime_config.config_root()),
        "data_root": str(runtime_config.data_root()),
        "mcp": _mcp_profile(),
    }
    try:
        executable, version = select_python()
        checks["python"] = {"ok": True, "executable": executable, "version": ".".join(map(str, version))}
    except BootstrapError as exc:
        checks["python"] = {"ok": False, "message": str(exc)}
    try:
        credentials = runtime_config.load_credentials()
        checks["authentication"] = {
            "configured": bool(credentials.get("email") and credentials.get("password")),
            "source": credentials.get("source"),
        }
    except runtime_config.RuntimeConfigError as exc:
        checks["authentication"] = {"configured": False, "code": exc.code, "path": str(exc.path or "")}
    try:
        runtime_config.load_settings()
        checks["configured_media_root_count"] = len(runtime_config.configured_media_roots())
    except runtime_config.RuntimeConfigError as exc:
        checks["settings"] = {"ok": False, "code": exc.code, "path": str(exc.path or "")}
    leftover = runtime_config.data_root() / "browsers"
    if leftover.exists():
        # Chromium the retired browser transport downloaded. Reported rather than deleted:
        # it is hundreds of MB, and reclaiming disk is the user's call, not an upgrade's.
        checks["retired_browser_cache"] = {"path": str(leftover), "note": "safe to delete"}
    return checks


if __name__ == "__main__":
    print(json.dumps(diagnose(), ensure_ascii=False, indent=2, sort_keys=True))
