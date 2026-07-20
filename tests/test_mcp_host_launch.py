"""Launch the exact commands written in each host config file.

test_mcp_protocol.py covers the runtime through hand-built launch parameters;
these tests instead parse .mcp.json, .claude-plugin/mcp.json, and
.codex-plugin/mcp.json as data, resolve variables the way each host does, and
complete a real initialize + tools/list over stdio. A config that points at a
missing script, globs an absent cache, or uses the wrong shape fails here
before it fails inside Claude or Codex.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from test_mcp_protocol import EXPECTED_TOOLS


ROOT = Path(__file__).resolve().parents[1]


def _environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "CASSETTE_AUTH_EMAIL",
        "CASSETTE_AUTH_ACCOUNT",
        "CASSETTE_EMAIL",
        "CASSETTE_AUTH_PASSWORD",
        "CASSETTE_PASSWORD",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "CASSETTE_CONFIG_HOME": str(tmp_path / "config"),
            "CASSETTE_DATA_HOME": str(tmp_path / "data"),
            "CASSETTE_TRANSPORT": "api",
            "CASSETTE_MCP_SKIP_BOOTSTRAP": "1",
            "CASSETTE_MCP_PYTHON": sys.executable,
        }
    )
    return environment


def _handshake(params: StdioServerParameters) -> set[str]:
    async def exercise():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "cassette"
                listed = await session.list_tools()
                return {tool.name for tool in listed.tools}

    return asyncio.run(exercise())


def test_claude_plugin_config_launches_the_packaged_server(tmp_path):
    server = json.loads((ROOT / ".claude-plugin" / "mcp.json").read_text("utf-8"))["cassette"]
    project = tmp_path / "project"
    project.mkdir()
    substitutions = {"${CLAUDE_PLUGIN_ROOT}": str(ROOT), "${CLAUDE_PROJECT_DIR}": str(project)}

    def expand(value: str) -> str:
        for token, replacement in substitutions.items():
            value = value.replace(token, replacement)
        return value

    environment = _environment(tmp_path)
    environment.update({key: expand(value) for key, value in server.get("env", {}).items()})
    params = StdioServerParameters(
        command=server["command"],
        args=[expand(argument) for argument in server["args"]],
        cwd=str(project),
        env=environment,
    )
    assert _handshake(params) == EXPECTED_TOOLS


def test_claude_project_config_launches_the_checkout_server(tmp_path):
    server = json.loads((ROOT / ".mcp.json").read_text("utf-8"))["mcpServers"]["cassette"]
    # CLAUDE_PROJECT_DIR is set only in the spawned server's environment, so
    # Claude expands ${CLAUDE_PROJECT_DIR:-.} to "." and relies on the project
    # working directory — reproduce exactly that.
    args = [argument.replace("${CLAUDE_PROJECT_DIR:-.}", ".") for argument in server["args"]]
    environment = _environment(tmp_path)
    environment.update(server.get("env", {}))
    params = StdioServerParameters(command=server["command"], args=args, cwd=str(ROOT), env=environment)
    assert _handshake(params) == EXPECTED_TOOLS


def _codex_server() -> dict:
    return json.loads((ROOT / ".codex-plugin" / "mcp.json").read_text("utf-8"))["mcpServers"]["cassette"]


def test_codex_plugin_config_launches_from_the_plugin_cache(tmp_path):
    server = _codex_server()
    cache = tmp_path / "codex-home" / "plugins" / "cache" / "cassette-editor" / "oh-my-cassette"
    cache.mkdir(parents=True)
    (cache / "9.9.9").symlink_to(ROOT, target_is_directory=True)
    project = tmp_path / "project"
    project.mkdir()
    environment = _environment(tmp_path)
    environment["CODEX_HOME"] = str(tmp_path / "codex-home")
    environment.update(server.get("env", {}))
    params = StdioServerParameters(
        command=server["command"],
        args=list(server["args"]),
        cwd=str(project),
        env=environment,
    )
    assert _handshake(params) == EXPECTED_TOOLS


def test_codex_launcher_reports_a_clear_error_when_no_plugin_is_installed(tmp_path):
    server = _codex_server()
    environment = _environment(tmp_path)
    environment["CODEX_HOME"] = str(tmp_path / "empty-codex-home")
    result = subprocess.run(
        [server["command"], *server["args"]],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "no installed plugin copy" in result.stderr
    assert "codex plugin add oh-my-cassette@cassette-editor" in result.stderr
