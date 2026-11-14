"""Console entry point: `oh-my-cassette` runs the stdio bridge; `oh-my-cassette check` tests an endpoint;
`oh-my-cassette key` keeps the account's own OpenAI key."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import threading

import anyio
import httpx
from keyring.errors import KeyringError
from mcp.server.stdio import stdio_server

from oh_my_cassette import __version__, auth, provider_key
from oh_my_cassette.auth import AuthenticationError, Credentials, login, saved_connection
from oh_my_cassette.bridge.files import transfer_guard
from oh_my_cassette.bridge.server import Bridge
from oh_my_cassette.bridge.settings import BridgeSettings


async def serve_stdio(settings: BridgeSettings) -> None:
    # Uploads and downloads: no overall deadline, only the same idle limit as the MCP connection.
    timeout = httpx.Timeout(settings.read_timeout_sec, connect=settings.connect_timeout_sec)
    hooks = {"request": [transfer_guard(settings.mcp_url)]}
    async with httpx.AsyncClient(timeout=timeout, event_hooks=hooks) as http, anyio.create_task_group() as tg:
        bridge = Bridge(settings, http)
        await tg.start(bridge.run)
        server = bridge.build_server()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
        tg.cancel_scope.cancel()


async def _check(args: argparse.Namespace) -> int:
    from oh_my_cassette.conformance import failed, render, run_checks, summary

    url = args.url or BridgeSettings.from_env().mcp_url
    token = os.environ.get("CASSETTE_AUTH_TOKEN") or await Credentials(url).token()
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
                lambda: login(no_browser=args.no_browser, cancelled=cancelled),
                abandon_on_cancel=True,
            )
        finally:
            cancelled.set()
        print(f"Signed in as {connection.email}. Restart the MCP host if its tool list does not update.")
        return 0
    connection = saved_connection()
    if not connection:
        print("Not signed in. Run oh-my-cassette login.")
        return 1
    credentials = Credentials(connection.resource, connection=connection)
    if args.command == "logout":
        await credentials.logout()
        print(f"Signed out of {connection.email}.")
    else:
        identity = await credentials.identity()
        print(
            json.dumps(
                {
                    "signed_in": True,
                    "email": identity["email"],
                    "resource": connection.resource,
                    "expires_at": identity["expiresAt"],
                }
            )
        )
    return 0


def _key_command(args: argparse.Namespace) -> int:
    """`key set|status|remove`: the account's own OpenAI key, kept in the keyring for the saved connection."""
    connection = saved_connection()
    if not connection:
        print(
            "Not signed in. Run oh-my-cassette login first; the key is kept for the account you sign in with."
        )
        return 1
    try:
        store = auth.native_keyring()
        if args.action == "set":
            key = getpass.getpass("OpenAI API key (input hidden): ").strip()
            if not provider_key.valid_key(key):
                print("That is not an OpenAI secret key (sk-…). Nothing was saved.", file=sys.stderr)
                return 1
            provider_key.save_key(store, connection.key, key)
            print(
                f"Saved the OpenAI key {provider_key.fingerprint(key)} for {connection.email}. "
                "The bridge hands it to Cassette with its next request; from then on the account's agent turns "
                "use your key until you switch back to Cassette's key in Cassette settings."
            )
        elif args.action == "remove":
            # Cassette lets go of its copy first, while the saved sign-in can still ask it to.
            released: str | None = None
            try:
                anyio.run(Credentials(connection.resource, connection=connection).release_provider_key)
            except AuthenticationError as exc:
                released = str(exc)
            removed = provider_key.remove_key(store, connection.key)
            print(
                f"Removed the OpenAI key for {connection.email}."
                if removed
                else f"No OpenAI key is saved for {connection.email}."
            )
            print(
                "Cassette no longer holds a key for the account; its agent turns wait for one until you set a key "
                "again or switch back to Cassette's key in Cassette settings."
                if released is None
                else f"Cassette could not be asked to let go of the key it holds ({released}); it does so after 30 idle minutes."
            )
            return 0 if released is None else 1
        else:
            key = provider_key.read_key(store, connection.key)
            print(
                json.dumps(
                    {
                        "saved": key is not None,
                        "email": connection.email,
                        "fingerprint": provider_key.fingerprint(key) if key else None,
                    },
                    ensure_ascii=False,
                )
            )
    except KeyringError as exc:
        raise AuthenticationError(
            "Secure credential storage unavailable; unlock your system keyring and retry", 503
        ) from exc
    return 0


def main(argv: list[str] | None = None) -> None:
    if sys.platform == "win32":
        # Windows otherwise looks up ffmpeg in the working directory, the host's project, before PATH.
        os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")
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
    account_commands = {
        "login": "sign in to Cassette in the browser and save the connection",
        "status": "show the saved connection",
        "logout": "forget the saved connection",
    }
    for name, help_text in account_commands.items():
        account = commands.add_parser(name, help=help_text)
        if name == "login":
            account.add_argument(
                "--no-browser",
                action="store_true",
                help="print the browser URL without opening it automatically",
            )
    key = commands.add_parser("key", help="save, show or remove your own OpenAI key for Cassette's agent")
    key.add_argument(
        "action",
        choices=("set", "status", "remove"),
        help="set prompts for the key and keeps it in the system keyring; status shows its fingerprint",
    )
    check = commands.add_parser("check", help="check a Cassette MCP endpoint against the bridge contract")
    check.add_argument(
        "--url", help="MCP endpoint (default: CASSETTE_MCP_URL, the saved connection, or Cassette's)"
    )
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
        if args.command == "key":
            sys.exit(_key_command(args))
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
