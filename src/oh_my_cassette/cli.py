"""Console entry point: `oh-my-cassette` runs the stdio bridge; `oh-my-cassette check` tests an endpoint."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading

import anyio
import httpx
from mcp.server.stdio import stdio_server

from oh_my_cassette import __version__
from oh_my_cassette.auth import AuthenticationError, Credentials, login, saved_connection
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
    token = args.token or os.environ.get("CASSETTE_AUTH_TOKEN") or await Credentials(url).token()
    checks = await run_checks(url, token=token, upload=args.upload, arguments=json.loads(args.arguments))
    print(render(checks, as_json=args.json))
    if not args.json:
        counts = summary(checks)
        print(f"\n{url}: {counts['pass']} passed, {counts['warn']} warnings, {counts['fail']} failed")
    return 1 if failed(checks) else 0


async def _account_command(args: argparse.Namespace) -> int:
    if args.command == "login":
        cancelled = threading.Event()
        try:
            connection = await anyio.to_thread.run_sync(
                lambda: login(args.target, no_browser=args.no_browser, cancelled=cancelled),
                abandon_on_cancel=True,
            )
        finally:
            cancelled.set()
        print(
            f"Signed in as {connection.email} → {connection.target}. Restart the MCP host if its tool list does not update."
        )
        return 0
    connection = saved_connection(args.target)
    if not connection:
        print("Not signed in. Run oh-my-cassette login --target web (or desktop).")
        return 1
    credentials = Credentials(connection.resource, connection=connection)
    if args.command == "logout":
        await credentials.logout()
        print(f"Signed out of {connection.target}.")
    else:
        identity = await credentials.identity()
        print(
            json.dumps(
                {
                    "signed_in": True,
                    "email": identity["email"],
                    "target": connection.target,
                    "resource": connection.resource,
                    "expires_at": identity["expiresAt"],
                }
            )
        )
    return 0


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
    for name in ("login", "status", "logout"):
        account = commands.add_parser(name, help=f"{name} a saved Cassette connection")
        account.add_argument(
            "--target", choices=("web", "desktop"), default="web" if name == "login" else None
        )
        if name == "login":
            account.add_argument(
                "--no-browser",
                action="store_true",
                help="print the browser URL without opening it automatically",
            )
    check = commands.add_parser("check", help="check a Cassette MCP endpoint against the bridge contract")
    check.add_argument(
        "--url", help="MCP endpoint (default: CASSETTE_MCP_URL, saved target, or Cassette Web)"
    )
    check.add_argument("--token", help="bearer token (default: CASSETTE_AUTH_TOKEN)")
    check.add_argument("--upload", action="store_true", help="also run an upload round trip")
    check.add_argument(
        "--arguments",
        default="{}",
        help="JSON arguments of the file tool for the upload round trip, e.g. a project id",
    )
    check.add_argument("--json", action="store_true", help="print the checks as JSON")
    args = parser.parse_args(argv)

    try:
        if args.command in ("login", "status", "logout"):
            sys.exit(anyio.run(_account_command, args))
        if args.command == "check":
            sys.exit(anyio.run(_check, args))
        anyio.run(serve_stdio, BridgeSettings.from_env())
    except AuthenticationError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
