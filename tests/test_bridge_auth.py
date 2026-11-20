"""A saved sign-in the service no longer honors, seen from the host: what the bridge reports and asks for."""

from __future__ import annotations

import time
from dataclasses import asdict

import anyio
import httpx
import pytest
from tests.bridge_support import bridge_host
from tests.reference_remote import free_port
from tests.test_auth import MemoryKeyring, mock_http

from oh_my_cassette import auth
from oh_my_cassette.bridge.server import STATUS_TOOL

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("answer", "code", "retryable"),
    [
        (400, "bridge.unauthorized", False),  # OAuth `invalid_grant`: the refresh token is dead
        (503, "bridge.auth_unavailable", True),  # the sign-in service is down: nothing to do but wait
    ],
)
async def test_a_refresh_the_service_turns_down_is_reported_for_what_it_is(
    remote, bridge_settings, monkeypatch, tmp_path, answer, code, retryable
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
    grant = auth.Grant(access_token="expired", refresh_token="dead", expires_at=0, subject="account-a")
    store.set_password(auth.SERVICE, selected.key, grant.model_dump_json())
    refreshes: list[httpx.Request] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        refreshes.append(request)
        return httpx.Response(answer, json={"error": "invalid_grant" if answer == 400 else "unavailable"})

    mock_http(monkeypatch, token_endpoint)
    async with bridge_host(bridge_settings(remote.url, auth_token=None)) as (_, host):
        assert [tool.name for tool in (await host.list_tools()).tools] == [STATUS_TOOL]
        assert code in host.instructions
        status = await host.call_tool(STATUS_TOOL, {})
        assert status.structured_content["error_code"] == code
        assert status.content[0].text.startswith(f"{code}: ")  # not "the backend is not reachable"
        refused = await host.call_tool("slow_turn", {"steps": 1})
        assert refused.structured_content["error"]["code"] == code
        assert refused.structured_content["error"]["retryable"] is retryable
    assert remote.calls == []
    if answer == 400:
        assert len(refreshes) == 1  # a refresh token the service refused is never sent again


def _signed_in_for(resource: str, directory, monkeypatch) -> None:
    monkeypatch.setenv("CASSETTE_AUTH_DIRECTORY", str(directory))
    monkeypatch.setattr(auth, "native_keyring", MemoryKeyring)
    issuer = "https://auth.cassette.test"
    saved = auth.Connection(
        resource,
        issuer,
        "account-a",
        "a@cassette.test",
        f"{issuer}/auth",
        f"{issuer}/token",
        f"{issuer}/revoke",
    )
    auth._write_public(directory / "connection.json", asdict(saved))


async def test_a_mistyped_endpoint_is_reported_as_not_found_not_as_signed_out(
    remote, bridge_settings, monkeypatch, tmp_path
):
    _signed_in_for(remote.url, tmp_path / "auth", monkeypatch)
    wrong = remote.url.rsplit("/", 1)[0] + "/nope"
    async with bridge_host(bridge_settings(wrong, auth_token=None)) as (_, host):
        status = await host.call_tool(STATUS_TOOL, {})
        assert status.structured_content["error_code"] == "bridge.backend_unreachable"
        assert "HTTP 404" in status.structured_content["error"]
        assert "CASSETTE_MCP_URL" in status.structured_content["error"]


async def test_a_sign_in_saved_for_another_endpoint_says_which(
    remote, bridge_settings, monkeypatch, tmp_path
):
    _signed_in_for("https://mcp.elsewhere.test/mcp", tmp_path / "auth", monkeypatch)
    async with bridge_host(bridge_settings(remote.url, auth_token=None)) as (_, host):
        status = await host.call_tool(STATUS_TOOL, {})
        assert status.structured_content["error_code"] == "bridge.unauthorized"
        assert "https://mcp.elsewhere.test/mcp" in status.structured_content["error"]


async def _status_and_instructions(settings) -> tuple[dict, str]:
    async with bridge_host(settings) as (_, host):
        status = await host.call_tool(STATUS_TOOL, {})
        return status.structured_content, host.instructions


@pytest.mark.parametrize("saved_for", [None, "https://mcp.trycassette.online/mcp"])
async def test_without_a_sign_in_the_instructions_give_the_reason_the_status_tool_gives(
    remote, bridge_settings, monkeypatch, tmp_path, saved_for
):
    directory = tmp_path / "auth"
    monkeypatch.setenv("CASSETTE_AUTH_DIRECTORY", str(directory))
    monkeypatch.setattr(auth, "native_keyring", MemoryKeyring)
    if saved_for:
        _signed_in_for(saved_for, directory, monkeypatch)
    endpoints = {
        "missing": remote.url.rsplit("/", 1)[0] + "/nope",
        "unreachable": f"http://127.0.0.1:{free_port()}/mcp",
        "signed_out": remote.url,
    }
    expected = {
        "missing": ("bridge.backend_unreachable", "HTTP 404"),
        "unreachable": ("bridge.backend_unreachable", "refused the connection"),
        "signed_out": ("bridge.unauthorized", "oh-my-cassette login"),
    }
    for case, url in endpoints.items():
        status, instructions = await _status_and_instructions(bridge_settings(url, auth_token=None))
        code, phrase = expected[case]
        assert status["error_code"] == code, case
        assert phrase in status["error"], case
        assert f"{code}: {status['error']}" in instructions, case


async def test_a_saved_token_the_endpoint_stops_honoring_is_renewed_and_the_call_goes_through(
    remote, bridge_settings, monkeypatch, tmp_path
):
    directory = tmp_path / "auth"
    _signed_in_for(remote.url, directory, monkeypatch)
    store = MemoryKeyring()
    monkeypatch.setattr(auth, "native_keyring", lambda: store)
    saved = auth.saved_connection()
    store.set_password(
        auth.SERVICE,
        saved.key,
        auth.Grant(
            access_token="first",
            refresh_token="refresh",
            expires_at=time.time() + 3600,
            subject=saved.subject,
        ).model_dump_json(),
    )
    remote.options.token = "first"
    refreshes: list[httpx.Request] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        refreshes.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "second",
                "refresh_token": "rotated",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    mock_http(monkeypatch, token_endpoint)
    async with bridge_host(bridge_settings(remote.url, auth_token=None)) as (_, host):
        assert not (await host.call_tool("slow_turn", {"steps": 1})).is_error
        remote.options.token = "second"  # the first token was revoked (a new sign-in elsewhere) or expired
        renewed = await host.call_tool("slow_turn", {"steps": 1})
        assert not renewed.is_error, renewed.structured_content
    assert len(refreshes) == 1
    assert [call["tool"] for call in remote.calls] == ["slow_turn", "slow_turn"]


async def test_a_credential_that_cannot_be_read_fails_only_its_own_call(remote, bridge_settings):
    async with bridge_host(bridge_settings(remote.url)) as (bridge, host):
        credentials = bridge.upstream.credentials
        original = credentials.headers
        failing = False

        async def headers(url: str) -> dict[str, str]:
            if failing:
                raise auth.AuthenticationError(
                    "Secure credential storage did not answer (InitError); retry", 503
                )
            return await original(url)

        credentials.headers = headers
        results = {}

        async def long_call():
            results["long"] = await host.call_tool("slow_turn", {"steps": 5, "delay": 0.1})

        async with anyio.create_task_group() as tg:
            tg.start_soon(long_call)
            await anyio.sleep(0.15)
            failing = True
            results["short"] = await host.call_tool("slow_turn", {"steps": 1})
            failing = False
        assert results["short"].structured_content["error"]["code"] == "bridge.auth_unavailable"
        assert results["short"].structured_content["error"]["retryable"] is True
        assert not results["long"].is_error, results["long"].structured_content
        assert not (await host.call_tool("slow_turn", {"steps": 1})).is_error


async def test_an_unsigned_bridge_stays_quiet_until_sign_in_happens(
    remote, bridge_settings, monkeypatch, tmp_path
):
    from tests.reference_remote import REFERENCE_TOKEN

    from oh_my_cassette.bridge import upstream as upstream_mod

    monkeypatch.setattr(upstream_mod, "AUTH_WATCH_SEC", 0.05)
    directory = tmp_path / "auth"
    monkeypatch.setenv("CASSETTE_AUTH_DIRECTORY", str(directory))
    store = MemoryKeyring()
    monkeypatch.setattr(auth, "native_keyring", lambda: store)

    async with bridge_host(bridge_settings(remote.url, auth_token=None)) as (bridge, host):
        assert ".." not in host.instructions
        assert bridge.upstream._attempts == 1
        await anyio.sleep(1.3)  # longer than the initial 1 s reconnect delay
        assert bridge.upstream._attempts == 1  # idle while unauthorized: no background polling

        # Signing in from another process writes connection.json and wakes the bridge automatically.
        issuer = "https://auth.cassette.test"
        saved = auth.Connection(
            remote.url,
            issuer,
            "account-a",
            "a@cassette.test",
            f"{issuer}/auth",
            f"{issuer}/token",
            f"{issuer}/revoke",
        )
        grant = auth.Grant(
            access_token=REFERENCE_TOKEN,
            refresh_token="r",
            expires_at=time.time() + 3600,
            subject="account-a",
        )
        store.set_password(auth.SERVICE, saved.key, grant.model_dump_json())
        auth._write_public(directory / "connection.json", asdict(saved))
        with anyio.fail_after(5):
            while not bridge.serving:
                await anyio.sleep(0.05)
        assert "import_media" in [t.name for t in (await host.list_tools()).tools]


async def test_a_forbidden_call_is_isolated_to_its_own_call(remote, bridge_settings):
    results = {}
    async with bridge_host(bridge_settings(remote.url)) as (_, host):

        async def long_call():
            results["long"] = await host.call_tool("slow_turn", {"steps": 5, "delay": 0.1})

        async with anyio.create_task_group() as tg:
            tg.start_soon(long_call)
            while not remote.calls:
                await anyio.sleep(0.01)
            remote.options.refusals = [(403, None)]
            results["forbidden"] = await host.call_tool("slow_turn", {"steps": 1})
    assert results["forbidden"].structured_content["error"]["code"] == "bridge.forbidden"
    assert results["forbidden"].structured_content["error"]["retryable"] is False
    assert not results["long"].is_error, results["long"].structured_content


async def test_cleartext_non_loopback_mcp_url_is_refused_without_connecting(bridge_settings):
    async with bridge_host(bridge_settings("http://cassette.example.test/mcp")) as (_, host):
        status = await host.call_tool(STATUS_TOOL, {})
        assert status.structured_content["error_code"] == "bridge.forbidden"
        assert "HTTPS" in status.structured_content["error"]
