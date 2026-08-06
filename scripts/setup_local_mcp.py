#!/usr/bin/env python3
"""Private first-run authentication and media-root setup."""

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


DEFAULT_API_URL = "https://remotion-canvas-server-5tdb2hkb4q-as.a.run.app"


class SetupError(RuntimeError):
    pass


class CredentialsRejected(SetupError):
    """The API answered, and the password was rejected.

    Kept distinct from a transport failure so the two produce different advice: a rejected
    password needs replacing, an unreachable API needs retrying. Cassette passwords are
    generated and mailed, never chosen, so "rejected" means stale rather than mistyped.
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
    timeout: float = 60.0,
) -> tuple[int, dict, str | None]:
    """POST JSON and return (status, body, retry-after).

    HTTP error statuses come back as values rather than exceptions so callers can tell 401
    from 429 from 500; only transport and decode failures raise. Unauthenticated by design:
    every call this script makes is a pre-sign-in one.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
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


def verify_credentials(api_url: str, email: str, password: str, *, timeout: float = 60.0) -> None:
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
    # The reply carries access and refresh tokens and an access level. None are returned: this
    # function answers "does this password work", and handing a token back to a caller in a
    # module that writes credential files is how one ends up persisted by accident.


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


def _write_credentials(*, email: str, password: str) -> None:
    """Commit credentials, building the payload from named fields only.

    Not a dict splat on purpose: session tokens flow through this module, and a `{**result}`
    anywhere upstream would silently persist one. With explicit fields it cannot happen.

    No api_url: nothing ever read the copy stored here, and it could silently disagree with
    the authoritative value in settings.json. The origin belongs in one place.
    """
    runtime_config.write_protected_json(
        runtime_config.credentials_path(),
        {
            "email": email,
            "password": password,
            "verified_at": _now_iso(),
        },
    )


