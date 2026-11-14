"""A saved sign-in the service no longer honors, seen from the host: what the bridge reports and asks for."""

from __future__ import annotations

from dataclasses import asdict

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
