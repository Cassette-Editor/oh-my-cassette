"""Local file arguments: the local policy, preparation and the upload handshake."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import httpx
import pytest
from tests.bridge_support import FakePreparer, bridge_host
from tests.reference_remote import REFERENCE_TOKEN, ReferenceRemote, RemoteOptions, serve_remote

from oh_my_cassette.bridge import files
from oh_my_cassette.bridge.errors import BridgeError
from oh_my_cassette.bridge.files import guess_mime, transfer_guard
from oh_my_cassette.bridge.settings import BridgeSettings

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
    for name in ("photo.heic", "photo.ppm", "photo.jfif", "photo.apng"):  # bytes only matter to the fake
        shutil.copy(FIXTURES / "slide.png", root / "media" / name)
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
    # each file goes up as soon as it is ready, so the two PUTs may arrive in either order
    assert sorted(h["content-type"] for h in remote.put_headers) == sorted(
        ["image/png", guess_mime(FIXTURES / "tone.wav")]
    )


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


async def test_each_file_uploads_while_the_next_one_is_prepared(remote, bridge_settings, workspace):
    (workspace / "media" / "second.mp4").write_bytes((FIXTURES / "clip.mp4").read_bytes() + b"another take")
    preparer = FakePreparer(delay=0.5, events=remote.events)
    async with bridge_host(bridge_settings(remote.url), preparer=preparer) as (_, host):
        result = await host.call_tool("import_media", {"paths": ["media/clip.mp4", "media/second.mp4"]})
    assert not result.is_error, result
    assert len(set(result.structured_content["refs"])) == 2
    assert remote.events == [
        "prepared:clip.mp4",
        "begin:clip.mp4",
        "complete:clip.mp4",
        "prepared:second.mp4",
        "begin:second.mp4",
        "complete:second.mp4",
    ]


async def test_a_second_copy_of_the_same_content_waits_for_the_first_and_is_not_sent(
    bridge_settings, workspace
):
    shutil.copy(FIXTURES / "clip.mp4", workspace / "media" / "copy.mp4")
    remote = ReferenceRemote(
        options=RemoteOptions(put_delay=0.5)
    )  # the first copy is still uploading when the second is ready
    preparer = FakePreparer(events=remote.events)
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url), preparer=preparer) as (_, host):
        result = await host.call_tool("import_media", {"paths": ["media/clip.mp4", "media/copy.mp4"]})
    assert not result.is_error, result
    first, second = result.structured_content["refs"]
    assert first == second
    assert remote.events.index("complete:clip.mp4") < remote.events.index("begin:copy.mp4")
    assert len(remote.put_headers) == 2  # the two renditions, once


async def test_parts_go_up_four_at_a_time(bridge_settings, workspace):
    data = (FIXTURES / "tone.wav").read_bytes()
    remote = ReferenceRemote(options=RemoteOptions(part_size=1024, part_batch=32, put_delay=0.05))
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
        result = await host.call_tool("import_media", {"paths": ["media/tone.wav"]})
    assert not result.is_error, result
    (ref,) = result.structured_content["refs"]
    assert remote.blobs[ref] == {"original": data}
    assert remote.most_puts_in_flight == files.UPLOAD_CONCURRENCY == 4


async def test_unaccepted_media_is_converted_before_upload(remote, bridge_settings, workspace):
    preparer = FakePreparer()
    async with bridge_host(bridge_settings(remote.url), preparer=preparer) as (_, host):
        result = await host.call_tool("import_media", {"paths": ["media/photo.ppm"]})
    assert not result.is_error, result
    (begun,) = remote.begun
    converted = b"converted:" + (FIXTURES / "slide.png").read_bytes()
    assert begun["name"] == "photo.jpg" and begun["relativePath"] == "media/photo.ppm"
    assert (begun["mimeType"], begun["size"], begun["sha256"]) == (
        "image/jpeg",
        len(converted),
        _sha(converted),
    )
    assert begun["artifacts"] == [
        {"role": "original", "size": len(converted), "sha256": _sha(converted), "mimeType": "image/jpeg"}
    ]
    assert preparer.conversions == 1 and preparer.transcodes == 0


async def test_images_the_service_converts_itself_and_its_other_types_upload_unchanged(
    remote, bridge_settings, workspace
):
    preparer = FakePreparer()
    paths = ["media/photo.heic", "media/photo.jfif", "media/photo.apng"]
    async with bridge_host(bridge_settings(remote.url), preparer=preparer) as (_, host):
        result = await host.call_tool("import_media", {"paths": paths})
    assert not result.is_error, result
    data = (FIXTURES / "slide.png").read_bytes()
    assert [(b["name"], b["mimeType"], b["sha256"]) for b in remote.begun] == [
        ("photo.heic", "image/heic", _sha(data)),
        ("photo.jfif", "image/jpeg", _sha(data)),
        ("photo.apng", "image/apng", _sha(data)),
    ]
    assert preparer.conversions == 0


async def test_large_artifacts_go_up_in_parts_retried_one_by_one(bridge_settings, workspace):
    data = (FIXTURES / "tone.wav").read_bytes()
    options = RemoteOptions(part_size=1024, part_batch=3, fail_part_once=2, expire_part_once=5)
    remote = ReferenceRemote(options=options)
    seen: list[float] = []

    async def on_progress(progress, total, message):
        seen.append(progress)

    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
        result = await host.call_tool(
            "import_media", {"paths": ["media/tone.wav"]}, progress_callback=on_progress
        )
    assert not result.is_error, result
    (ref,) = result.structured_content["refs"]
    assert remote.blobs[ref] == {"original": data}  # the parts, joined in order, are the file
    count = -(-len(data) // 1024)
    # batches of three, one part URL signed again after a 403, and a last request that finds nothing left
    assert [entry["partNumbers"] for entry in remote.signed] == [None, None, [5], *[None] * (count // 3)]
    assert len(remote.part_headers) == count + 2  # one 503 and one 403 sent again
    assert all("content-type" not in headers for headers in remote.part_headers)
    assert all(headers["authorization"] == "Bearer reference-token" for headers in remote.part_headers)
    assert remote.put_headers == []  # nothing went to the single-PUT URL


async def test_part_urls_signed_too_long_ago_are_signed_again_before_use(
    bridge_settings, workspace, monkeypatch
):
    monkeypatch.setattr(files, "PART_URL_MAX_AGE_SEC", -1.0)  # every URL is stale by the time it is used
    remote = ReferenceRemote(options=RemoteOptions(part_size=4096, part_batch=2))
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
        result = await host.call_tool("set_cover", {"image": "media/slide.png"})
        assert not result.is_error, result
        result = await host.call_tool("import_media", {"paths": ["media/tone.wav"]})
    assert not result.is_error, result
    # tone.wav is two parts: one batch, each part's URL signed again just before it is sent
    signed = [entry["partNumbers"] for entry in remote.signed]
    assert signed[0] is None and sorted(signed[1:3]) == [[1], [2]] and signed[3:] == [None]


async def test_parts_without_a_tool_to_sign_them_are_a_contract_violation(bridge_settings, workspace):
    remote = ReferenceRemote(options=RemoteOptions(part_size=64, parts_tool=False))
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
        result = await host.call_tool("set_cover", {"image": "media/slide.png"})
    assert (
        _error(result) == "bridge.contract_violation"
        and "parts" in result.structured_content["error"]["message"]
    )
    assert remote.put_headers == [] and remote.part_headers == []


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


async def test_a_typed_refusal_of_the_upload_reaches_the_host_as_the_service_sent_it(
    bridge_settings, workspace
):
    refusal = {
        "code": "project_media_on_device",
        "message": "Import them in Cassette for Mac.",
        "retryable": False,
    }
    remote = ReferenceRemote(options=RemoteOptions(begin_refusal=refusal))
    async with serve_remote(remote), bridge_host(bridge_settings(remote.url)) as (_, host):
        result = await host.call_tool("set_cover", {"image": "media/slide.png"})
    assert result.is_error and result.structured_content == {"error": refusal}
    assert remote.put_headers == [] and remote.calls == []


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


async def test_a_file_that_does_not_begin_like_its_type_does_not_leave(remote, bridge_settings, workspace):
    (workspace / "id_ed25519.png").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    async with bridge_host(bridge_settings(remote.url)) as (_, host):
        result = await host.call_tool("set_cover", {"image": "id_ed25519.png"})
    assert _error(result) == "bridge.file_type_rejected"
    assert remote.begun == [] and remote.put_headers == []


async def test_a_host_started_in_the_home_folder_reads_only_the_folders_named_for_it(
    remote, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    (home / "Movies").mkdir(parents=True)
    shutil.copy(FIXTURES / "slide.png", home / "Movies" / "slide.png")
    for name in ("CASSETTE_WORKSPACE", "CASSETTE_ALLOWED_ROOTS", "CASSETTE_DOWNLOAD_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CASSETTE_MCP_URL", remote.url)
    monkeypatch.setenv("CASSETTE_AUTH_TOKEN", REFERENCE_TOKEN)
    monkeypatch.setenv("CASSETTE_TEMP_DIR", str(tmp_path / "bridge-temp"))

    async def cover(settings: BridgeSettings):
        async with bridge_host(settings) as (_, host):
            return await host.call_tool("set_cover", {"image": "Movies/slide.png"})

    refused = await cover(BridgeSettings.from_env(cwd=home))
    assert _error(refused) == "bridge.file_outside_workspace"
    assert "CASSETTE_WORKSPACE" in refused.structured_content["error"]["message"]
    monkeypatch.setenv("CASSETTE_ALLOWED_ROOTS", str(home / "Movies"))
    assert not (await cover(BridgeSettings.from_env(cwd=home))).is_error
    monkeypatch.delenv("CASSETTE_ALLOWED_ROOTS")
    monkeypatch.setenv("CASSETTE_WORKSPACE", str(home))  # chosen on purpose, it is the workspace
    assert not (await cover(BridgeSettings.from_env(cwd=home))).is_error


@pytest.mark.parametrize(
    "url, refusal",
    [
        ("https://mcp.example.com/downloads/export/1", None),  # the endpoint's own origin
        ("https://93.184.216.34/object", None),
        ("http://93.184.216.34/object", "HTTPS"),
        ("https://127.0.0.1/object", "public"),
        ("https://[::1]/object", "public"),
        ("https://10.1.2.3/object", "public"),
        ("https://169.254.169.254/latest/meta-data", "public"),
        ("https://localhost/object", "public"),
    ],
)
async def test_transfers_go_to_the_endpoint_or_public_https_hosts_only(url, refusal):
    check = transfer_guard("https://mcp.example.com/mcp")
    if refusal is None:
        await check(httpx.Request("GET", url))
    else:
        with pytest.raises(BridgeError, match=refusal) as caught:
            await check(httpx.Request("GET", url))
        assert caught.value.code == "bridge.contract_violation" and "object" not in caught.value.message


async def test_a_local_backend_may_hand_out_loopback_storage_only():
    check = transfer_guard("http://127.0.0.1:8790/mcp")
    await check(httpx.Request("PUT", "http://127.0.0.1:9000/bucket/key"))
    with pytest.raises(BridgeError):
        await check(httpx.Request("PUT", "http://10.0.0.7/bucket/key"))


@pytest.mark.parametrize(
    "header", ["Host", "Cookie", "Transfer-Encoding", "Proxy-Authorization", "X-Forwarded-For"]
)
def test_put_targets_may_not_set_routing_or_cookie_headers(header):
    artifact = files.UploadArtifact(files.ARTIFACT_ORIGINAL, Path("slide.png"), 10, "0" * 64, "image/png")
    file = files.UploadFile(0, "slide.png", "slide.png", 10, "0" * 64, "image/png", (artifact,), None)
    put = {
        "role": files.ARTIFACT_ORIGINAL,
        "url": "https://store.example/k",
        "headers": {"X-Upload-Token": "t"},
    }
    assert files.LocalFiles._targets("upload.begin", file, [put])[files.ARTIFACT_ORIGINAL].headers == {
        "X-Upload-Token": "t"
    }
    with pytest.raises(BridgeError, match=header) as caught:
        files.LocalFiles._targets("upload.begin", file, [{**put, "headers": {header: "x"}}])
    assert caught.value.code == "bridge.contract_violation"
