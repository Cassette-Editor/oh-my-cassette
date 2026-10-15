"""The bridge against the reference backend over real HTTP, from both host protocol eras."""

from __future__ import annotations

import hashlib
import uuid

import anyio
import httpx
import pytest
from mcp.shared.exceptions import MCPError
from tests.bridge_support import bridge_host
from tests.reference_remote import ReferenceRemote, RemoteOptions, free_port, serve_remote

from oh_my_cassette.bridge import downloads
from oh_my_cassette.bridge.downloads import Downloads
from oh_my_cassette.bridge.errors import BridgeError
from oh_my_cassette.bridge.server import STATUS_TOOL
from oh_my_cassette.contract import META_ELAPSED, META_HOST, META_WORKSPACE, DownloadSpec

pytestmark = pytest.mark.anyio

HOST_MODES = ["auto", "legacy"]  # 2026-07-28 per-request host, and a handshake-era host


@pytest.mark.parametrize("mode", HOST_MODES)
async def test_mirrors_backend_tools_and_instructions(remote, bridge_settings, mode):
    async with bridge_host(bridge_settings(remote.url), mode=mode) as (_, host):
        tools = {tool.name: tool for tool in (await host.list_tools()).tools}
        assert list(tools) == [
            "import_media",
            "set_cover",
            "slow_turn",
            "silent_turn",
            "export_video",
            "add_late_tool",
            "run_turn",
        ]
        assert host.instructions == remote.options.instructions
        # a tool taking local files may answer `preparing`, which its own output schema does not describe
        assert tools["import_media"].output_schema is None and tools["set_cover"].output_schema is None
        assert tools["slow_turn"].output_schema is not None
        with pytest.raises(MCPError):  # bridge-only tools are hidden and refused
            await host.call_tool("cassette_upload_begin", {"files": []})
        with pytest.raises(MCPError):
            await host.call_tool("cassette_upload_parts", {"uploadId": "u", "role": "original"})


@pytest.mark.parametrize("mode", HOST_MODES)
async def test_forwards_progress_and_context(remote, bridge_settings, mode):
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    settings = bridge_settings(remote.url, host_name="unit-host")
    async with bridge_host(settings, mode=mode) as (_, host):
        result = await host.call_tool("slow_turn", {"steps": 3}, progress_callback=on_progress)
    assert result.structured_content == {"status": "completed", "steps": 3}
    assert [message for _, _, message in seen] == ["step 1", "step 2", "step 3"]
    meta = remote.calls[-1]["meta"]
    workspace = settings.workspace
    assert meta[META_WORKSPACE] == {
        "id": hashlib.sha256(f"explicit\n{settings.mcp_url}\n{workspace}".encode()).hexdigest()[:32],
        "name": workspace.name,
    }
    assert meta[META_HOST]["name"]  # the host's clientInfo wins over CASSETTE_MCP_HOST
    assert 0 <= meta[META_ELAPSED] < 5


async def test_sends_bearer_token_upstream(remote, bridge_settings):
    async with bridge_host(bridge_settings(remote.url)) as (_, host):
        await host.call_tool("slow_turn", {"steps": 1})
    assert remote.request_headers[-1]["authorization"] == "Bearer reference-token"


@pytest.mark.parametrize("token", [None, "wrong-token"])
async def test_a_refused_token_is_reported_as_unauthorized(remote, bridge_settings, token):
    async with bridge_host(bridge_settings(remote.url, auth_token=token)) as (_, host):
        assert [tool.name for tool in (await host.list_tools()).tools] == [STATUS_TOOL]
        assert "bridge.unauthorized" in host.instructions
        status = await host.call_tool(STATUS_TOOL, {})
        assert status.structured_content["error_code"] == "bridge.unauthorized"
        assert "oh-my-cassette login" in status.structured_content["error"]
        refused = await host.call_tool("slow_turn", {"steps": 1})
        assert refused.structured_content["error"]["code"] == "bridge.unauthorized"
        assert refused.structured_content["error"]["retryable"] is False
    assert remote.calls == []


async def test_a_token_revoked_mid_session_is_reported_per_call(remote, bridge_settings):
    async with bridge_host(bridge_settings(remote.url)) as (_, host):
        assert not (await host.call_tool("slow_turn", {"steps": 1})).is_error
        remote.options.token = "rotated"
        refused = await host.call_tool("slow_turn", {"steps": 1})
        assert refused.structured_content["error"]["code"] == "bridge.unauthorized"