def configure_jamendo(*, offer: bool, host: str | None = None) -> str:
    """Validate and store an ID-only Jamendo configuration for MCP hosts."""
    target_host = str(host or os.getenv("CASSETTE_MCP_HOST", "") or "").strip().lower()
    direct = str(os.getenv("JAMENDO_CLIENT_ID", "") or "").strip()
    if direct:
        runtime_config.validate_jamendo_client_id(direct)
        return "Jamendo Client ID is configured through the environment; no local value was written."
    if target_host == "hermes":
        existing = runtime_config.stored_hermes_jamendo()
    else:
        existing = runtime_config.stored_jamendo()
    if offer and sys.stdin.isatty():
        answer = input("Configure a Jamendo Client ID for music matching now? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            return (
                f"Skipped Jamendo setup. Configure it later with `{runtime_config.jamendo_setup_command(PLUGIN_ROOT)}`."
            )
    elif offer:
        return (
            "Skipped interactive Jamendo setup outside a terminal. Configure it later with "
            f"`{runtime_config.jamendo_setup_command(PLUGIN_ROOT)}`."
        )
    current = str(existing.get("client_id") or "").strip()
    prompt = f"Jamendo Client ID [{runtime_config.mask_client_id(current)}]: " if current else "Jamendo Client ID: "
    client_id = input(prompt).strip() or current
    if not client_id:
        raise SetupError("A Jamendo Client ID is required; existing configuration was unchanged.")
    try:
        runtime_config.validate_jamendo_client_id(client_id)
    except runtime_config.JamendoValidationError as exc:
        raise SetupError(f"{exc} Existing Jamendo configuration was unchanged.") from exc
    if target_host == "hermes":
        destination = runtime_config.store_hermes_jamendo_client_id(client_id)
    else:
        runtime_config.store_jamendo_client_id(client_id, verified_at=_now_iso())
        destination = runtime_config.settings_path()
    return (
        f"Verified Jamendo Client ID {runtime_config.mask_client_id(client_id)} and stored it privately at "
        f"{destination}. No Client Secret is required."
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


def _confirm_password_replacement(email: str, *, assume_yes: bool) -> None:
    """Refuse to replace an account password without an explicit yes.

    The action is irreversible, applies to every machine the account is set up on, and spends
    one of three hourly attempts. The server also replaces the password *before* it attempts
    delivery, so even a failed send leaves the old one dead. This used to print the warning and
    fire immediately, which gave the user nothing to stop.
    """
    print(
        f"This requests a new generated password for {email} and emails it to you.\n"
        "The current password stops working everywhere, including on your other machines.",
        file=sys.stderr,
    )
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise SetupError(
            "Refusing to replace the account password without confirmation. Re-run with --yes "
            "if there is no terminal to prompt on."
        )
    if input("Continue? [y/N] ").strip().lower() not in {"y", "yes"}:
        raise SetupError("Cancelled. The account password is unchanged.")


def logout() -> str:
    """Forget the stored credentials on this machine, leaving the account untouched."""
    path = runtime_config.credentials_path()
    if not path.exists() and not path.is_symlink():
        return f"No stored Cassette credentials at {path}."
    try:
        path.unlink()
    except OSError as exc:
        raise SetupError(f"Could not remove {path} ({type(exc).__name__}).") from exc
    return f"Removed {path}.\nThe account password is unchanged — this machine simply no longer holds it."


def reset_password(args: argparse.Namespace) -> str:
    """Replace the account password by email and store the new one privately.

    Email delivery is the only route. An earlier version first tried to rotate a still-working
    password inline, which changed it with no email, returned a value the user never saw, and
    silently broke every other machine they had set up — the opposite of what someone typing
    "reset my password" while troubleshooting wants. Rotation is fine where it is invisible and
    beneficial; it is not the answer to this command.
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

    _confirm_password_replacement(email, assume_yes=args.yes)
    _request_new_password(api_url, email)
    password = getpass.getpass("Paste the new password from your email: ").strip()
    if not password:
        raise SetupError(
            "No password was entered. The account password has already been replaced, so re-run "
            "this command with the password from your email."
        )
    verify_credentials(api_url, email, password)
    _write_credentials(email=email, password=password)
    return "emailed"


CLAUDE_MARKETPLACE = "cassette-editor"


def claude_settings_path() -> Path:
    base = str(os.getenv("CLAUDE_CONFIG_DIR", "") or "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".claude"
    return root / "settings.json"


def enable_claude_auto_update(*, skip: bool, assume_yes: bool = False) -> str:
    """Turn on Claude Code's per-marketplace auto-update for an existing cassette-editor entry.

    Claude Code is the only host that updates plugins on its own, and third-party
    marketplaces ship with it off. It is a user setting with no CLI flag, so the one
    piece of ours that runs on a Claude user's machine offers to set it.
    """
    if skip:
        return "Skipped the Claude Code auto-update opt-in (--no-auto-update)."
    path = claude_settings_path()
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(settings, dict):
        return ""
    marketplaces = settings.get("extraKnownMarketplaces")
    entry = marketplaces.get(CLAUDE_MARKETPLACE) if isinstance(marketplaces, dict) else None
    # Never add the marketplace here: its absence means Claude Code is not the host, or the
    # user installed the plugin some other way. `claude plugin marketplace add` owns that entry.
    if not isinstance(entry, dict):
        return ""
    if entry.get("autoUpdate") is True:
        return f"Claude Code auto-update is already on for {CLAUDE_MARKETPLACE}."
    if not assume_yes and sys.stdin.isatty():
        answer = input(f"Enable automatic plugin updates for {CLAUDE_MARKETPLACE} in Claude Code? [Y/n] ")
        if answer.strip().lower() in {"n", "no"}:
            return f"Left auto-update off; enable it later in /plugin > Marketplaces > {CLAUDE_MARKETPLACE}."
    elif not assume_yes:
        return f"Enable auto-update in /plugin > Marketplaces > {CLAUDE_MARKETPLACE} (not a terminal here)."
    entry["autoUpdate"] = True
    try:
        # Read-modify-write of the parsed document: every other Claude setting is preserved
        # untouched, and a partial write can never leave the user without settings.
        temporary = path.with_name(f".{path.name}.oh-my-cassette.tmp")
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        return f"Could not write {path} ({exc.__class__.__name__}); enable auto-update from /plugin instead."
    # Claude Code syncs a settings-declared autoUpdate into its marketplace state and then
    # refuses to change it from /plugin, so say where to turn it back off.
    return f"Claude Code will now auto-update {CLAUDE_MARKETPLACE} plugins. Change it in {path}."


def configure(args: argparse.Namespace) -> None:
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
    verify_credentials(api_url, email, password)

    existing_settings = runtime_config.read_protected_json(runtime_config.settings_path())
    roots = _canonical_media_roots(args.allowed_root)
    settings = {
        **existing_settings,
        "media_roots": roots if args.allowed_root else existing_settings.get("media_roots", []),
        "api_url": api_url,
    }

    # Verification is complete; only now are credentials committed atomically.
    _write_credentials(email=email, password=password)
    runtime_config.write_protected_json(runtime_config.settings_path(), settings)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify and privately store credentials for the Oh My Cassette local MCP plugin"
    )
    parser.add_argument("--email", help="Cassette account email; password is never accepted as a command-line argument")
    parser.add_argument("--api-url", help="Cassette API origin")
    parser.add_argument("--allowed-root", action="append", default=[], help="Additional trusted media directory")
    parser.add_argument(
        "--import-hermes",
        nargs="?",
        const=Path.home() / ".hermes" / ".env",
        type=Path,
        help="Explicitly import Cassette credentials from a Hermes .env file",
    )
    parser.add_argument(
        "--use-environment",
        action="store_true",
        help="Read credentials from ephemeral environment variables (intended for maintainer acceptance only)",
    )
    parser.add_argument(
        "--jamendo",
        action="store_true",
        help="Interactively verify and store only a Jamendo Client ID",
    )
    parser.add_argument(
        "--host",
        choices=("codex", "claude", "opencode", "hermes"),
        help="MCP host for --jamendo storage (Hermes uses ~/.hermes/.env)",
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="Have Cassette email a new generated password and store it privately",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="Forget the stored credentials on this machine; the account password is unchanged",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt when replacing the account password",
    )
    parser.add_argument(
        "--no-auto-update",
        action="store_true",
        help="Do not offer to enable Claude Code's automatic plugin updates for cassette-editor",
    )
    return parser.parse_args()


def _reject_mode_conflicts(args: argparse.Namespace, mode: str) -> None:
    """A reset or logout touches only the stored password; silently ignoring setup flags is worse."""
    conflicts = [
        name
        for name, used in (
            ("--api-url", bool(args.api_url)),
            ("--allowed-root", bool(args.allowed_root)),
            ("--import-hermes", args.import_hermes is not None),
            ("--use-environment", args.use_environment),
            ("--no-auto-update", args.no_auto_update),
            ("--email", bool(args.email) and mode in {"--logout", "--jamendo"}),
            ("--yes", args.yes and mode in {"--logout", "--jamendo"}),
            ("--host", bool(getattr(args, "host", None)) and mode != "--jamendo"),
        )
        if used
    ]
    if conflicts:
        raise SetupError(
            f"{mode} does not accept {', '.join(conflicts)}; it only touches the stored "
            "credentials on this machine. Run those separately."
        )


def main() -> None:
    args = parse_args()
    # The two paths keep separate result variables. Sharing one made every read of it a read
    # of a value that had passed through the password flow, which is both harder to follow
    # and what CodeQL's clear-text-logging rule objected to.
    try:
        modes = [
            name
            for name, enabled in (
                ("--reset-password", args.reset_password),
                ("--logout", args.logout),
                ("--jamendo", args.jamendo),
            )
            if enabled
        ]
        if len(modes) > 1:
            raise SetupError(f"{', '.join(modes)} are separate setup modes; pick one.")
        if args.logout:
            _reject_mode_conflicts(args, "--logout")
            print(logout())
            return
        if args.reset_password:
            _reject_mode_conflicts(args, "--reset-password")
            reset_password(args)
            print("New password stored.")
            print(f"Saved at {runtime_config.credentials_path()}.")
            return
        if args.jamendo:
            _reject_mode_conflicts(args, "--jamendo")
            print(configure_jamendo(offer=False, host=args.host))
            return
        if args.host:
            raise SetupError("--host is only valid together with --jamendo.")
        configure(args)
    except (SetupError, runtime_config.JamendoValidationError, runtime_config.RuntimeConfigError) as exc:
        path = getattr(exc, "path", None)
        location = f" ({path})" if path else ""
        print(f"oh-my-cassette setup: {exc}{location}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Verified credentials saved privately at {runtime_config.credentials_path()}.")
    auto_update = enable_claude_auto_update(skip=args.no_auto_update)
    if auto_update:
        print(auto_update)
    try:
        print(configure_jamendo(offer=True))
    except (SetupError, runtime_config.JamendoValidationError, runtime_config.RuntimeConfigError) as exc:
        print(
            "Cassette sign-in succeeded, but optional Jamendo setup did not: "
            f"{exc} Run `{runtime_config.jamendo_setup_command(PLUGIN_ROOT)}` to retry.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
