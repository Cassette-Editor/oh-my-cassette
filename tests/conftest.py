from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from mcp.client import Client
from tests.fake_cassette import FakeCassette, make_app

from oh_my_cassette.app import App
from oh_my_cassette.server import build_server
from oh_my_cassette.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(config, items):
    if os.getenv("RUN_CASSETTE_LIVE") == "1":
        return
    skip = pytest.mark.skip(
        reason="set RUN_CASSETTE_LIVE=1 with a local Cassette-Editor stack to run live tests"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def fake() -> FakeCassette:
    return FakeCassette()


@pytest.fixture
def transport(fake: FakeCassette) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=make_app(fake))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_url="http://fake.cassette",
        web_url="http://fake.web",
        home=tmp_path / "home",
        run_timeout_sec=20,
        import_ready_timeout_sec=10,
        export_timeout_sec=10,
        http_timeout_sec=10,
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
async def app(settings: Settings, transport: httpx.ASGITransport, workdir: Path) -> AsyncIterator[App]:
    application = App(settings, transport=transport, cwd=str(workdir))
    try:
        yield application
    finally:
        await application.aclose()


@pytest.fixture
async def client(
    settings: Settings, transport: httpx.ASGITransport, workdir: Path
) -> AsyncIterator[tuple[Client, App]]:
    server, application = build_server(settings, transport=transport, cwd=str(workdir))
    async with Client(server) as mcp_client:
        yield mcp_client, application
