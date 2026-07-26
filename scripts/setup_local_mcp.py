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


class CredentialsRejected(SetupError):
    """The API answered, and the password was wrong.

    Distinct from a transport failure so the reset flow only falls back to the destructive
    email path when the password is genuinely dead — a network blip must not replace a
    working account password.
    """


# Every variable load_credentials() consults, in its own precedence order.
ENV_CREDENTIAL_VARS = (
    "CASSETTE_AUTH_EMAIL",
    "CASSETTE_AUTH_ACCOUNT",
    "CASSETTE_EMAIL",
    "CASSETTE_AUTH_PASSWORD",
    "CASSETTE_PASSWORD",
)


def _post_json(
    api_url: str,
    path: str,
    payload: dict,
    *,
    token: str | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict, str | None]:
    """POST JSON and return (status, body, retry-after).

    HTTP error statuses come back as values rather than exceptions so callers can tell 401
    from 429 from 500; only transport and decode failures raise.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(api_url.rstrip("/") + path, data=body, method="POST", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read().decode("utf-8")
            retry_after = response.headers.get("retry-after")
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8", "replace")
        retry_after = exc.headers.get("retry-after") if exc.headers else None
    except (URLError, TimeoutError, OSError) as exc:
        raise SetupError(f"Could not reach the Cassette API ({type(exc).__name__}).") from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}, retry_after


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
        if exc.code in {400, 401, 403}:
            raise CredentialsRejected(
                f"Cassette rejected the credentials (HTTP {exc.code}); no credentials were written."
            ) from exc
        raise SetupError(
            f"Cassette credential verification failed (HTTP {exc.code}); no credentials were written."
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SetupError(
            f"Cassette credential verification could not reach the API ({type(exc).__name__}); no credentials were written."
        ) from exc
    except ValueError as exc:
        raise SetupError(
            "Cassette credential verification returned invalid JSON; no credentials were written."
        ) from exc
    session = payload.get("session") if isinstance(payload, dict) else {}
    if status != 200 or not isinstance(session, dict) or not session.get("access_token"):
        raise CredentialsRejected("Cassette rejected the credentials; no credentials were written.")
    # access_token is returned for the caller's immediate use only. It must never reach
    # _write_credentials, which is why that helper takes named fields instead of a dict.
    return {
        "full_api_access": bool(payload.get("isFullUser")),
        "access_token": str(session["access_token"]),
    }


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


def _write_credentials(*, email: str, password: str, full_api_access: bool, api_url: str) -> None:
    """Commit credentials, building the payload from named fields only.

    Not a dict splat on purpose: session tokens flow through this module, and a `{**result}`
    anywhere upstream would silently persist one. With explicit fields it cannot happen.
    """
    runtime_config.write_protected_json(
        runtime_config.credentials_path(),
        {
            "email": email,
            "password": password,
            "full_api_access": full_api_access,
            "verified_at": _now_iso(),
            "api_url": api_url,
        },
    )


def _reset_api_url() -> str:
    """Resolve the origin the MCP runtime will actually talk to.

    runtime_config.mcp_env_value() is gated on the mcp adapter, which is not set when this
    script runs from a terminal, so mirror its order by hand. --api-url is deliberately not
    honoured on this path: rotating against an origin the runtime does not use would replace
    the wrong account's password. The api_url stored inside credentials.json is ignored for
    the same reason — nothing reads it back, so it can silently disagree with settings.json.
    """
    from_env = str(os.getenv("CASSETTE_API_URL") or os.getenv("CASSETTE_API_BASE_URL") or "").strip()
    if from_env:
        return from_env.rstrip("/")
    stored = str(runtime_config.load_settings().get("api_url") or "").strip()
    return (stored or DEFAULT_API_URL).rstrip("/")


def _request_new_password(api_url: str, email: str) -> None:
    """Ask the server to replace the account password and mail it."""
    status, body, retry_after = _post_json(api_url, "/api/agent-auth/request-code", {"email": email})
    if status == 429:
        delay = f" Try again in {retry_after} seconds." if retry_after else ""
        raise SetupError(f"Too many password requests for {email}.{delay}")
    if status != 200:
        raise SetupError(
            f"Cassette could not send a new password (HTTP {status}). The account password may "
            "already have been replaced, so check your email before retrying — every attempt "
            "spends the hourly limit."
        )
    # The server answers 200 with sent=false for unknown emails so it never confirms who has
    # an account. No mail is coming, so stopping here beats prompting for a password forever.
    if body.get("sent") is not True:
        raise SetupError(
            f"{email} is not authorised for Cassette, so no password was sent. Request access for this address first."
        )


def reset_password(args: argparse.Namespace) -> str:
    """Replace the account password and store the new one privately.

    Two routes. If the stored password still works, the account is rotated through an
    authenticated call that returns the replacement inline, so nothing is typed and a running
    MCP host picks it up on its next call. If the stored password is already dead — the usual
    reason for running this — that route is unreachable, so fall back to email delivery.
    """
    api_url = _reset_api_url()
    credentials = runtime_config.load_credentials()

    # The setup script sees the terminal's environment; the MCP host has its own. Writing the
    # private config while env vars are set would leave them shadowing the new password.
    if credentials.get("source") == "environment":
        present = ", ".join(name for name in ENV_CREDENTIAL_VARS if os.getenv(name)) or "the environment"
        raise SetupError(
            f"Cassette credentials come from {present}, which takes precedence over the private "
            "config. Reset the password in the Cassette web app and update those variables; "
            "writing the private config here would have no effect."
        )

    email = str(args.email or credentials.get("email") or "").strip()
    if not email:
        email = input("Cassette account email: ").strip()
    if not email:
        raise SetupError("An account email is required to reset the password.")

    stored_password = str(credentials.get("password") or "")
    if stored_password:
        try:
            verification = verify_credentials(api_url, email, stored_password)
        except CredentialsRejected:
            verification = None  # already stale; only email delivery can recover it
        if verification is not None:
            print("Rotating the account password. This replaces it everywhere, including other machines.")
            status, body, _ = _post_json(
                api_url,
                "/api/agent-auth/rotate-password",
                {},
                token=verification["access_token"],
            )
            new_password = str(body.get("password") or "").strip() if status == 200 else ""
            if new_password:
                _write_credentials(
                    email=email,
                    password=new_password,
                    full_api_access=bool(verification["full_api_access"]),
                    api_url=api_url,
                )
                return "rotated"
            # A deployment without the rotate route (or one that refused) still resets by email.

    print(f"Sending a new password to {email}. This replaces it everywhere, including other machines.")
    _request_new_password(api_url, email)
    password = getpass.getpass("Paste the new password from your email: ").strip()
    if not password:
        raise SetupError(
            "No password was entered. The account password has already been replaced, so re-run "
            "this command with the password from your email."
        )
    verification = verify_credentials(api_url, email, password)
    _write_credentials(
        email=email,
        password=password,
        full_api_access=bool(verification["full_api_access"]),
        api_url=api_url,
    )
    return "emailed"


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
    # Strip on write, because load_credentials() strips on read: a pasted password with a
    # trailing space would otherwise verify here and then be handed to the runtime as a
    # different string. The generated alphabet contains no whitespace, so this is lossless.
    password = (
        password
        or imported.get("CASSETTE_AUTH_PASSWORD")
        or imported.get("CASSETTE_PASSWORD")
        or getpass.getpass("Cassette account password: ")
    ).strip()
    if not email or not password:
        raise SetupError("Both email and password are required; no credentials were written.")

    api_url = str(
        args.api_url
        or (os.getenv("CASSETTE_API_URL") if args.use_environment else "")
        or imported.get("CASSETTE_API_URL")
        or DEFAULT_API_URL
    ).rstrip("/")
    verification = verify_credentials(api_url, email, password)

    existing_settings = runtime_config.read_protected_json(runtime_config.settings_path())
    roots = _canonical_media_roots(args.allowed_root)
    settings = {
        **existing_settings,
        "transport": "browser" if args.with_browser else args.transport,
        "media_roots": roots if args.allowed_root else existing_settings.get("media_roots", []),
        "api_url": api_url,
    }

    # Verification is complete; only now are credentials committed atomically.
    _write_credentials(
        email=email,
        password=password,
        full_api_access=bool(verification["full_api_access"]),
        api_url=api_url,
    )
    runtime_config.write_protected_json(runtime_config.settings_path(), settings)

    if args.with_browser:
        try:
            bootstrap_runtime(with_browser=True, output=sys.stderr)
        except BootstrapError as exc:
            raise SetupError(f"Credentials were saved, but optional browser setup failed: {exc}") from exc

    return {
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
    parser.add_argument(
        "--with-browser",
        action="store_true",
        help="Install pinned Playwright and Chromium, then select browser transport",
    )
    parser.add_argument(
        "--use-environment",
        action="store_true",
        help="Read credentials from ephemeral environment variables (intended for maintainer acceptance only)",
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="Replace the account password and store the new one privately",
    )
    return parser.parse_args()


def _reject_reset_password_conflicts(args: argparse.Namespace) -> None:
    """A reset only replaces the stored password; silently ignoring setup flags is worse."""
    conflicts = [
        name
        for name, used in (
            ("--api-url", bool(args.api_url)),
            ("--transport", args.transport != "api"),
            ("--allowed-root", bool(args.allowed_root)),
            ("--import-hermes", args.import_hermes is not None),
            ("--with-browser", args.with_browser),
            ("--use-environment", args.use_environment),
        )
        if used
    ]
    if conflicts:
        raise SetupError(
            f"--reset-password does not accept {', '.join(conflicts)}; it only replaces the "
            "stored password. Run those separately."
        )


def main() -> None:
    args = parse_args()
    # The two paths keep separate result variables. Sharing one made every read of it a read
    # of a value that had passed through the password flow, which is both harder to follow
    # and what CodeQL's clear-text-logging rule objected to.
    try:
        if args.reset_password:
            _reject_reset_password_conflicts(args)
            delivery = reset_password(args)
            print("Password rotated." if delivery == "rotated" else "New password stored.")
            print(f"Saved at {runtime_config.credentials_path()}.")
            return
        setup = configure(args)
    except (SetupError, runtime_config.RuntimeConfigError) as exc:
        path = getattr(exc, "path", None)
        location = f" ({path})" if path else ""
        print(f"oh-my-cassette setup: {exc}{location}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Verified credentials saved privately at {runtime_config.credentials_path()}.")
    print(f"Selected transport: {setup['transport']}.")
    if not setup["full_api_access"] and setup["transport"] == "api":
        print("This account lacks full API access. Run this command again with --with-browser.")


if __name__ == "__main__":
    main()