async def test_a_silent_connection_detaches_and_says_the_work_may_go_on(remote, bridge_settings):
    settings = bridge_settings(remote.url, read_timeout_sec=0.5)
    async with bridge_host(settings) as (_, host):
        with anyio.fail_after(10):
            dropped = await host.call_tool("silent_turn", {"seconds": 2})
        error = dropped.structured_content["error"]
        assert error["code"] == "bridge.backend_unreachable" and error["retryable"] is True
        assert "may still be running" in error["message"]
        with anyio.fail_after(10):  # the bridge reconnects for the next call
            while (await host.call_tool("slow_turn", {"steps": 1})).is_error:
                await anyio.sleep(0.2)


@pytest.mark.parametrize("mode", HOST_MODES)
async def test_downloads_declared_artifacts_into_the_workspace(remote, bridge_settings, mode):
    settings = bridge_settings(remote.url)
    async with bridge_host(settings, mode=mode) as (_, host):
        first = await host.call_tool("export_video", {"name": "final"})
        second = await host.call_tool("export_video", {"name": "final"})
    exports = settings.workspace / "cassette-exports"
    assert first.structured_content["file"] == str(exports / "final.mp4")
    assert second.structured_content["file"] == str(exports / "final (1).mp4")
    assert (exports / "final.mp4").read_bytes() == b"FAKE-MP4-" * 4096
    assert "Downloaded final.mp4" in first.content[-1].text
    assert sorted(p.name for p in exports.iterdir()) == ["final (1).mp4", "final.mp4"]


async def test_bad_download_keeps_the_url_and_leaves_no_file(remote, bridge_settings):
    settings = bridge_settings(remote.url)
    async with bridge_host(settings) as (_, host):
        result = await host.call_tool("export_video", {"name": "broken", "corrupt": True})
    assert result.structured_content["file"].startswith("http://127.0.0.1")
    assert "sha256 mismatch" in result.content[-1].text
    assert list((settings.workspace / "cassette-exports").iterdir()) == []


def _export(remote: ReferenceRemote) -> tuple[bytes, list[str]]:
    data = b"FAKE-MP4-" * 4096
    return data, [h.get("range", "") for h in remote.download_headers]


async def test_a_download_cut_short_resumes_from_where_it_stopped(remote, bridge_settings):
    settings = bridge_settings(remote.url)
    remote.options.cut_downloads = 1
    async with bridge_host(settings) as (_, host):
        result = await host.call_tool("export_video", {"name": "final"})
    data, ranges = _export(remote)
    assert (settings.workspace / "cassette-exports/final.mp4").read_bytes() == data
    assert ranges == ["", f"bytes={len(data) // 2}-"]
    assert remote.download_headers[1]["if-range"].startswith('"')  # the ETag the cut answer carried
    assert "Downloaded final.mp4" in result.content[-1].text
    assert [p.name for p in (settings.workspace / "cassette-exports").iterdir()] == ["final.mp4"]


async def test_a_download_the_server_will_not_resume_starts_over(remote, bridge_settings):
    settings = bridge_settings(remote.url)
    remote.options.cut_downloads, remote.options.ignore_ranges = 1, True
    async with bridge_host(settings) as (_, host):
        await host.call_tool("export_video", {"name": "final"})
    data, ranges = _export(remote)
    assert ranges == ["", f"bytes={len(data) // 2}-"]  # asked to resume, answered 200 with the whole file
    assert (settings.workspace / "cassette-exports/final.mp4").read_bytes() == data


