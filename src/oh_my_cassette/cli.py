"""Console entry point: `oh-my-cassette` runs the stdio bridge; `oh-my-cassette check` tests an endpoint."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import anyio
import httpx
from mcp.server.stdio import stdio_server

from oh_my_cassette import __version__
from oh_my_cassette.bridge.server import Bridge
from oh_my_cassette.bridge.settings import BridgeSettings


async def serve_stdio(settings: BridgeSettings) -> None:
    # Uploads and downloads: no overall deadline, only the same idle limit as the MCP connection.
    timeout = httpx.Timeout(settings.read_timeout_sec, connect=settings.connect_timeout_sec)
    async with httpx.AsyncClient(timeout=timeout) as http, anyio.create_task_group() as tg:
        bridge = Bridge(settings, http)
        await tg.start(bridge.run)
        server = bridge.build_server()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
        tg.cancel_scope.cancel()


async def _check(args: argparse.Namespace) -> int:
    from oh_my_cassette.conformance import failed, render, run_checks, summary

    url = args.url or BridgeSettings.from_env().mcp_url
    token = args.token or os.environ.get("CASSETTE_AUTH_TOKEN") or None
    checks = await run_checks(url, token=token, upload=args.upload, arguments=json.loads(args.arguments))
    print(render(checks, as_json=args.json))
    if not args.json:
        counts = summary(checks)
        print(f"\n{url}: {counts['pass']} passed, {counts['warn']} warnings, {counts['fail']} failed")
    return 1 if failed(checks) else 0


def main(argv: list[str] | None = None) -> None:
    level = os.environ.get("OH_MY_CASSETTE_LOG", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        stream=sys.stderr,
        format="%(name)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="oh-my-cassette",
        description="Bridge MCP hosts to the Cassette video editor's MCP service.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"oh-my-cassette {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the stdio bridge (the default)")
    check = commands.add_parser("check", help="check a Cassette MCP endpoint against the bridge contract")
    check.add_argument("--url", help="MCP endpoint (default: CASSETTE_MCP_URL or http://127.0.0.1:8790/mcp)")
    check.add_argument("--token", help="bearer token (default: CASSETTE_AUTH_TOKEN)")
    check.add_argument("--upload", action="store_true", help="also run an upload round trip")
    check.add_argument(
        "--arguments",
        default="{}",
        help="JSON arguments of the file tool for the upload round trip, e.g. a project id",
    )
    check.add_argument("--json", action="store_true", help="print the checks as JSON")
    args = parser.parse_args(argv)

    if args.command == "check":
        sys.exit(anyio.run(_check, args))
    anyio.run(serve_stdio, BridgeSettings.from_env())


if __name__ == "__main__":
    main()
