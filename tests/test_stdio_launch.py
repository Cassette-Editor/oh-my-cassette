"""Launch the entry points the way hosts do (stdio subprocess) against backends on real loopback ports."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from tests.reference_remote import REFERENCE_TOKEN, free_port

from oh_my_cassette import __version__
from oh_my_cassette.bridge.server import STATUS_TOOL

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"


def _bridge(workspace: Path, **env: str) -> StdioServerParameters:
    # the bridge must not inherit a developer's real Cassette configuration
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("CASSETTE_")}
    defaults = {
        "CASSETTE_CONNECT_TIMEOUT_SEC": "2",
        "CASSETTE_TEMP_DIR": str(workspace.parent / "bridge-temp"),
    }
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "oh_my_cassette"],
        env={**inherited, **defaults, **env},
        cwd=str(workspace),
    )


async def test_bridge_over_stdio(remote, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    shutil.copy(FIXTURES / "slide.png", workspace / "slide.png")
    async with Client(
        _bridge(workspace, CASSETTE_MCP_URL=remote.url, CASSETTE_AUTH_TOKEN=REFERENCE_TOKEN)
    ) as host:
        assert host.instructions == remote.options.instructions
        tools = [tool.name for tool in (await host.list_tools()).tools]
        assert "import_media" in tools and "cassette_upload_begin" not in tools
        imported = await host.call_tool("import_media", {"paths": ["slide.png"]})
        assert imported.structured_content["refs"][0].startswith("media:")
        exported = await host.call_tool("export_video", {"name": "cut"})
        assert exported.structured_content["file"] == str(
            (workspace / "cassette-exports" / "cut.mp4").resolve()
        )
    assert remote.calls[0]["meta"]["io.github.cassette-editor/workspace"]["name"] == "project"


async def test_bridge_over_stdio_without_a_backend(tmp_path):
    url = f"http://127.0.0.1:{free_port()}/mcp"
    async with Client(_bridge(tmp_path, CASSETTE_MCP_URL=url)) as host:
        assert [tool.name for tool in (await host.list_tools()).tools] == [STATUS_TOOL]
        status = await host.call_tool(STATUS_TOOL, {})
        assert status.structured_content["connected"] is False
        assert status.structured_content["mcp_url"] == url
        assert status.structured_content["error_code"] == "bridge.backend_unreachable"


async def test_bridge_over_stdio_with_a_refused_token(remote, tmp_path):
    async with Client(_bridge(tmp_path, CASSETTE_MCP_URL=remote.url)) as host:
        assert "bridge.unauthorized" in host.instructions
        status = await host.call_tool(STATUS_TOOL, {})
        assert status.structured_content["error_code"] == "bridge.unauthorized"


def test_version_flag():
    result = subprocess.run(
        [sys.executable, "-m", "oh_my_cassette", "--version"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0 and result.stdout.strip() == f"oh-my-cassette {__version__}"