async def test_a_download_that_keeps_failing_keeps_its_part_for_the_next_call(
    remote, bridge_settings, monkeypatch
):
    monkeypatch.setattr(downloads, "DOWNLOAD_ATTEMPTS", 1)
    settings = bridge_settings(remote.url)
    data = b"0123456789" * 1000
    remote.files["kept"] = data
    spec = DownloadSpec(
        url=f"{remote.base_url}/files/kept",
        file_name="kept.bin",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        pointer=None,
    )
    async with httpx.AsyncClient() as http:
        fetcher = Downloads(settings, http)
        remote.options.cut_downloads = 1
        with pytest.raises(BridgeError) as caught:
            await fetcher.fetch(spec)
        assert caught.value.code == "bridge.download_failed" and caught.value.retryable
        (part,) = settings.downloads.iterdir()
        assert part.name.startswith(".kept.bin.") and part.read_bytes() == data[: len(data) // 2]
        path = await Downloads(settings, http).fetch(spec)  # a fresh process: no ETag known
    assert path.read_bytes() == data and [p.name for p in settings.downloads.iterdir()] == ["kept.bin"]
    assert [h.get("range") for h in remote.download_headers] == [None, f"bytes={len(data) // 2}-"]
    assert "if-range" not in remote.download_headers[1]


async def test_a_call_that_names_its_request_is_sent_again_after_a_dropped_connection(
    remote, bridge_settings
):
    remote.options.drop_turn_once = True
    async with bridge_host(bridge_settings(remote.url)) as (_, host):
        with anyio.fail_after(30):
            result = await host.call_tool("run_turn", {"message": "brighter"})
    assert not result.is_error, result
    assert result.structured_content == {"status": "done", "message": "brighter", "turn": 1}
    first, again = remote.calls
    request_id = first["arguments"]["client_request_id"]
    assert uuid.UUID(request_id) and again["arguments"] == first["arguments"]
    assert remote.acted == [request_id]  # sent twice, acted on once
    assert again["meta"][META_ELAPSED] > first["meta"][META_ELAPSED]


async def test_a_refused_named_request_waits_as_told_and_is_sent_again(remote, bridge_settings):
    remote.options.refusals = [(503, 0), (429, 1)]
    async with bridge_host(bridge_settings(remote.url)) as (_, host):
        with anyio.fail_after(30):
            result = await host.call_tool("run_turn", {"message": "cut", "client_request_id": "host-chosen"})
    assert not result.is_error, result
    assert remote.refused == [503, 429]
    assert [call["arguments"]["client_request_id"] for call in remote.calls] == ["host-chosen"]


async def test_a_named_request_whose_wait_ran_out_is_picked_up_by_the_next_identical_call(
    remote, bridge_settings
):
    remote.options.drop_turn_once = True
    async with bridge_host(bridge_settings(remote.url, local_wait_sec=0.5)) as (_, host):
        with anyio.fail_after(30):
            dropped = await host.call_tool("run_turn", {"message": "warmer"})
            error = dropped.structured_content["error"]
            assert (
                error["code"] == "bridge.backend_unreachable" and "may still be running" in error["message"]
            )
            while (again := await host.call_tool("run_turn", {"message": "warmer"})).is_error:
                await anyio.sleep(0.2)
            fresh = await host.call_tool("run_turn", {"message": "warmer"})
    assert again.structured_content["turn"] == 1  # the first request's answer, not a second turn
    assert fresh.structured_content["turn"] == 2  # once settled, the same words are a new request
    ids = [call["arguments"]["client_request_id"] for call in remote.calls]
    assert ids[0] == ids[-2] != ids[-1]


async def test_a_rate_limited_call_reports_the_wait_and_keeps_the_connection(remote, bridge_settings):
    remote.options.refusals = [(429, 7)]
    async with bridge_host(bridge_settings(remote.url)) as (bridge, host):
        attempts = bridge.upstream._attempts
        refused = await host.call_tool("slow_turn", {"steps": 1})
        assert refused.structured_content["error"] == {
            "code": "bridge.rate_limited",
            "message": refused.structured_content["error"]["message"],
            "retryable": True,
            "retry_after_seconds": 7,
        }
        assert "HTTP 429" in refused.structured_content["error"]["message"]
        assert bridge.upstream.connected
        assert not (await host.call_tool("slow_turn", {"steps": 1})).is_error
        assert bridge.upstream._attempts == attempts  # no reconnect
    assert remote.refused == [429] and len(remote.calls) == 1


async def test_a_refused_call_says_cassette_did_not_take_it(remote, bridge_settings):
    remote.options.refusals = [(503, 2)]
    async with bridge_host(bridge_settings(remote.url)) as (_, host):
        refused = await host.call_tool("slow_turn", {"steps": 1})
        error = refused.structured_content["error"]
        assert error["code"] == "bridge.backend_unreachable" and error["retry_after_seconds"] == 2
        assert "did not take the call" in error["message"] and "during the call" not in error["message"]
        with anyio.fail_after(15):
            while (await host.call_tool("slow_turn", {"steps": 1})).is_error:
                await anyio.sleep(0.2)
    assert remote.calls[0]["tool"] == "slow_turn" and len(remote.calls) == 1


async def test_backend_tool_list_changes_reach_a_legacy_host(remote, bridge_settings):
    changed = anyio.Event()

    async def on_message(message):
        if getattr(message, "method", None) == "notifications/tools/list_changed":
            changed.set()

    async with bridge_host(bridge_settings(remote.url), mode="legacy", message_handler=on_message) as (
        _,
        host,
    ):
        await host.list_tools()
        await host.call_tool("add_late_tool", {})
        with anyio.fail_after(5):
            await changed.wait()
        assert "late_tool" in [tool.name for tool in (await host.list_tools()).tools]


async def test_backend_tool_list_changes_reach_a_modern_host(remote, bridge_settings):
    async with bridge_host(bridge_settings(remote.url)) as (bridge, host):
        async with host.listen(tools_list_changed=True) as subscription:
            await host.call_tool("add_late_tool", {})
            with anyio.fail_after(5):
                await subscription.__anext__()
        assert bridge.upstream.tool("late_tool") is not None


@pytest.mark.parametrize("mode", HOST_MODES)
async def test_offline_start_exposes_only_the_status_tool_then_recovers(bridge_settings, mode):
    port = free_port()
    remote = ReferenceRemote()
    settings = bridge_settings(f"http://127.0.0.1:{port}/mcp")
    async with bridge_host(settings, mode=mode) as (bridge, host):
        assert [tool.name for tool in (await host.list_tools()).tools] == [STATUS_TOOL]
        assert STATUS_TOOL in host.instructions
        down = await host.call_tool(STATUS_TOOL, {})
        assert down.structured_content["connected"] is False and down.structured_content["error"]
        blocked = await host.call_tool("import_media", {"paths": []})
        assert (
            blocked.is_error and blocked.structured_content["error"]["code"] == "bridge.backend_unreachable"
        )

        async with serve_remote(remote, port=port):
            up = await host.call_tool(STATUS_TOOL, {})
            assert up.structured_content["connected"] is True
            assert up.structured_content["server"] == {"name": "reference-cassette", "version": "9.9.9"}
            assert "import_media" in [tool.name for tool in (await host.list_tools()).tools]
            assert bridge.serving


async def test_reconnects_after_a_backend_restart(bridge_settings):
    port = free_port()
    settings = bridge_settings(f"http://127.0.0.1:{port}/mcp")
    first = ReferenceRemote()
    async with serve_remote(first, port=port), bridge_host(settings) as (bridge, host):
        assert not (await host.call_tool("slow_turn", {"steps": 1})).is_error
        # a deploy: the backend process dies ...
        await first.crash()
        with anyio.fail_after(10):  # ... and the next call fails promptly instead of hanging
            failed = await host.call_tool("slow_turn", {"steps": 1})
        assert failed.is_error and failed.structured_content["error"]["code"] == "bridge.backend_unreachable"
        assert failed.structured_content["error"]["retryable"] is True
        # ... then comes back as a new process on the same port
        second = ReferenceRemote()
        async with serve_remote(second, port=port):
            with anyio.fail_after(15):
                while (await host.call_tool("slow_turn", {"steps": 1})).is_error:
                    await anyio.sleep(0.2)
            assert bridge.serving and len(second.calls) == 1


async def test_handshake_era_backend_is_refused(bridge_settings):
    remote = ReferenceRemote(options=RemoteOptions(handshake_only=True))
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (bridge, host):
        assert [tool.name for tool in (await host.list_tools()).tools] == [STATUS_TOOL]
        assert "bridge.protocol_unsupported" in host.instructions
        status = await host.call_tool(STATUS_TOOL, {})
        assert status.structured_content["connected"] is False
        assert status.structured_content["error_code"] == "bridge.protocol_unsupported"
        assert "speaks only MCP 2025-11-25" in status.structured_content["error"]
        refused = await host.call_tool("slow_turn", {"steps": 1})
        assert refused.is_error
        assert refused.structured_content["error"] == {
            "code": "bridge.protocol_unsupported",
            "message": status.structured_content["error"],
            "retryable": False,
        }
        assert not bridge.upstream.connected and remote.calls == []


@pytest.mark.parametrize(
    "options, reason",
    [
        (RemoteOptions(contract_version=2), "contract v2"),
        (RemoteOptions(min_bridge_version="99.0.0"), ">= 99.0.0"),
    ],
)
async def test_incompatible_backend_asks_for_an_upgrade(bridge_settings, options, reason):
    remote = ReferenceRemote(options=options)
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
        assert [tool.name for tool in (await host.list_tools()).tools] == [STATUS_TOOL]
        status = await host.call_tool(STATUS_TOOL, {})
        assert (
            status.structured_content["compatible"] is False and reason in status.structured_content["error"]
        )
        refused = await host.call_tool("slow_turn", {"steps": 1})
        assert refused.structured_content["error"]["code"] == "bridge.upgrade_required"
        assert remote.calls == []


async def test_undeclared_contract_is_a_plain_proxy(bridge_settings):
    remote = ReferenceRemote(options=RemoteOptions(declare_contract=False))
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
        result = await host.call_tool("slow_turn", {"steps": 1})
    assert result.structured_content["status"] == "completed"
