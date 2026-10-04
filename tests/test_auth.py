import hashlib
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
import httpx
import pytest
from keyring.backend import KeyringBackend

from oh_my_cassette import auth


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[service, username] = password

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


@pytest.fixture
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CASSETTE_AUTH_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("CASSETTE_MCP_URL", "https://cassette.test/mcp")
    store = MemoryKeyring()
    monkeypatch.setattr(auth, "native_keyring", lambda: store)
    return tmp_path, store


def connection(subject="account-a", target="web"):
    return auth.Connection(
        target,
        "https://cassette.test/mcp",
        "https://auth.cassette.test",
        subject,
        "a@cassette.test",
        "https://auth.cassette.test/auth",
        "https://auth.cassette.test/token",
        "https://auth.cassette.test/revoke",
    )


def mock_http(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        auth.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    return original


def test_credentials_rotate_once_under_competing_clients_and_never_write_tokens_to_config(
    environment, monkeypatch
):
    root, store = environment
    selected = connection()
    store.set_password(
        auth.SERVICE,
        selected.key,
        auth.Grant(
            access_token="expired-secret",
            refresh_token="refresh-secret",
            expires_at=0,
            subject=selected.subject,
        ).model_dump_json(),
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert parse_qs(request.content.decode())["resource"] == [selected.resource]
        return httpx.Response(
            200,
            json={
                "access_token": "new-secret",
                "refresh_token": "rotated-secret",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    mock_http(monkeypatch, handler)
    auth._write_public(root / "web.json", asdict(selected))
    auth._write_public(root / "active.json", {"target": "web"})
    with ThreadPoolExecutor(max_workers=6) as pool:
        tokens = list(pool.map(lambda _: anyio.run(auth.Credentials(selected.resource).token), range(6)))
    assert tokens == ["new-secret"] * 6
    assert len(calls) == 1
    assert all("secret" not in file.read_text() for file in root.glob("*.json"))
    assert (root / "web.json").stat().st_mode & 0o777 == 0o600


def test_credentials_are_isolated_by_account_target_and_directory(environment):
    root, store = environment
    a, b = connection(), connection("account-b")
    grant = auth.Grant(
        access_token="secret", refresh_token="refresh", expires_at=time.time() + 3600, subject=a.subject
    )
    store.set_password(auth.SERVICE, a.key, grant.model_dump_json())
    first = auth.Credentials(a.resource, connection=a)
    assert anyio.run(first.headers, "https://another.test/file") == {}
    assert anyio.run(first.headers, "https://cassette.test/file") == {"Authorization": "Bearer secret"}
    assert first.workspace_id(root) != first.workspace_id(root / "another")
    assert first.workspace_id(root) != auth.Credentials(b.resource, connection=b).workspace_id(root)
    with pytest.raises(auth.AuthenticationError, match="No saved grant"):
        anyio.run(auth.Credentials(b.resource, connection=b).token)


def test_logout_handles_standard_empty_response_and_retains_grant_when_offline(environment, monkeypatch):
    root, store = environment
    c = connection()
    auth._write_public(root / "web.json", asdict(c))
    auth._write_public(root / "active.json", {"target": "web"})
    store.set_password(
        auth.SERVICE,
        c.key,
        auth.Grant(
            access_token="expired", refresh_token="refresh", expires_at=0, subject=c.subject
        ).model_dump_json(),
    )
    online = False

    def handler(request):
        if not online:
            raise httpx.ConnectError("offline", request=request)
        assert parse_qs(request.content.decode())["token"] == ["refresh"]
        return httpx.Response(200)

    mock_http(monkeypatch, handler)
    credentials = auth.Credentials(c.resource, connection=c)
    with pytest.raises(auth.AuthenticationError) as caught:
        anyio.run(credentials.logout)
    assert caught.value.status == 503
    assert store.get_password(auth.SERVICE, c.key)
    online = True
    anyio.run(credentials.logout)
    assert auth.saved_connection() is None
    assert store.get_password(auth.SERVICE, c.key) is None


@pytest.mark.parametrize("cancel", [False, True])
def test_browser_callback_checks_state_and_pkce_and_finishes_cancel_without_another_browser(
    environment, monkeypatch, cancel
):
    _, store = environment
    c = connection()
    metadata = {
        "issuer": c.issuer,
        "authorization_endpoint": c.authorization_endpoint,
        "token_endpoint": c.token_endpoint,
        "revocation_endpoint": c.revocation_endpoint,
        "code_challenge_methods_supported": ["S256"],
    }
    opened = []

    def handler(request):
        if request.url.path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(200, json={"resource": c.resource, "authorization_servers": [c.issuer]})
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=metadata)
        if request.url.path == "/token":
            import base64

            body = parse_qs(request.content.decode())
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"][0].encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            assert opened[0]["code_challenge"] == [challenge]
            assert body["code"] == ["one-time-code"]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(
            200,
            json={
                "subject": c.subject,
                "email": c.email,
                "resource": c.resource,
                "clientId": auth.CLIENT_ID,
                "capabilities": ["agent"],
            },
        )

    original = mock_http(monkeypatch, handler)

    def browser(url):
        params = parse_qs(urlsplit(url).query)
        opened.append(params)
        assert params["code_challenge_method"] == ["S256"]
        callback = params["redirect_uri"][0]
        with original(timeout=3) as http:
            assert http.get(callback, params={"state": "wrong", "code": "bad"}).status_code == 400
            assert (
                http.get(
                    callback
                    + "?"
                    + urlencode(
                        {
                            "state": params["state"][0],
                            "error" if cancel else "code": "access_denied" if cancel else "one-time-code",
                        }
                    )
                ).status_code
                == 302
            )

    monkeypatch.setattr(auth.webbrowser, "open", browser)
    if cancel:
        with pytest.raises(auth.AuthenticationError, match="cancelled"):
            auth.login("web")
        assert not store.values
    else:
        assert auth.login("web") == c
        assert auth.saved_connection() == c
    assert len(opened) == 1


def test_discovery_rejects_resource_and_issuer_confusion(environment, monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={"resource": "https://evil.test/mcp"}))
    with pytest.raises(auth.AuthenticationError, match="does not match"):
        auth.discover("https://cassette.test/mcp")


def test_login_interruption_closes_callback_and_saves_no_grant(environment, monkeypatch):
    root, store = environment
    c = connection()
    monkeypatch.setattr(
        auth,
        "discover",
        lambda resource: {
            "issuer": c.issuer,
            "authorization_endpoint": c.authorization_endpoint,
            "token_endpoint": c.token_endpoint,
            "revocation_endpoint": c.revocation_endpoint,
        },
    )
    cancelled = threading.Event()
    opened = []

    def browser(url):
        callback = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0])
        opened.append(callback.port)
        with socket.create_connection(("127.0.0.1", callback.port), timeout=1):
            pass
        cancelled.set()

    monkeypatch.setattr(auth.webbrowser, "open", browser)
    with pytest.raises(auth.AuthenticationError, match="cancelled"):
        auth.login("web", cancelled=cancelled)
    assert len(opened) == 1
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", opened[0])) != 0
    assert not store.values
    assert not (root / "web.json").exists()
