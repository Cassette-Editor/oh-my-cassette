#!/usr/bin/env python3
"""Private first-run authentication and optional browser setup."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(PLUGIN_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import runtime_config  # noqa: E402
from local_mcp_bootstrap import BootstrapError, bootstrap_runtime  # noqa: E402


DEFAULT_API_URL = "https://remotion-canvas-server-5tdb2hkb4q-as.a.run.app"


class SetupError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _read_hermes_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError as exc:
        raise SetupError(f"Could not read the explicit Hermes env file: {path}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if key in {
            "CASSETTE_AUTH_EMAIL",
            "CASSETTE_AUTH_ACCOUNT",
            "CASSETTE_EMAIL",
            "CASSETTE_AUTH_PASSWORD",
            "CASSETTE_PASSWORD",
            "CASSETTE_API_URL",
        }:
            values[key] = _unquote(value)
    return values


def verify_credentials(api_url: str, email: str, password: str, *, timeout: float = 60.0) -> dict:
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    request = Request(
        api_url.rstrip("/") + "/api/agent-auth/verify",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise SetupError(f"Cassette credential verification failed (HTTP {exc.code}); no credentials were written.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SetupError(f"Cassette credential verification could not reach the API ({type(exc).__name__}); no credentials were written.") from exc
    except ValueError as exc:
        raise SetupError("Cassette credential verification returned invalid JSON; no credentials were written.") from exc
    session = payload.get("session") if isinstance(payload, dict) else {}
    if status != 200 or not isinstance(session, dict) or not session.get("access_token"):
        raise SetupError("Cassette rejected the credentials; no credentials were written.")
    return {"full_api_access": bool(payload.get("isFullUser"))}


def _canonical_media_roots(values: list[str]) -> list[str]:
    roots: list[str] = []
    for value in values:
        path = Path(os.path.expandvars(value)).expanduser()
        if path.is_symlink() or not path.exists() or not path.is_dir():
            raise SetupError(f"Configured media root must be an existing, non-symlink directory: {path}")
        resolved = path.resolve()
        if str(resolved) not in roots:
            roots.append(str(resolved))
    return roots


def configure(args: argparse.Namespace) -> dict:
    imported: dict[str, str] = {}
    if args.import_hermes is not None:
        imported = _read_hermes_env(args.import_hermes)

    email = str(
        args.email
        or (os.getenv("CASSETTE_AUTH_EMAIL") if args.use_environment else "")
        or imported.get("CASSETTE_AUTH_EMAIL")
        or imported.get("CASSETTE_AUTH_ACCOUNT")
        or imported.get("CASSETTE_EMAIL")
        or ""
    ).strip()
    if not email:
        email = input("Cassette account email: ").strip()
    if args.use_environment:
        password = str(os.getenv("CASSETTE_AUTH_PASSWORD") or os.getenv("CASSETTE_PASSWORD") or "")
    else:
        password = ""
    password = (
        password
        or imported.get("CASSETTE_AUTH_PASSWORD")
        or imported.get("CASSETTE_PASSWORD")
        or getpass.getpass("Cassette account password: ")
    )
    if not email or not password:
        raise SetupError("Both email and password are required; no credentials were written.")

    api_url = str(
        args.api_url
        or (os.getenv("CASSETTE_API_URL") if args.use_environment else "")
        or imported.get("CASSETTE_API_URL")
        or DEFAULT_API_URL
    ).rstrip("/")
    verification = verify_credentials(api_url, email, password)

    credentials = {
        "email": email,
        "password": password,
        "full_api_access": verification["full_api_access"],
        "verified_at": _now_iso(),
        "api_url": api_url,
    }
    existing_settings = runtime_config.read_protected_json(runtime_config.settings_path())
    roots = _canonical_media_roots(args.allowed_root)
    settings = {
        **existing_settings,
        "transport": "browser" if args.with_browser else args.transport,
        "media_roots": roots if args.allowed_root else existing_settings.get("media_roots", []),
        "api_url": api_url,
    }

    # Verification is complete; only now are credentials committed atomically.
    runtime_config.write_protected_json(runtime_config.credentials_path(), credentials)
    runtime_config.write_protected_json(runtime_config.settings_path(), settings)

    if args.with_browser:
        try:
            bootstrap_runtime(with_browser=True, output=sys.stderr)
        except BootstrapError as exc:
            raise SetupError(f"Credentials were saved, but optional browser setup failed: {exc}") from exc

    return {
        "credential_path": str(runtime_config.credentials_path()),
        "transport": settings["transport"],
        "full_api_access": verification["full_api_access"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify and privately store credentials for the Oh My Cassette local MCP plugin"
    )
    parser.add_argument("--email", help="Cassette account email; password is never accepted as a command-line argument")
    parser.add_argument("--api-url", help="Cassette API origin")
    parser.add_argument("--transport", choices=("api", "browser"), default="api")
    parser.add_argument("--allowed-root", action="append", default=[], help="Additional trusted media directory")
    parser.add_argument(
        "--import-hermes",
        nargs="?",
        const=Path.home() / ".hermes" / ".env",
        type=Path,
        help="Explicitly import Cassette credentials from a Hermes .env file",
    )
    parser.add_argument("--with-browser", action="store_true", help="Install pinned Playwright and Chromium, then select browser transport")
    parser.add_argument(
        "--use-environment",
        action="store_true",
        help="Read credentials from ephemeral environment variables (intended for maintainer acceptance only)",
    )
    return parser.parse_args()


def main() -> None:
    try:
        result = configure(parse_args())
    except (SetupError, runtime_config.RuntimeConfigError) as exc:
        print(f"oh-my-cassette setup: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Verified credentials saved privately at {result['credential_path']}.")
    print(f"Selected transport: {result['transport']}.")
    if not result["full_api_access"] and result["transport"] == "api":
        print("This account lacks full API access. Run this command again with --with-browser.")


if __name__ == "__main__":
    main()
