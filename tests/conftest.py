from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from tests.reference_remote import REFERENCE_TOKEN, ReferenceRemote, serve_remote

from oh_my_cassette.bridge.settings import BridgeSettings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(config, items):
    if os.getenv("RUN_CASSETTE_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="set RUN_CASSETTE_LIVE=1 with a Cassette MCP service at CASSETTE_MCP_URL")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
async def remote() -> AsyncIterator[ReferenceRemote]:
    """The reference backend on a real loopback port; `remote.url` is its MCP endpoint."""
    reference = ReferenceRemote()
    async with serve_remote(reference):
        yield reference


@pytest.fixture
def bridge_settings(tmp_path: Path) -> Callable[..., BridgeSettings]:
    """Settings for a bridge in a fresh workspace, holding the reference backend's token."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def make(url: str, **overrides) -> BridgeSettings:
        values = {
            "auth_token": REFERENCE_TOKEN,
            "connect_timeout_sec": 5,
            "temp_dir": tmp_path / "bridge-temp",
            **overrides,
        }
        return BridgeSettings(mcp_url=url, workspace=workspace.resolve(), **values)

    return make
