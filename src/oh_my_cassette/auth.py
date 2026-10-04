"""Native OAuth grants shared by stdio, uploads, downloads, and the CLI.

Only public connection metadata goes on disk. Tokens live in the operating system keyring;
a process lock covers the entire read/rotate/write transaction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import secrets
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
import httpx
from filelock import FileLock
from filelock import Timeout as LockTimeout
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, PasswordDeleteError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

CLIENT_ID = "oh-my-cassette"
SCOPE = "cassette:agent"
DEFAULT_WEB_RESOURCE = "https://trycassette.online/mcp"
SERVICE = "oh-my-cassette.oauth"


class AuthenticationError(Exception):
    """Public-safe authentication state, without provider bodies or secrets."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def auth_directory() -> Path:
    return Path(
        os.environ.get("CASSETTE_AUTH_DIRECTORY", Path.home() / ".config/oh-my-cassette")
    ).expanduser()


def _secure_url(value: str) -> str:
    url = urlsplit(value)
    if url.username or url.password or url.fragment:
        raise AuthenticationError("Invalid authentication URL")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname == "127.0.0.1"):
        raise AuthenticationError("Authentication requires HTTPS or an explicit loopback target")
    return value


def _origin(value: str) -> tuple[str, str | None, int | None]:
    url = urlsplit(value)
    return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)


def native_keyring() -> KeyringBackend:
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    from keyring.backends.SecretService import Keyring

    return Keyring()


@contextmanager
def auth_http():
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as http:
            yield http
    except httpx.HTTPError as exc:
        raise AuthenticationError("Authentication service unavailable", 503) from exc


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    expires_at: float
    subject: str


@dataclass(frozen=True)
class Connection:
    target: str
    resource: str
    issuer: str
    subject: str
    email: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str

    @property
    def key(self) -> str:
        return hashlib.sha256(
            f"{self.issuer}\n{self.resource}\n{CLIENT_ID}\n{self.subject}".encode()
        ).hexdigest()


def saved_connection(target: str | None = None) -> Connection | None:
    root = auth_directory()
    try:
        selected = target or json.loads((root / "active.json").read_text())["target"]
        if selected not in ("web", "desktop"):
            raise ValueError("Invalid target")
        connection = Connection(**json.loads((root / f"{selected}.json").read_text()))
        if connection.target != selected:
            raise ValueError("Target mismatch")
        _secure_url(connection.resource)
        _secure_url(connection.issuer)
        for endpoint in (
            connection.authorization_endpoint,
            connection.token_endpoint,
            connection.revocation_endpoint,
        ):
            if _origin(_secure_url(endpoint)) != _origin(connection.issuer):
                raise ValueError("Issuer endpoint mismatch")
        return connection
    except FileNotFoundError:
        return None
    except (ValueError, TypeError, KeyError) as exc:
        raise AuthenticationError("Saved connection is invalid; sign in again") from exc


def _write_public(file: Path, value: object) -> None:
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = file.with_name(f".{file.name}.{secrets.token_hex(6)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output)
    os.replace(temporary, file)


def _answer(response: httpx.Response) -> dict:
    if not response.is_success:
        status = 503 if response.status_code >= 500 else response.status_code
        raise AuthenticationError(
            "Authentication service unavailable"
            if status == 503
            else "Authorization rejected; run oh-my-cassette login",
            status,
        )
    try:
        value = response.json()
    except ValueError as exc:
        raise AuthenticationError("Invalid authentication response", 503) from exc
    if not isinstance(value, dict):
        raise AuthenticationError("Invalid authentication response", 503)
    return value


def discover(resource: str) -> dict:
    _secure_url(resource)
    url = urlsplit(resource)
    metadata_url = f"{url.scheme}://{url.netloc}/.well-known/oauth-protected-resource{url.path}"
    with auth_http() as http:
        resource_metadata = _answer(http.get(metadata_url))
        if resource_metadata.get("resource") != resource:
            raise AuthenticationError("Resource metadata does not match the chosen target")
        issuers = resource_metadata.get("authorization_servers")
        if not isinstance(issuers, list) or len(issuers) != 1 or not isinstance(issuers[0], str):
            raise AuthenticationError("Expected one Cassette authorization service")
        issuer = _secure_url(issuers[0])
        metadata = _answer(http.get(f"{issuer}/.well-known/openid-configuration"))
        if metadata.get("issuer") != issuer or "S256" not in metadata.get(
            "code_challenge_methods_supported", []
        ):
            raise AuthenticationError("Authorization service does not support the required PKCE flow")
        for name in ("authorization_endpoint", "token_endpoint", "revocation_endpoint"):
            endpoint = metadata.get(name)
            if not isinstance(endpoint, str) or _origin(_secure_url(endpoint)) != _origin(issuer):
                raise AuthenticationError("Invalid authorization endpoint")
        return metadata


def target_resource(target: str) -> tuple[str, str | None]:
    if target == "web":
        return os.environ.get("CASSETTE_MCP_URL") or DEFAULT_WEB_RESOURCE, None
    default = (
        Path.home() / "Library/Application Support/Cassette/mcp.json"
        if sys.platform == "darwin"
        else Path.home() / ".config/Cassette/mcp.json"
    )
    file = Path(os.environ.get("CASSETTE_DESKTOP_DISCOVERY", default)).expanduser()
    try:
        device = json.loads(file.read_text())
        resource, subject, device_id = device["resource"], device["subject"], device["deviceId"]
        url = urlsplit(resource)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or not url.port
            or url.path != f"/devices/{device_id}/mcp"
        ):
            raise ValueError("Invalid discovery")
        return resource, subject
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError(
            "Open Cassette Desktop and sign in before choosing --target desktop", 503
        ) from exc


