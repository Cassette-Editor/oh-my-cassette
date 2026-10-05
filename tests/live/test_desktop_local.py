"""The stdio host, local discovery and OAuth Agent grant against an open Desktop profile."""

from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

import anyio
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from oh_my_cassette.auth import desktop_discovery, target_resource

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_local_desktop_stdio_discovery_and_project_read(tmp_path):
    discovery = desktop_discovery()
    resource, subject = target_resource("desktop")
    assert subject == str(discovery.subject)
    assert resource == discovery.resource
    assert urlsplit(resource).hostname == "127.0.0.1"
    assert urlsplit(discovery.issuer).hostname == "127.0.0.1"
    assert os.environ["CASSETTE_AUTH_TOKEN"]
    project_id = os.environ["CASSETTE_EXPECTED_PROJECT"]
    with anyio.fail_after(60):
        async with Client(
            StdioServerParameters(
                command=sys.executable,
                args=["-m", "oh_my_cassette"],
                env={**os.environ, "CASSETTE_MCP_URL": resource, "CASSETTE_WORKSPACE": str(tmp_path)},
                cwd=str(tmp_path),
            )
        ) as host:
            names = {tool.name for tool in (await host.list_tools()).tools}
            assert {"cassette_projects", "cassette_project_open"} <= names
            projects = await host.call_tool("cassette_projects", {})
            assert not projects.is_error
            assert any(
                project["project_id"] == project_id for project in projects.structured_content["projects"]
            )
            opened = await host.call_tool("cassette_project_open", {"project_id": project_id})
            assert not opened.is_error
            assert opened.structured_content["project_id"] == project_id
