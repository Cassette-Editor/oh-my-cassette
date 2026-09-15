"""Launch the console entry point the way hosts do (stdio subprocess) against a uvicorn-hosted fake."""

from __future__ import annotations

import asyncio
import os
import socket
import sys

import pytest
import uvicorn
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from tests.fake_cassette import FakeCassette, make_app

pytestmark = pytest.mark.anyio


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_console_script_over_stdio(tmp_path):
    fake = FakeCassette()
    port = _free_port()
    config = uvicorn.Config(make_app(fake), host="127.0.0.1", port=port, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        env = {
            **os.environ,
            "CASSETTE_API_URL": f"http://127.0.0.1:{port}",
            "CASSETTE_WEB_URL": "http://web.test",
            "OH_MY_CASSETTE_HOME": str(tmp_path / "home"),
            "PYTHONPATH": os.pathsep.join(
                p for p in [os.environ.get("PYTHONPATH", ""), str(tmp_path.parents[0])] if p
            ),
        }
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "oh_my_cassette"], env=env, cwd=str(tmp_path)
        )
        async with Client(params) as client:
            tools = await client.list_tools()
            assert [t.name for t in tools.tools][:2] == ["cassette_project", "cassette_import"]
            created = await client.call_tool("cassette_project", {"action": "create", "title": "stdio"})
            assert created.structured_content["status"] == "ok"
            assert created.structured_content["editor_url"].startswith(
                "http://web.test/try?projectSessionId="
            )
            listed = await client.call_tool("cassette_project", {"action": "list"})
            assert len(listed.structured_content["projects"]) == 1
    finally:
        server.should_exit = True
        await task