class Credentials:
    def __init__(
        self,
        resource: str,
        *,
        explicit_token: str | None = None,
        connection: Connection | None = None,
        keyring: KeyringBackend | None = None,
    ):
        self.resource = resource
        self.explicit_token = explicit_token
        self.connection = connection
        self._keyring = keyring

    @property
    def configured(self) -> bool:
        connection = self.connection or saved_connection()
        return bool(self.explicit_token or (connection and connection.resource == self.resource))

    def _store(self) -> KeyringBackend:
        self._keyring = self._keyring or native_keyring()
        return self._keyring

    def _connection(self) -> Connection:
        connection = self.connection or saved_connection()
        if not connection or connection.resource != self.resource:
            raise AuthenticationError(
                "Not signed in. Run oh-my-cassette login --target web (or desktop). Restart the MCP host if it cannot refresh tools."
            )
        return connection

    def _read(self, connection: Connection) -> Grant:
        value = self._store().get_password(SERVICE, connection.key)
        if not value:
            raise AuthenticationError("No saved grant. Run oh-my-cassette login")
        grant = Grant.model_validate_json(value)
        if grant.subject != connection.subject:
            raise AuthenticationError("Saved grant belongs to another account", 403)
        return grant

    def _token(self) -> str:
        if self.explicit_token:
            return self.explicit_token
        connection = self._connection()
        root = auth_directory()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(root / f"{connection.key}.lock", timeout=30):
            grant = self._read(connection)
            if grant.expires_at <= time.time() + 60:
                try:
                    with auth_http() as http:
                        body = _answer(
                            http.post(
                                connection.token_endpoint,
                                data={
                                    "grant_type": "refresh_token",
                                    "refresh_token": grant.refresh_token,
                                    "client_id": CLIENT_ID,
                                    "resource": self.resource,
                                },
                            )
                        )
                except httpx.HTTPError as exc:
                    raise AuthenticationError("Authentication service unavailable", 503) from exc
                grant = Grant(
                    access_token=body["access_token"],
                    refresh_token=body["refresh_token"],
                    subject=grant.subject,
                    expires_at=time.time() + float(body["expires_in"]),
                )
                if body.get("token_type", "").lower() != "bearer":
                    raise AuthenticationError("Unsupported token type")
                self._store().set_password(SERVICE, connection.key, grant.model_dump_json())
            return grant.access_token

    async def token(self) -> str:
        try:
            return await anyio.to_thread.run_sync(self._token)
        except (KeyringError, ValidationError, LockTimeout) as exc:
            raise AuthenticationError(
                "Secure credential storage unavailable; unlock your system keyring and retry", 503
            ) from exc

    async def headers(self, url: str) -> dict[str, str]:
        if _origin(url) != _origin(self.resource):
            return {}
        return {"Authorization": f"Bearer {await self.token()}"}

    def workspace_id(self, directory: Path) -> str:
        connection = self.connection or saved_connection()
        subject = connection.subject if connection and connection.resource == self.resource else "explicit"
        identity = f"{subject}\n{self.resource}\n{directory.resolve()}"
        return hashlib.sha256(identity.encode()).hexdigest()[:32]

    async def identity(self) -> dict:
        connection = self._connection()
        token = await self.token()
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                principal = _answer(
                    await http.post(
                        f"{connection.issuer}/session",
                        json={"resource": self.resource},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                )
        except httpx.HTTPError as exc:
            raise AuthenticationError("Authentication service unavailable", 503) from exc
        if (
            principal.get("subject") != connection.subject
            or principal.get("resource") != self.resource
            or principal.get("clientId") != CLIENT_ID
        ):
            raise AuthenticationError("Saved identity does not match this connection", 403)
        return principal

    async def logout(self) -> None:
        connection = self._connection()
        try:
            await anyio.to_thread.run_sync(self._logout, connection)
        except (KeyringError, ValidationError, LockTimeout) as exc:
            raise AuthenticationError(
                "Secure credential storage unavailable; unlock your system keyring and retry", 503
            ) from exc

    def _logout(self, connection: Connection) -> None:
        root = auth_directory()
        with FileLock(root / f"{connection.key}.lock", timeout=30):
            grant = self._read(connection)
            with auth_http() as http:
                response = http.post(
                    connection.revocation_endpoint,
                    data={
                        "client_id": CLIENT_ID,
                        "token": grant.refresh_token,
                        "token_type_hint": "refresh_token",
                    },
                )
                if not response.is_success:
                    _answer(response)
            try:
                self._store().delete_password(SERVICE, connection.key)
            except PasswordDeleteError:
                pass
            (root / f"{connection.target}.json").unlink(missing_ok=True)
            if saved_connection() is None:
                (root / "active.json").unlink(missing_ok=True)


def login(target: str, *, no_browser: bool = False, cancelled: threading.Event | None = None) -> Connection:
    resource, desktop_subject = target_resource(target)
    metadata = discover(resource)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    result: queue.Queue[str | AuthenticationError] = queue.Queue(maxsize=1)
    issuer = metadata["issuer"]

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Callback URLs contain authorization codes.

        def do_GET(self):
            url = urlsplit(self.path)
            values = parse_qs(url.query)
            valid = (
                url.path == "/callback"
                and self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
                and len(values.get("state", [])) == 1
                and hmac.compare_digest(values["state"][0], state)
            )
            valid = valid and ("iss" not in values or values["iss"] == [issuer])
            cancelled = "error" in values or len(values.get("code", [])) != 1
            self.send_response(302 if valid else 400)
            if valid:
                self.send_header(
                    "Location",
                    f"{issuer}/complete?"
                    + urlencode({"target": "cli", "status": "cancelled" if cancelled else "received"}),
                )
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not valid:
                self.wfile.write(b"Invalid authorization state")
                return
            self.wfile.write(
                b"Sign-in cancelled." if cancelled else b"Authorization received. Return to your terminal."
            )
            try:
                result.put_nowait(
                    AuthenticationError("Sign-in cancelled") if cancelled else values["code"][0]
                )
            except queue.Full:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    authorize = (
        metadata["authorization_endpoint"]
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": redirect,
                "scope": SCOPE,
                "resource": resource,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    print(f"Open this URL to sign in to {target}:\n{authorize}", file=sys.stderr, flush=True)
    try:
        if not no_browser:
            webbrowser.open(authorize)
        deadline = time.monotonic() + 600
        while True:
            if cancelled is not None and cancelled.is_set():
                raise AuthenticationError("Sign-in cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuthenticationError("Sign-in timed out. Run login again when ready.")
            try:
                code = result.get(timeout=min(0.2, remaining))
                break
            except queue.Empty:
                continue
        if isinstance(code, AuthenticationError):
            raise code
        with auth_http() as http:
            tokens = _answer(
                http.post(
                    metadata["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "client_id": CLIENT_ID,
                        "code": code,
                        "code_verifier": verifier,
                        "redirect_uri": redirect,
                        "resource": resource,
                    },
                )
            )
            principal = _answer(
                http.post(
                    f"{issuer}/session",
                    json={"resource": resource},
                    headers={"Authorization": f"Bearer {tokens['access_token']}"},
                )
            )
        if (
            principal.get("resource") != resource
            or principal.get("clientId") != CLIENT_ID
            or "agent" not in principal.get("capabilities", [])
        ):
            raise AuthenticationError("Authorization is not valid for this target", 403)
        if desktop_subject and principal.get("subject") != desktop_subject:
            raise AuthenticationError("Use the account currently signed in to Desktop", 403)
        connection = Connection(
            target,
            resource,
            issuer,
            principal["subject"],
            principal["email"],
            metadata["authorization_endpoint"],
            metadata["token_endpoint"],
            metadata["revocation_endpoint"],
        )
        if str(tokens.get("token_type", "")).lower() != "bearer":
            raise AuthenticationError("Unsupported token type")
        grant = Grant(
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            subject=principal["subject"],
            expires_at=time.time() + float(tokens["expires_in"]),
        )
        root = auth_directory()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(root / f"{connection.key}.lock", timeout=30):
            native_keyring().set_password(SERVICE, connection.key, grant.model_dump_json())
            _write_public(root / f"{target}.json", asdict(connection))
            _write_public(root / "active.json", {"target": target})
        return connection
    except (KeyringError, ValidationError, LockTimeout) as exc:
        raise AuthenticationError(
            "Secure credential storage unavailable; unlock your system keyring and retry", 503
        ) from exc
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
