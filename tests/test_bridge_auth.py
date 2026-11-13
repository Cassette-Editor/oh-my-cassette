"""A saved sign-in the service no longer honors, seen from the host: what the bridge reports and asks for."""

from __future__ import annotations

from dataclasses import asdict

import httpx
import pytest
from tests.bridge_support import bridge_host
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
