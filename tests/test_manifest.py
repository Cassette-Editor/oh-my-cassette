from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cassette import manifest


def test_ingest_asset_deduplicates_and_hashes_ids(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")

    first = manifest.ingest_asset(
        str(media), chat_id="wxid_chat", user_id="wxid_user", message_id="msg1", platform="weixin"
    )
    second = manifest.ingest_asset(
        str(media), chat_id="wxid_chat", user_id="wxid_user", message_id="msg2", platform="weixin"
    )

    assert first["sha256"] == second["sha256"]
    assert second["deduplicated"] is True
    listed = manifest.list_assets(chat_id="wxid_chat")
    session_manifest = listed["manifest"]
    assert len(session_manifest["assets"]) == 1
    assert session_manifest["session_id"] != "wxid_chat"
    assert session_manifest["chat_hash"] != "wxid_chat"
    assert session_manifest["user_hash"] != "wxid_user"
    assert session_manifest["delivery"]["platform"] == "weixin"
    assert session_manifest["delivery"]["chat_id"] == "wxid_chat"
    assert session_manifest["delivery"]["user_id"] == "wxid_user"
    assert Path(first["saved_path"]).exists()
    assert Path(listed["manifest_path"]).exists()


def test_list_assets_updates_exists(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    ingested = manifest.ingest_asset(str(media), session_id="s1")
    Path(ingested["saved_path"]).unlink()

    listed = manifest.list_assets(session_id="s1")
    assert listed["manifest"]["assets"][0]["exists"] is False


def test_list_assets_accepts_existing_session_hash(cassette_env):
    media = cassette_env["source_root"] / "clip.mp4"
    media.write_bytes(b"video")
    ingested = manifest.ingest_asset(str(media), session_id="gateway_media_weixin_test")

    listed = manifest.list_assets(session_id=ingested["session_hash"])

    assert listed["manifest"]["session_hash"] == ingested["session_hash"]
    assert len(listed["manifest"]["assets"]) == 1


def test_weixin_video_is_saved_as_internal_h264(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_WEIXIN_FORCE_H264", "1")
    media = cassette_env["source_root"] / "wechat.mp4"
    media.write_bytes(b"hevc-video")
    observed = {}

    def fake_run(cmd, stdout=None, stderr=None, text=None):
        observed["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"h264-video")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(manifest.subprocess, "run", fake_run)

    ingested = manifest.ingest_asset(
        str(media), original_name="wechat.mp4", media_type="video", platform="weixin", session_id="s1"
    )
    listed = manifest.list_assets(session_id="s1")
    asset = listed["manifest"]["assets"][0]

    assert ingested["sha256"] != manifest.security.sha256_file(Path(ingested["saved_path"]))
    assert Path(ingested["saved_path"]).name.endswith(".h264.mp4")
    assert Path(ingested["saved_path"]).read_bytes() == b"h264-video"
    assert asset["original_name"] == "wechat.mp4"
    assert asset["saved_path"] == ingested["saved_path"]
    assert asset["extension"] == ".mp4"
    assert "-c:v" in observed["cmd"]
    assert "libx264" in observed["cmd"]


def test_qq_video_is_saved_as_internal_h264(cassette_env, monkeypatch):
    monkeypatch.setenv("CASSETTE_FORCE_H264", "1")
    media = cassette_env["source_root"] / "qq.mp4"
    media.write_bytes(b"hevc-video")

    def fake_run(cmd, stdout=None, stderr=None, text=None):
        Path(cmd[-1]).write_bytes(b"h264-video")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(manifest.subprocess, "run", fake_run)

    ingested = manifest.ingest_asset(
        str(media), original_name="qq.mp4", media_type="video", platform="qqbot", session_id="s1"
    )

    assert Path(ingested["saved_path"]).name.endswith(".h264.mp4")
    assert Path(ingested["saved_path"]).read_bytes() == b"h264-video"
