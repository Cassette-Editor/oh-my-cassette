#!/usr/bin/env python3
"""Credential-free diagnostics for the local Codex/Claude MCP installation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(PLUGIN_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import runtime_config  # noqa: E402
from local_mcp_bootstrap import BootstrapError, select_python  # noqa: E402


def diagnose() -> dict:
    checks: dict[str, object] = {
        "plugin_root": str(PLUGIN_ROOT),
        "platform": sys.platform,
        "config_root": str(runtime_config.config_root()),
        "data_root": str(runtime_config.data_root()),
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
            "full_api_access": credentials.get("full_api_access"),
        }
    except runtime_config.RuntimeConfigError as exc:
        checks["authentication"] = {"configured": False, "code": exc.code, "path": str(exc.path or "")}
    try:
        settings = runtime_config.load_settings()
        checks["transport"] = settings.get("transport") or os.getenv("CASSETTE_TRANSPORT") or "api"
        checks["configured_media_root_count"] = len(runtime_config.configured_media_roots())
    except runtime_config.RuntimeConfigError as exc:
        checks["settings"] = {"ok": False, "code": exc.code, "path": str(exc.path or "")}
    return checks


if __name__ == "__main__":
    print(json.dumps(diagnose(), ensure_ascii=False, indent=2, sort_keys=True))
