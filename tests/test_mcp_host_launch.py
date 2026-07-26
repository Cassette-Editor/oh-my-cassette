"""Launch the exact commands written in each host config file.

test_mcp_protocol.py covers the runtime through hand-built launch parameters;
these tests instead parse .mcp.json, .claude-plugin/mcp.json,
.codex-plugin/mcp.json and opencode.json as data, resolve variables the way each
host does, and complete a real initialize + tools/list over stdio. A config that
points at a missing script, globs an absent cache, or uses the wrong shape fails
here before it fails inside Claude, Codex or opencode.

Each launch also probes the credential-less error envelope, because the setup and
reset commands the plugin tells a user to run are resolved per host and are worth
proving runnable on the host that would print them.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shlex
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


def _handshake(params: StdioServerParameters) -> tuple[set[str], dict]:
    """Initialize, list tools, and read back the recovery hints this host would print.

    _environment() already strips every credential variable, so the launch is credential-less
    and cassette_run_job answers auth_required. It checks auth before it validates session_id,
    so a bare prompt with no session is a side-effect-free probe: no session, no media, no
    network. Both are gathered in one launch because starting the server is the slow part.
    """

    async def exercise():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "cassette"
                listed = await session.list_tools()
                probe = await session.call_tool("cassette_run_job", {"prompt": "edit"})
                error = probe.structuredContent["error"]
                assert error["code"] == "auth_required"
                return {tool.name for tool in listed.tools}, error["details"]

    return asyncio.run(exercise())


def _assert_recovery_commands_run_on_this_host(details: dict) -> None:
    """The commands a host prints must be runnable on that host.

    Each one resolves the plugin tree differently — Claude from its plugin root, Codex from a
    globbed cache, opencode from wherever its installer landed — and run_local_mcp.py rewrites
    CASSETTE_MCP_SETUP_COMMAND to match. A well-formed string is therefore not enough; the
    script it names has to actually exist in the tree that was launched.
    """
    for key, flag in (("setup_command", None), ("reset_password_command", "--reset-password")):
        parts = shlex.split(details[key])
        if flag is not None:
            assert parts[-1] == flag, f"{key} must end with {flag}: {details[key]}"
            parts = parts[:-1]
        script = Path(parts[-1])
        assert script.name == "setup_local_mcp.py", f"{key} does not name the setup script: {details[key]}"
        assert script.is_file(), f"{key} names a script that does not exist: {script}"


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
    tools, details = _handshake(params)
    assert tools == EXPECTED_TOOLS
    _assert_recovery_commands_run_on_this_host(details)


def test_claude_project_config_launches_the_checkout_server(tmp_path):
    server = json.loads((ROOT / ".mcp.json").read_text("utf-8"))["mcpServers"]["cassette"]
    # CLAUDE_PROJECT_DIR is set only in the spawned server's environment, so
    # Claude expands ${CLAUDE_PROJECT_DIR:-.} to "." and relies on the project
    # working directory — reproduce exactly that.
    args = [argument.replace("${CLAUDE_PROJECT_DIR:-.}", ".") for argument in server["args"]]
    environment = _environment(tmp_path)
    environment.update(server.get("env", {}))
    params = StdioServerParameters(command=server["command"], args=args, cwd=str(ROOT), env=environment)
    tools, details = _handshake(params)
    assert tools == EXPECTED_TOOLS
    _assert_recovery_commands_run_on_this_host(details)


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
    tools, details = _handshake(params)
    assert tools == EXPECTED_TOOLS
    _assert_recovery_commands_run_on_this_host(details)


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


def test_opencode_project_config_launches_the_checkout_server(tmp_path):
    server = json.loads((ROOT / "opencode.json").read_text("utf-8"))["mcp"]["cassette"]
    # opencode's shape differs from the others: command is a list, the env key is
    # "environment", and the script path is repo-relative, so cwd has to be the checkout.
    command, *args = server["command"]
    environment = _environment(tmp_path)
    environment.update(server.get("environment", {}))
    params = StdioServerParameters(command=command, args=args, cwd=str(ROOT), env=environment)
    tools, details = _handshake(params)
    assert tools == EXPECTED_TOOLS
    _assert_recovery_commands_run_on_this_host(details)


def test_opencode_installer_entry_launches_from_an_absolute_path(tmp_path):
    # Nearly every opencode user runs the installer rather than the repo config, and the
    # installer writes an absolute command into ~/.config/opencode. test_mcp_packaging.py
    # only compares that entry structurally; this actually starts it.
    spec = importlib.util.spec_from_file_location("install_opencode", ROOT / "scripts" / "install_opencode.py")
    installer = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(installer)
    # The installer defers its repo imports so it can run piped over stdin, with no tree to
    # import from yet; resolve them the way the packaging tests do.
    installer._load_repo_modules(ROOT)

    entry = installer.mcp_server_entry(ROOT)
    command, *args = entry["command"]
    project = tmp_path / "project"
    project.mkdir()
    environment = _environment(tmp_path)
    environment.update(entry.get("environment", {}))
    params = StdioServerParameters(command=command, args=args, cwd=str(project), env=environment)
    tools, details = _handshake(params)
    assert tools == EXPECTED_TOOLS
    _assert_recovery_commands_run_on_this_host(details)
