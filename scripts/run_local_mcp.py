#!/usr/bin/env python3
"""Idempotent entrypoint used by both Codex and Claude plugin manifests."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(PLUGIN_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import runtime_config  # noqa: E402
from local_mcp_bootstrap import BootstrapError, bootstrap_runtime, select_python  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the Oh My Cassette local stdio MCP runtime")
    parser.add_argument("--bootstrap-only", action="store_true", help="Prepare the locked runtime and exit")
    args = parser.parse_args()

    project_dir = Path.cwd().expanduser().resolve()
    environment = os.environ.copy()
    environment["CASSETTE_RUNTIME_ADAPTER"] = "mcp"
    environment.setdefault("CASSETTE_PROJECT_ROOT", str(project_dir))
    environment.setdefault("CASSETTE_MCP_SETUP_COMMAND", runtime_config.setup_command(PLUGIN_ROOT))

    try:
        if str(os.getenv("CASSETTE_MCP_SKIP_BOOTSTRAP", "") or "").lower() in {"1", "true", "yes"}:
            python, _ = select_python()
            python_path = Path(python)
        else:
            python_path = bootstrap_runtime(output=sys.stderr)
    except BootstrapError as exc:
        print(f"oh-my-cassette: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc

    if args.bootstrap_only:
        print(str(python_path))
        return

    os.chdir(PLUGIN_ROOT)
    command = [str(python_path), "-m", "mcp_plugin.server"]
    if sys.platform == "win32":
        # exec* on Windows detaches from the inherited stdio pipes; run as a
        # child that shares them instead and mirror its exit code.
        import subprocess

        raise SystemExit(subprocess.run(command, env=environment).returncode)
    os.execve(str(python_path), command, environment)


if __name__ == "__main__":
    main()
