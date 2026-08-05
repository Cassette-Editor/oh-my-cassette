#!/usr/bin/env python3
"""Install Cassette's shared stdio MCP entry into a Hermes config."""

from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
from typing import Any


NETWORK_KEYS = {
    "url",
    "headers",
    "auth",
    "oauth",
    "transport",
    "skip_preflight",
    "ssl_verify",
    "client_cert",
    "client_key",
}


def merge_cassette_mcp(config: dict[str, Any], plugin_root: Path) -> dict[str, Any]:
    """Return a copy with Cassette routed through the host-neutral MCP runtime."""
    merged = copy.deepcopy(config)
    servers = merged.get("mcp_servers")
    servers = dict(servers) if isinstance(servers, dict) else {}

    previous = servers.get("cassette")
    entry = dict(previous) if isinstance(previous, dict) else {}
    for key in NETWORK_KEYS:
        entry.pop(key, None)

    existing_env = entry.get("env")
    child_env = dict(existing_env) if isinstance(existing_env, dict) else {}
    child_env["CASSETTE_MCP_HOST"] = "hermes"
    # Hermes resolves these placeholders from its private ~/.hermes/.env before spawning the
    # stdio child. Keeping references here avoids duplicating secrets into config.yaml while the
    # shared MCP runtime still receives the credentials already collected by the plugin installer.
    child_env["CASSETTE_AUTH_EMAIL"] = "${CASSETTE_AUTH_EMAIL}"
    child_env["CASSETTE_AUTH_PASSWORD"] = "${CASSETTE_AUTH_PASSWORD}"

    entry.update(
        {
            "command": "python3",
            "args": [str(plugin_root.resolve() / "scripts" / "run_local_mcp.py")],
            "env": child_env,
            "enabled": True,
            "timeout": 1800,
            "connect_timeout": 240,
            "supports_parallel_tool_calls": False,
        }
    )
    servers["cassette"] = entry
    merged["mcp_servers"] = servers
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure Hermes to use Cassette's shared MCP server.")
    parser.add_argument("--plugin-root", required=True, type=Path)
    parser.add_argument("--hermes-home", type=Path)
    args = parser.parse_args()

    plugin_root = args.plugin_root.expanduser().resolve()
    launcher = plugin_root / "scripts" / "run_local_mcp.py"
    if not launcher.is_file():
        parser.error(f"Cassette MCP launcher was not found: {launcher}")

    if args.hermes_home:
        os.environ["HERMES_HOME"] = str(args.hermes_home.expanduser().resolve())

    # The installer launches this helper with Hermes's own virtualenv. Its supported config
    # writer performs an atomic YAML merge without adding PyYAML to the bootstrap installer.
    from hermes_cli.config import load_config, save_config

    save_config(merge_cassette_mcp(load_config(), plugin_root))
    print(f"configured Hermes MCP server 'cassette' -> {launcher}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
