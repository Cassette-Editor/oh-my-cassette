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


def test_session_thread_round_trip(cassette_env):
    sess_hash = manifest.resolve_session_hash(session_id="try-session-abc")
    assert manifest.load_session_thread(sess_hash) == {}

    manifest.save_session_thread(sess_hash, "11111111-2222-3333-4444-555555555555", "http://web/try?x=1")
    saved = manifest.load_session_thread(sess_hash)
    assert saved["thread_id"] == "11111111-2222-3333-4444-555555555555"
    assert saved["editor_url"] == "http://web/try?x=1"
    assert saved["updated_at"]

    manifest.save_session_thread(sess_hash, "new-thread", None)
    assert manifest.load_session_thread(sess_hash)["thread_id"] == "new-thread"


def test_session_thread_corrupt_file_returns_empty(cassette_env):
    sess_hash = manifest.resolve_session_hash(session_id="try-session-corrupt")
    path = manifest.session_thread_path(sess_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json{", encoding="utf-8")
    assert manifest.load_session_thread(sess_hash) == {}
    path.write_text('["a-list"]', encoding="utf-8")
    assert manifest.load_session_thread(sess_hash) == {}


def test_sweep_removes_stale_derived_artifacts_only(cassette_env, monkeypatch):
    import os
    import time

    from cassette import manifest

    root = manifest.get_asset_root()
    old = time.time() - 90 * 86400
    stale_sheet = root / "previews" / "try-session-old" / "sheet-v3.jpg"
    fresh_sheet = root / "previews" / "try-session-new" / "sheet-v1.jpg"
    stale_upload = root / "api_uploads" / "old.mp4"
    export = root / "exports" / "job-1" / "final.mp4"
    for p in (stale_sheet, fresh_sheet, stale_upload, export):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    os.utime(stale_sheet, (old, old))
    os.utime(stale_upload, (old, old))
    os.utime(export, (old, old))

    removed = manifest.sweep_stale_artifacts()

    assert removed == {"previews": 1, "api_uploads": 1}
    assert not stale_sheet.exists()
    assert not stale_sheet.parent.exists()  # emptied session dir pruned
    assert fresh_sheet.exists()
    assert export.exists()  # exports are never swept


def test_sweep_disabled_by_zero_ttl(cassette_env, monkeypatch):
    import os
    import time

    from cassette import manifest

    monkeypatch.setenv("CASSETTE_ARTIFACT_TTL_DAYS", "0")
    root = manifest.get_asset_root()
    stale = root / "previews" / "s" / "sheet.jpg"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"x")
    old = time.time() - 90 * 86400
    os.utime(stale, (old, old))

    assert manifest.sweep_stale_artifacts() == {}
    assert stale.exists()
