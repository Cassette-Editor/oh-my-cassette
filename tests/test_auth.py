import hashlib
import os
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
from keyring.errors import InitError, KeyringLocked

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


def connection(subject="account-a"):
    return auth.Connection(
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
    auth._write_public(root / "connection.json", asdict(selected))
    with ThreadPoolExecutor(max_workers=6) as pool:
        tokens = list(pool.map(lambda _: anyio.run(auth.Credentials(selected.resource).token), range(6)))
    assert tokens == ["new-secret"] * 6
    assert len(calls) == 1
    assert all("secret" not in file.read_text() for file in root.glob("*.json"))
    if os.name == "posix":  # Windows keeps no POSIX permission bits; the profile's ACL guards the file there
        assert (root / "connection.json").stat().st_mode & 0o777 == 0o600


def test_credentials_are_isolated_by_account_and_directory(environment):
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
    auth._write_public(root / "connection.json", asdict(c))
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


@pytest.mark.parametrize("revocation", [400, 401, None])
def test_logout_forgets_a_grant_the_service_no_longer_honors_or_none_at_all(
    environment, monkeypatch, revocation
):
    root, store = environment
    c = connection()
    auth._write_public(root / "connection.json", asdict(c))
    if revocation is not None:
        grant = auth.Grant(access_token="a", refresh_token="dead", expires_at=0, subject=c.subject)
        store.set_password(auth.SERVICE, c.key, grant.model_dump_json())
    revoked = []

    def handler(request):
        revoked.append(request)
        return httpx.Response(revocation or 500, json={"error": "invalid_grant"})

    mock_http(monkeypatch, handler)
    anyio.run(auth.Credentials(c.resource, connection=c).logout)
    assert auth.saved_connection() is None
    assert store.get_password(auth.SERVICE, c.key) is None
    assert len(revoked) == (0 if revocation is None else 1)  # no grant: nothing to revoke


def test_a_refused_refresh_asks_for_sign_in_and_is_not_sent_again(environment, monkeypatch):
    root, store = environment
    c = connection()
    auth._write_public(root / "connection.json", asdict(c))
    grant = auth.Grant(access_token="a", refresh_token="dead", expires_at=0, subject=c.subject)
    store.set_password(auth.SERVICE, c.key, grant.model_dump_json())
    refreshes = []

    def handler(request):
        refreshes.append(request)
        return httpx.Response(400, json={"error": "invalid_grant"})

    mock_http(monkeypatch, handler)
    credentials = auth.Credentials(c.resource)
    for _ in range(3):
        with pytest.raises(auth.AuthenticationError) as caught:
            anyio.run(credentials.token)
        assert caught.value.status == 401 and caught.value.needs_sign_in
        assert "oh-my-cassette login" in str(caught.value)
    assert len(refreshes) == 1
    renewed = auth.Grant(
        access_token="b", refresh_token="fresh", expires_at=time.time() + 3600, subject=c.subject
    )
    store.set_password(auth.SERVICE, c.key, renewed.model_dump_json())  # as `login` saves it
    assert anyio.run(credentials.token) == "b"


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
        assert params["device_name"] == [auth.device_name()]
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
            auth.login()
        assert not store.values
    else:
        assert auth.login() == c
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
        auth.login(cancelled=cancelled)
    assert len(opened) == 1
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", opened[0])) != 0
    assert not store.values
    assert not (root / "connection.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_auth_directory_is_kept_to_its_owner(environment):
    root, _ = environment
    root.chmod(0o755)
    assert auth.private_auth_directory() == root and root.stat().st_mode & 0o777 == 0o700


class LockedKeyring(MemoryKeyring):
    def get_password(self, service, username):
        raise KeyringLocked("Failed to unlock the collection!")


@pytest.mark.parametrize(
    "platform, expected",
    [
        ("linux", ["gnome-keyring-daemon --unlock", "dbus-run-session", "oh-my-cassette login"]),
        ("darwin", ["login keychain"]),
    ],
)
def test_a_keyring_that_cannot_be_read_says_what_to_do_on_this_system(
    environment, monkeypatch, platform, expected
):
    root, _ = environment
    c = connection()
    auth._write_public(root / "connection.json", asdict(c))
    monkeypatch.setattr(auth, "native_keyring", LockedKeyring)
    monkeypatch.setattr(auth.sys, "platform", platform)
    with pytest.raises(auth.AuthenticationError) as caught:
        anyio.run(auth.Credentials(c.resource, connection=c).token)
    assert caught.value.status == 503
    for text in expected:
        assert text in str(caught.value)


class CountingKeyring(MemoryKeyring):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def get_password(self, service, username):
        self.reads += 1
        time.sleep(0.001)  # a Secret Service read takes a D-Bus round trip
        return super().get_password(service, username)


def _saved_grant(root, store, *, access="secret", expires_in=3600.0):
    c = connection()
    auth._write_public(root / "connection.json", asdict(c))
    grant = auth.Grant(
        access_token=access, refresh_token="refresh", expires_at=time.time() + expires_in, subject=c.subject
    )
    store.set_password(auth.SERVICE, c.key, grant.model_dump_json())
    return c


def _renewing(monkeypatch, refreshes, access="renewed"):
    def handler(request):
        refreshes.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "refresh_token": "rotated",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    mock_http(monkeypatch, handler)


def test_a_token_is_renewed_while_it_still_outlives_the_longest_call(environment, monkeypatch):
    root, store = environment
    c = _saved_grant(root, store, access="expiring", expires_in=200)  # would expire during a 290 s wait
    refreshes = []
    _renewing(monkeypatch, refreshes)
    assert anyio.run(auth.Credentials(c.resource).token) == "renewed"
    assert len(refreshes) == 1


def test_a_burst_of_calls_reads_the_keyring_once(environment, monkeypatch):
    root, _ = environment
    store = CountingKeyring()
    monkeypatch.setattr(auth, "native_keyring", lambda: store)
    c = _saved_grant(root, store)
    credentials = auth.Credentials(c.resource)

    async def burst():
        tokens = []

        async def one():
            tokens.append(await credentials.token())
            await credentials.provider_key_headers(c.resource)

        async with anyio.create_task_group() as tg:
            for _ in range(240):
                tg.start_soon(one)
        return tokens

    assert anyio.run(burst) == ["secret"] * 240
    assert store.reads == 2  # the grant once, the OpenAI key once


def test_a_token_the_endpoint_refused_is_renewed_on_the_next_request(environment, monkeypatch):
    root, store = environment
    c = _saved_grant(root, store, access="revoked")
    refreshes = []
    _renewing(monkeypatch, refreshes)
    credentials = auth.Credentials(c.resource)
    assert anyio.run(credentials.token) == "revoked"
    assert credentials.reject("revoked")
    assert anyio.run(credentials.token) == "renewed"
    credentials.reject("revoked")  # a call that sent the old token answered late: the renewed one stays
    assert anyio.run(credentials.token) == "renewed"
    assert len(refreshes) == 1
    assert not auth.Credentials(c.resource, explicit_token="given").reject("given")


class FlakyKeyring(MemoryKeyring):
    def get_password(self, service, username):
        raise InitError("Failed to create the collection: D-Bus connection timed out")


def test_a_keyring_that_fails_for_a_moment_asks_for_a_retry_not_an_unlock(environment, monkeypatch):
    root, _ = environment
    c = connection()
    auth._write_public(root / "connection.json", asdict(c))
    monkeypatch.setattr(auth, "native_keyring", FlakyKeyring)
    monkeypatch.setattr(auth.sys, "platform", "linux")
    with pytest.raises(auth.AuthenticationError) as caught:
        anyio.run(auth.Credentials(c.resource, connection=c).token)
    assert caught.value.status == 503
    assert "retry" in str(caught.value) and "InitError" in str(caught.value)
    assert "unlocked" not in str(caught.value) and "gnome-keyring" not in str(caught.value)


def test_signing_in_again_revokes_the_grant_it_replaces(environment, monkeypatch):
    root, store = environment
    c = connection()
    auth._write_public(root / "connection.json", asdict(c))
    store.set_password(
        auth.SERVICE,
        c.key,
        auth.Grant(
            access_token="old", refresh_token="old-refresh", expires_at=0, subject=c.subject
        ).model_dump_json(),
    )
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
    revoked: list[str] = []

    def handler(request):
        if request.url.path == "/token":
            tokens = {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}
            return httpx.Response(200, json={**tokens, "token_type": "Bearer"})
        if request.url.path == "/revoke":
            revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200)
        principal = {
            "subject": c.subject,
            "email": c.email,
            "resource": c.resource,
            "clientId": auth.CLIENT_ID,
        }
        return httpx.Response(200, json={**principal, "capabilities": ["agent"]})

    original = mock_http(monkeypatch, handler)

    def browser(url):
        params = parse_qs(urlsplit(url).query)
        query = urlencode({"state": params["state"][0], "code": "one-time-code"})
        with original(timeout=3) as http:
            assert http.get(f"{params['redirect_uri'][0]}?{query}").status_code == 302

    monkeypatch.setattr(auth.webbrowser, "open", browser)
    assert auth.login() == c
    assert revoked == ["old-refresh"]  # the grant this machine held before, so the account lists it once
    assert (
        auth.Grant.model_validate_json(store.get_password(auth.SERVICE, c.key)).refresh_token == "new-refresh"
    )


def test_device_name_names_the_host_and_its_system(monkeypatch):
    monkeypatch.setattr(auth.platform, "node", lambda: "studio-mac.local")
    monkeypatch.setattr(auth.platform, "system", lambda: "Darwin")
    assert auth.device_name() == "studio-mac (macOS)"
    monkeypatch.setattr(auth.platform, "node", lambda: "")
    monkeypatch.setattr(auth.platform, "system", lambda: "Linux")
    assert auth.device_name() == "This computer (Linux)"


class UnwritableKeyring(MemoryKeyring):
    def set_password(self, service, username, password):
        raise KeyringLocked("Failed to unlock the collection!")


@pytest.mark.parametrize("no_browser", [False, True])
def test_login_without_a_usable_keyring_stops_before_the_browser(
    environment, monkeypatch, capsys, no_browser
):
    monkeypatch.setattr(auth, "native_keyring", UnwritableKeyring)
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    discovered, opened = [], []
    monkeypatch.setattr(auth, "discover", discovered.append)
    monkeypatch.setattr(auth.webbrowser, "open", opened.append)
    with pytest.raises(auth.AuthenticationError) as caught:
        auth.login(no_browser=no_browser)
    assert caught.value.status == 503 and "login keychain" in str(caught.value)
    assert discovered == [] and opened == []
    assert "Open this URL" not in capsys.readouterr().err


def test_the_keyring_check_before_login_leaves_nothing_behind(environment, monkeypatch):
    _, store = environment
    monkeypatch.setattr(
        auth, "discover", lambda resource: (_ for _ in ()).throw(auth.AuthenticationError("stop"))
    )
    with pytest.raises(auth.AuthenticationError, match="stop"):
        auth.login()
    assert store.values == {}


def test_an_unresolvable_authentication_host_is_named(environment, monkeypatch):
    def handler(request):
        raise httpx.ConnectError(
            "[Errno 8] nodename nor servname provided", request=request
        ) from socket.gaierror(8, "nodename nor servname provided")

    mock_http(monkeypatch, handler)
    with pytest.raises(auth.AuthenticationError) as caught:
        auth.discover("https://cassette.test/mcp")
    assert caught.value.status == 503
    assert "cassette.test" in str(caught.value) and "cannot resolve" in str(caught.value)


def test_cleartext_non_loopback_endpoints_never_receive_credentials():
    insecure = "http://cassette.test/mcp"
    credentials = auth.Credentials(insecure, explicit_token="secret-token")
    with pytest.raises(auth.AuthenticationError) as caught:
        anyio.run(credentials.headers, insecure)
    assert caught.value.status == 403 and "HTTPS" in str(caught.value)
