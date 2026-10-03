"""Local file arguments: the local policy, preparation and the upload handshake (contract §3)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from tests.bridge_support import FakePreparer, bridge_host
from tests.reference_remote import ReferenceRemote, RemoteOptions, serve_remote

from oh_my_cassette.bridge.files import guess_mime

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"


def _error(result) -> str:
    assert result.is_error, result
    return result.structured_content["error"]["code"]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def workspace(bridge_settings) -> Path:
    root = bridge_settings("http://unused").workspace
    (root / "media").mkdir()
    for name in ("slide.png", "tone.wav", "clip.mp4"):
        shutil.copy(FIXTURES / name, root / "media" / name)
    shutil.copy(FIXTURES / "slide.png", root / "media" / "photo.heic")  # bytes only matter to the fake
    (root / "notes.txt").write_text("not media")
    return root


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_accepted_files_upload_unchanged_and_refs_replace_paths(
    remote, bridge_settings, workspace, mode
):
    async with bridge_host(bridge_settings(remote.url), mode=mode) as (_, host):
        result = await host.call_tool(
            "import_media",
            {
                "project_id": "p1",
                "paths": ["media/slide.png", str(workspace / "media/tone.wav"), "media/slide.png"],
            },
        )
    assert not result.is_error, result
    refs = result.structured_content["refs"]
    assert all(ref.startswith("media:") for ref in refs) and refs[0] == refs[2] != refs[1]
    assert remote.calls[-1]["arguments"] == {"paths": refs, "project_id": "p1"}
    assert len(remote.put_headers) == 2  # the repeated path is uploaded once
    assert remote.blobs[refs[0]] == {"original": (FIXTURES / "slide.png").read_bytes()}

    png, wav = remote.begun
    # the backend sees the call's arguments with every local path replaced by its clientRef
    assert png["tool"] == "import_media"
    assert png["arguments"] == {"project_id": "p1", "paths": ["f0", "f1", "f0"]}
    data = (FIXTURES / "slide.png").read_bytes()
    assert {k: png[k] for k in ("clientRef", "name", "relativePath", "size", "sha256", "mimeType")} == {
        "clientRef": "f0",
        "name": "slide.png",
        "relativePath": "media/slide.png",
        "size": len(data),
        "sha256": _sha(data),
        "mimeType": "image/png",
    }
    assert png["artifacts"] == [
        {"role": "original", "size": len(data), "sha256": _sha(data), "mimeType": "image/png"}
    ]
    assert png["preparation"] is None and wav["preparation"] is None
    assert [h["content-type"] for h in remote.put_headers] == ["image/png", guess_mime(FIXTURES / "tone.wav")]


async def test_known_content_is_not_uploaded_again(remote, bridge_settings, workspace):
    shutil.copy(workspace / "media/slide.png", workspace / "copy.png")
    async with bridge_host(bridge_settings(remote.url)) as (_, host):
        first = await host.call_tool("set_cover", {"image": "media/slide.png"})
        second = await host.call_tool("set_cover", {"image": "copy.png"})
    assert first.structured_content["ref"] == second.structured_content["ref"]
    assert len(remote.put_headers) == 1  # begin answered the copy with the existing ref
    assert len(remote.uploads) == 1


async def test_video_uploads_every_rendition_under_the_source_identity(remote, bridge_settings, workspace):
    preparer = FakePreparer()
    settings = bridge_settings(remote.url)
    async with bridge_host(settings, preparer=preparer) as (_, host):
        result = await host.call_tool("import_media", {"paths": ["media/clip.mp4"]})
    assert not result.is_error, result
    (ref,) = result.structured_content["refs"]
    (begun,) = remote.begun
    source = (FIXTURES / "clip.mp4").read_bytes()
    assert (begun["name"], begun["size"], begun["sha256"], begun["mimeType"]) == (
        "clip.mp4",
        len(source),
        _sha(source),
        "video/mp4",
    )
    assert [a["role"] for a in begun["artifacts"]] == ["canonical", "preview"]
    assert begun["preparation"] == {"fake": True, "source": "clip.mp4", "roles": ["canonical", "preview"]}
    blobs = remote.blobs[ref]
    for artifact in begun["artifacts"]:
        assert artifact["mimeType"] == "video/mp4"
        assert artifact["sha256"] == _sha(blobs[artifact["role"]]) and artifact["size"] == len(
            blobs[artifact["role"]]
        )
    assert "original" not in blobs  # the source itself never leaves the machine
    assert [h["content-type"] for h in remote.put_headers] == ["video/mp4", "video/mp4"]
    assert all(h["content-length"] for h in remote.put_headers)
    assert list(settings.temp_root.iterdir()) == []  # intermediate files are gone once uploaded


async def test_unaccepted_media_is_converted_before_upload(remote, bridge_settings, workspace):
    preparer = FakePreparer()
    async with bridge_host(bridge_settings(remote.url), preparer=preparer) as (_, host):
        result = await host.call_tool("import_media", {"paths": ["media/photo.heic"]})
    assert not result.is_error, result
    (begun,) = remote.begun
    converted = b"converted:" + (FIXTURES / "slide.png").read_bytes()
    assert begun["name"] == "photo.jpg" and begun["relativePath"] == "media/photo.heic"
    assert (begun["mimeType"], begun["size"], begun["sha256"]) == (
        "image/jpeg",
        len(converted),
        _sha(converted),
    )
    assert begun["artifacts"] == [
        {"role": "original", "size": len(converted), "sha256": _sha(converted), "mimeType": "image/jpeg"}
    ]
    assert preparer.conversions == 1 and preparer.transcodes == 0


async def test_token_goes_to_same_origin_uploads_only(bridge_settings, workspace):
    same = ReferenceRemote()
    cross = ReferenceRemote(options=RemoteOptions(cross_origin_uploads=True))
    for remote in (same, cross):
        async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
            assert not (await host.call_tool("set_cover", {"image": "media/slide.png"})).is_error
    assert same.put_headers[0]["authorization"] == "Bearer reference-token"
    assert "authorization" not in cross.put_headers[0]
    assert cross.put_headers[0]["x-upload-token"]  # the presigned headers are still sent


async def test_paths_outside_the_workspace_are_refused(remote, bridge_settings, workspace, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    shutil.copy(FIXTURES / "slide.png", outside / "slide.png")
    (workspace / "link.png").symlink_to(outside / "slide.png")
    async with bridge_host(bridge_settings(remote.url)) as (_, host):
        for path in (str(outside / "slide.png"), "../elsewhere/slide.png", "link.png"):
            result = await host.call_tool("set_cover", {"image": path})
            assert _error(result) == "bridge.file_outside_workspace", path
    assert remote.uploads == {} and remote.calls == []

    async with bridge_host(bridge_settings(remote.url, allowed_roots=(outside,))) as (_, host):
        assert not (await host.call_tool("set_cover", {"image": str(outside / "slide.png")})).is_error


async def test_type_policy_is_local_floor_and_server_accept(remote, bridge_settings, workspace):
    async with bridge_host(bridge_settings(remote.url)) as (_, host):
        assert (
            _error(await host.call_tool("import_media", {"paths": ["notes.txt"]}))
            == "bridge.file_type_rejected"
        )
        # the server narrows further: set_cover accepts images only
        assert (
            _error(await host.call_tool("set_cover", {"image": "media/tone.wav"}))
            == "bridge.file_type_rejected"
        )
    async with bridge_host(bridge_settings(remote.url, upload_any_type=True)) as (_, host):
        # widening the local floor never widens what the tool accepts
        assert (
            _error(await host.call_tool("import_media", {"paths": ["notes.txt"]}))
            == "bridge.file_type_rejected"
        )
    assert remote.uploads == {}


async def test_missing_large_and_malformed_arguments(remote, bridge_settings, workspace):
    async with bridge_host(bridge_settings(remote.url, max_upload_bytes=1024)) as (_, host):
        assert (
            _error(await host.call_tool("import_media", {"paths": ["media/none.mp4"]}))
            == "bridge.file_not_found"
        )
        assert _error(await host.call_tool("import_media", {"paths": ["media"]})) == "bridge.file_not_found"
        assert (
            _error(await host.call_tool("import_media", {"paths": ["media/clip.mp4"]}))
            == "bridge.file_too_large"
        )
        assert _error(await host.call_tool("import_media", {"paths": [7]})) == "bridge.invalid_argument"
    assert remote.uploads == {} and remote.calls == []


async def test_file_tools_without_upload_tools_are_a_contract_violation(bridge_settings, workspace):
    remote = ReferenceRemote(options=RemoteOptions(upload_tools=False))
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
        result = await host.call_tool("set_cover", {"image": "media/slide.png"})
        assert _error(result) == "bridge.contract_violation"
        assert "upload" in result.structured_content["error"]["message"]
        # tools without local files still work
        assert not (await host.call_tool("slow_turn", {"steps": 1})).is_error


async def test_a_malformed_prepare_declaration_is_a_contract_violation(bridge_settings, workspace):
    remote = ReferenceRemote(options=RemoteOptions(bad_prepare=True))
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
        result = await host.call_tool("import_media", {"paths": ["media/slide.png"]})
    assert _error(result) == "bridge.contract_violation"
    assert "crf" in result.structured_content["error"]["message"]
    assert remote.begun == []


async def test_a_begin_without_put_targets_is_a_contract_violation(bridge_settings, workspace):
    remote = ReferenceRemote(options=RemoteOptions(begin_without_puts=True))
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
        first = await host.call_tool("set_cover", {"image": "media/slide.png"})
        second = await host.call_tool("set_cover", {"image": "media/slide.png"})
    assert (
        _error(first) == "bridge.contract_violation" and "put" in first.structured_content["error"]["message"]
    )
    # a failed local phase is reported once and forgotten: the next call starts over
    assert _error(second) == "bridge.contract_violation" and len(remote.begun) == 2
    assert remote.put_headers == [] and remote.calls == []
