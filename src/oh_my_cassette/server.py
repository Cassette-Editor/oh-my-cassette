"""MCP server entry point (stdio)."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp.server.mcpserver import MCPServer

from oh_my_cassette import __version__
from oh_my_cassette.app import App
from oh_my_cassette.settings import Settings
from oh_my_cassette.tools import export as export_tools
from oh_my_cassette.tools import media as media_tools
from oh_my_cassette.tools import project as project_tools
from oh_my_cassette.tools import run as run_tools
from oh_my_cassette.tools import timeline as timeline_tools

INSTRUCTIONS = """Oh My Cassette drives the Cassette video editor. One project = one editor_url = one running
conversation with the editing agent.

Workflow: cassette_project (bind/create) → cassette_import (local files) → cassette_run per user request →
cassette_timeline to ground any statement about the project → cassette_export only when the user asks to render.

Rules: pass the user's words to cassette_run verbatim; route on the typed `status` field
(completed / not_done / needs_input / aborted / failed / timeout), never on prose; answer needs_input with
cassette_answer; after a host timeout call cassette_status once to re-attach (never poll in a loop); quote
`version_from→version_to` and `editor_url` in replies so the user can open the editor and see the result."""

TOOL_NAMES = (
    "cassette_project",
    "cassette_import",
    "cassette_run",
    "cassette_answer",
    "cassette_status",
    "cassette_stop",
    "cassette_timeline",
    "cassette_history",
    "cassette_export",
)


def build_server(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    cwd: str | None = None,
) -> tuple[MCPServer, App]:
    settings = settings or Settings.from_env()
    app = App(settings, transport=transport, cwd=cwd)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await app.aclose()

    server = MCPServer(
        name="oh-my-cassette",
        title="Oh My Cassette",
        version=__version__,
        instructions=INSTRUCTIONS,
        website_url="https://github.com/Cassette-Editor/oh-my-cassette",
        lifespan=lifespan,
        log_level="WARNING",
    )
    project_tools.register(server, app)
    media_tools.register(server, app)
    run_tools.register(server, app)
    timeline_tools.register(server, app)
    export_tools.register(server, app)
    return server, app


def main() -> None:
    level = os.environ.get("OH_MY_CASSETTE_LOG", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        stream=sys.stderr,
        format="%(name)s %(levelname)s %(message)s",
    )
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"oh-my-cassette {__version__}")
        return
    server, _ = build_server()
    server.run("stdio")


if __name__ == "__main__":
    main()
