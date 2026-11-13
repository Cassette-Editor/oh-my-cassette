"""The account's own OpenAI key: kept in the keyring for the saved connection, shown only by its fingerprint,
and sent to the Cassette MCP service, and nowhere else, on the bridge's requests."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from tests.bridge_support import bridge_host
from tests.reference_remote import REFERENCE_TOKEN
from tests.test_auth import MemoryKeyring, connection

from oh_my_cassette import auth, cli, provider_key

KEY = "sk-proj-" + "a1B2" * 10


@pytest.fixture
def keyring_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CASSETTE_AUTH_DIRECTORY", str(tmp_path))
    store = MemoryKeyring()
    monkeypatch.setattr(auth, "native_keyring", lambda: store)
    return tmp_path, store


def test_the_fingerprint_is_the_one_cassette_shows():
    # Cassette's `providerKeyFingerprint` gives this key the same fingerprint.
    assert provider_key.fingerprint(KEY) == "81f10b9a…a1B2"


@pytest.mark.parametrize(
    ("key", "valid"),
    [
        (KEY, True),
        ("sk-" + "x" * 17, True),
        ("sk-short", False),
        ("pk-" + "x" * 30, False),
        (f"{KEY} trailing", False),
    ],
)
def test_only_an_openai_secret_key_is_taken(key, valid):
    assert provider_key.valid_key(key) is valid


def run_key(monkeypatch, capsys, action: str, typed: str = KEY) -> tuple[int, str]:
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: typed)
    with pytest.raises(SystemExit) as exited:
        cli.main(["key", action])
    captured = capsys.readouterr()
    return exited.value.code, captured.out + captured.err


def test_key_set_status_and_remove_keep_the_key_in_the_keyring_and_never_print_it(
    keyring_home, monkeypatch, capsys
):
    root, store = keyring_home
    selected = connection()
    auth._write_public(root / "connection.json", asdict(selected))

    code, output = run_key(monkeypatch, capsys, "set", f"  {KEY}\n")
    assert code == 0
    assert store.values[provider_key.SERVICE, selected.key] == KEY
    assert "81f10b9a…a1B2" in output and KEY not in output
    assert KEY not in "".join(path.read_text() for path in root.iterdir() if path.is_file())

    code, output = run_key(monkeypatch, capsys, "status")
    assert code == 0
    assert '"saved": true' in output and "81f10b9a…a1B2" in output and KEY not in output

    released: list[str] = []

    async def release(credentials):
        released.append(credentials.resource)

    monkeypatch.setattr(auth.Credentials, "release_provider_key", release)
    code, output = run_key(monkeypatch, capsys, "remove")
    assert code == 0
    assert released == [selected.resource]
    assert (provider_key.SERVICE, selected.key) not in store.values
    code, output = run_key(monkeypatch, capsys, "status")
    assert '"saved": false' in output


def test_key_remove_forgets_the_key_here_even_when_cassette_cannot_be_asked_to_let_go(
    keyring_home, monkeypatch, capsys
):
    root, store = keyring_home
    selected = connection()
    auth._write_public(root / "connection.json", asdict(selected))
    run_key(monkeypatch, capsys, "set")

    async def unreachable(_credentials):
        raise auth.AuthenticationError("Cassette could not be reached", 503)

    monkeypatch.setattr(auth.Credentials, "release_provider_key", unreachable)
    code, output = run_key(monkeypatch, capsys, "remove")
    assert code == 1 and "30 idle minutes" in output
    assert (provider_key.SERVICE, selected.key) not in store.values


@pytest.mark.anyio
async def test_releasing_the_key_asks_the_mcp_service_with_the_saved_sign_in(monkeypatch):
    sent: list[tuple[str, str, str | None]] = []

    def answer(request):
        sent.append((request.method, str(request.url), request.headers.get("authorization")))
        return auth.httpx.Response(204)

    real_client = auth.httpx.AsyncClient
    monkeypatch.setattr(
        auth.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=auth.httpx.MockTransport(answer), **kwargs),
    )
    credentials = auth.Credentials("https://mcp.example/mcp", explicit_token=REFERENCE_TOKEN)

    async def token():
        return REFERENCE_TOKEN

    monkeypatch.setattr(credentials, "token", token)
    await credentials.release_provider_key()
    assert sent == [("DELETE", "https://mcp.example/mcp/provider-key", f"Bearer {REFERENCE_TOKEN}")]


def test_key_set_refuses_what_is_no_openai_key_and_needs_a_sign_in(keyring_home, monkeypatch, capsys):
    root, store = keyring_home
    code, output = run_key(monkeypatch, capsys, "set")
    assert code == 1 and "login" in output
    auth._write_public(root / "connection.json", asdict(connection()))
    code, output = run_key(monkeypatch, capsys, "set", "not-a-key")
    assert code == 1 and "not-a-key" not in output
    assert store.values == {}


@pytest.mark.anyio
@pytest.mark.parametrize("saved", [True, False])
async def test_the_bridge_sends_a_saved_key_to_the_mcp_service(
    remote, bridge_settings, monkeypatch, tmp_path, saved
):
    directory = tmp_path / "auth"
    monkeypatch.setenv("CASSETTE_AUTH_DIRECTORY", str(directory))
    store = MemoryKeyring()
    monkeypatch.setattr(auth, "native_keyring", lambda: store)
    issuer = "https://auth.cassette.test"
    selected = auth.Connection(
        remote.url,
        issuer,
        "account-a",
        "a@cassette.test",
        f"{issuer}/auth",
        f"{issuer}/token",
        f"{issuer}/revoke",
    )
    auth._write_public(directory / "connection.json", asdict(selected))
    grant = auth.Grant(
        access_token=REFERENCE_TOKEN, refresh_token="r", expires_at=4_102_444_800, subject="account-a"
    )
    store.set_password(auth.SERVICE, selected.key, grant.model_dump_json())
    if saved:
        provider_key.save_key(store, selected.key, KEY)
    async with bridge_host(bridge_settings(remote.url, auth_token=None)) as (_, host):
        await host.call_tool("slow_turn", {"steps": 1})
    assert remote.request_headers
    assert {headers.get(provider_key.HEADER) for headers in remote.request_headers} == {
        KEY if saved else None
    }


@pytest.mark.anyio
async def test_a_key_is_never_sent_to_another_origin_or_with_a_token_override(keyring_home, monkeypatch):
    root, store = keyring_home
    selected = connection()
    auth._write_public(root / "connection.json", asdict(selected))
    provider_key.save_key(store, selected.key, KEY)
    credentials = auth.Credentials(selected.resource, keyring=store)
    assert await credentials.provider_key_headers("https://cassette.test/mcp") == {provider_key.HEADER: KEY}
    assert await credentials.provider_key_headers("https://elsewhere.test/mcp") == {}
    override = auth.Credentials(selected.resource, explicit_token="token", keyring=store)
    assert await override.provider_key_headers("https://cassette.test/mcp") == {}
