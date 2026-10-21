"""The local phase runs in the background: bounded waits, `preparing`, attaching, caching, failures."""

from __future__ import annotations

import shutil
from pathlib import Path

import anyio
import pytest
from tests.bridge_support import FakePreparer, bridge_host

from oh_my_cassette.contract import META_ELAPSED

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
CLIP = {"paths": ["media/clip.mp4"], "project_id": "p1"}


@pytest.fixture
def workspace(bridge_settings) -> Path:
    root = bridge_settings("http://unused").workspace
    (root / "media").mkdir()
    shutil.copy(FIXTURES / "clip.mp4", root / "media" / "clip.mp4")
    return root


async def _until_settled(host, arguments, *, limit: float = 15):
    with anyio.fail_after(limit):
        while True:
            result = await host.call_tool("import_media", arguments)
            if (result.structured_content or {}).get("status") != "preparing":
                return result


async def test_a_slow_local_phase_answers_preparing_and_later_calls_attach(
    remote, bridge_settings, workspace
):
    preparer = FakePreparer(delay=1.5)
    settings = bridge_settings(remote.url, local_wait_sec=0.3)
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async with bridge_host(settings, preparer=preparer) as (_, host):
        first = await host.call_tool("import_media", CLIP, progress_callback=on_progress)
        assert not first.is_error
        assert first.structured_content["status"] == "preparing"
        progress = first.structured_content["progress"]
        assert progress["done"] == 0 and progress["total"] == 1
        (file,) = progress["files"]
        assert file["path"] == "media/clip.mp4" and file["stage"] == "transcoding"
        assert "call import_media again with the same arguments" in first.content[0].text.lower()
        assert seen and seen[0][1] == 100.0 and "media/clip.mp4" in seen[0][2]

        second = await host.call_tool("import_media", CLIP)
        assert second.structured_content["status"] == "preparing"
        assert preparer.transcodes == 1  # attached to the running task instead of starting another
        assert remote.calls == []  # nothing reaches the backend's tool while the files are local

        done = await _until_settled(host, CLIP)
        assert not done.is_error, done
        assert done.structured_content["refs"][0].startswith("media:")
        assert preparer.transcodes == 1 and len(remote.begun) == 1
        forwarded = remote.calls[-1]
        assert forwarded["arguments"] == {"paths": done.structured_content["refs"], "project_id": "p1"}
        assert 0 <= forwarded["meta"][META_ELAPSED] <= settings.local_wait_sec + 0.5

        # the refs are cached for the bridge's lifetime: the same call goes straight to the backend
        again = await host.call_tool("import_media", CLIP)
        assert again.structured_content["refs"] == done.structured_content["refs"]
        assert preparer.transcodes == 1 and len(remote.begun) == 1
    assert [value for value, _, _ in seen] == sorted(value for value, _, _ in seen)


async def test_other_arguments_are_other_local_work(remote, bridge_settings, workspace):
    preparer = FakePreparer()
    async with bridge_host(bridge_settings(remote.url), preparer=preparer) as (_, host):
        await host.call_tool("import_media", CLIP)
        await host.call_tool("import_media", {**CLIP, "project_id": "p2"})
    assert preparer.transcodes == 2
    assert [b["arguments"]["project_id"] for b in remote.begun] == ["p1", "p2"]


async def test_a_failure_within_the_window_is_reported_and_the_next_call_retries(
    remote, bridge_settings, workspace
):
    preparer = FakePreparer(failures=1)
    async with bridge_host(bridge_settings(remote.url), preparer=preparer) as (_, host):
        failed = await host.call_tool("import_media", CLIP)
        assert failed.is_error and failed.structured_content["error"]["code"] == "bridge.prepare_failed"
        assert "fake failure" in failed.structured_content["error"]["message"]
        retried = await host.call_tool("import_media", CLIP)
    assert not retried.is_error and preparer.transcodes == 2


async def test_a_failure_nobody_waited_for_is_reported_once_then_forgotten(
    remote, bridge_settings, workspace
):
    preparer = FakePreparer(delay=0.6, failures=1)
    settings = bridge_settings(remote.url, local_wait_sec=0.1)
    async with bridge_host(settings, preparer=preparer) as (_, host):
        first = await host.call_tool("import_media", CLIP)
        assert first.structured_content["status"] == "preparing"
        await anyio.sleep(1.0)  # the task fails in the background
        failed = await host.call_tool("import_media", CLIP)
        assert failed.structured_content["error"]["code"] == "bridge.prepare_failed"
        assert preparer.transcodes == 1
        done = await _until_settled(host, CLIP)
    assert not done.is_error and preparer.transcodes == 2
    assert list(settings.temp_root.iterdir()) == []
