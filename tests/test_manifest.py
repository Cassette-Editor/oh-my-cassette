from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cassette.core import manifest


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


def test_uploaded_managed_copy_is_removed_through_a_resolved_asset_root_alias(cassette_env, monkeypatch, tmp_path):
    actual_root = tmp_path / "actual-asset-root"
    actual_root.mkdir()
    alias_root = tmp_path / "asset-root-alias"
    try:
        alias_root.symlink_to(actual_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")
    monkeypatch.setenv("CASSETTE_ASSET_ROOT", str(alias_root))
    source = cassette_env["source_root"] / "clip.mp4"
    source.write_bytes(b"video")

    ingested = manifest.ingest_asset(str(source), session_id="alias-session")
    saved_path = Path(ingested["saved_path"])
    assert saved_path.exists()

    manifest.mark_managed_asset_uploaded(
        "alias-session",
        str(saved_path),
        "00000000-0000-4000-8000-000000000001",
        fingerprint="clip.mp4:5",
    )

    assert source.exists()
    assert not saved_path.exists()
    stored = manifest.load_manifest(ingested["session_hash"])
    assert stored["assets"][0]["media_file_id"] == "00000000-0000-4000-8000-000000000001"
    assert stored["assets"][0]["exists"] is False


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

    manifest.save_session_thread(sess_hash, "11111111-2222-3333-4444-555555555555")
    saved = manifest.load_session_thread(sess_hash)
    assert saved["thread_id"] == "11111111-2222-3333-4444-555555555555"
    assert "editor_url" not in saved  # no deep link is persisted any more
    assert saved["updated_at"]

    manifest.save_session_thread(sess_hash, "new-thread")
    assert manifest.load_session_thread(sess_hash)["thread_id"] == "new-thread"


def test_session_thread_corrupt_file_returns_empty(cassette_env):
    sess_hash = manifest.resolve_session_hash(session_id="try-session-corrupt")
    path = manifest.session_thread_path(sess_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json{", encoding="utf-8")
    assert manifest.load_session_thread(sess_hash) == {}
    path.write_text('["a-list"]', encoding="utf-8")
    assert manifest.load_session_thread(sess_hash) == {}


def test_sweep_removes_all_stale_managed_artifacts(cassette_env, monkeypatch):
    import os
    import time

    from cassette.core import manifest

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

    assert removed == {"previews": 1, "api_uploads": 1, "exports": 1}
    assert not stale_sheet.exists()
    assert not stale_sheet.parent.exists()  # emptied session dir pruned
    assert fresh_sheet.exists()
    assert not export.exists()


def test_retention_cannot_be_disabled_by_legacy_ttl_setting(cassette_env, monkeypatch):
    import os
    import time

    from cassette.core import manifest

    monkeypatch.setenv("CASSETTE_ARTIFACT_TTL_DAYS", "0")
    root = manifest.get_asset_root()
    stale = root / "previews" / "s" / "sheet.jpg"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"x")
    old = time.time() - 90 * 86400
    os.utime(stale, (old, old))

    assert manifest.sweep_stale_artifacts() == {"previews": 1}
    assert not stale.exists()


def test_first_ingest_deadline_is_immutable_and_user_source_is_untouched(cassette_env):
    from datetime import datetime

    media = cassette_env["source_root"] / "source.mp4"
    media.write_bytes(b"video")

    first = manifest.ingest_asset(str(media), session_id="deadline")
    second = manifest.ingest_asset(str(media), session_id="deadline")
    stored = manifest.list_assets(session_id="deadline")["manifest"]

    created = datetime.fromisoformat(stored["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(stored["expires_at"].replace("Z", "+00:00"))
    assert (expires - created).total_seconds() == manifest.MEDIA_RETENTION_SECONDS
    assert first["expires_at"] == second["expires_at"] == stored["expires_at"]
    assert media.exists()


def test_sweep_uses_session_deadline_for_jobs_exports_previews_cache_and_state(cassette_env, monkeypatch):
    import hashlib
    import json
    from datetime import datetime

    data_root = cassette_env["asset_root"].parent / "data"
    monkeypatch.setenv("CASSETTE_DATA_HOME", str(data_root))
    source = cassette_env["source_root"] / "keep.mp4"
    source.write_bytes(b"source")
    ingested = manifest.ingest_asset(str(source), session_id="session-deadline")
    session_hash = ingested["session_hash"]
    stored = manifest.load_manifest(session_hash)
    expires = datetime.fromisoformat(stored["expires_at"].replace("Z", "+00:00")).timestamp()
    job_id = "cassette_retention_test"
    job = {
        "job_id": job_id,
        "session_hash": session_hash,
        "cassette_session_id": "session-deadline",
        "expires_at": stored["expires_at"],
    }
    paths = [
        cassette_env["asset_root"] / "jobs" / f"{job_id}.json",
        cassette_env["asset_root"] / "exports" / job_id / "cut.mp4",
        cassette_env["asset_root"] / "previews" / "session-deadline" / "sheet.jpg",
        cassette_env["asset_root"] / "screenshots" / job_id / "frame.jpg",
        cassette_env["asset_root"] / "api_uploads" / "session-deadline.json",
        data_root / "mcp-state" / "sessions" / f"{hashlib.sha256(b'session-deadline').hexdigest()}.json",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job), encoding="utf-8")

    removed = manifest.sweep_stale_artifacts(now=expires)

    assert removed["sessions"] >= 1
    assert removed["jobs"] == 1
    assert removed["exports"] == 1
    assert removed["previews"] == 1
    assert removed["screenshots"] == 1
    assert removed["api_uploads"] == 1
    assert removed["state"] == 1
    assert all(not path.exists() for path in paths)
    assert source.exists(), "retention must never delete the user's source outside managed roots"
    assert manifest.sweep_stale_artifacts(now=expires) == {}, "a repeated production sweep must be idempotent"

    with pytest.raises(manifest.CassetteError) as exc:
        manifest.ingest_asset(str(source), session_id="session-deadline")
    assert exc.value.code == "session_expired", "the session tombstone must prevent late-ingest resurrection"
    assert source.exists(), "a rejected late ingest must not touch the user's source"


def test_sweep_removes_old_corrupt_and_missing_manifest_sessions(cassette_env):
    import os
    import time

    root = cassette_env["asset_root"] / "sessions"
    corrupt = root / "corrupt-session"
    missing = root / "missing-session"
    corrupt.mkdir(parents=True)
    missing.mkdir(parents=True)
    (corrupt / "manifest.json").write_text("not-json", encoding="utf-8")
    (corrupt / "media.mp4").write_bytes(b"managed")
    (missing / "media.mp4").write_bytes(b"managed")
    old = time.time() - manifest.MEDIA_RETENTION_SECONDS - 10
    os.utime(corrupt, (old, old))
    os.utime(missing, (old, old))

    removed = manifest.sweep_stale_artifacts(now=time.time())

    assert removed["sessions"] == 3
    assert not corrupt.exists()
    assert not missing.exists()
    assert len(list((cassette_env["asset_root"] / "session_tombstones").glob("*.json"))) == 2


def test_file_backed_acceptance_clock_is_double_gated(cassette_env, monkeypatch, tmp_path):
    clock = tmp_path / "clock.txt"
    clock.write_text("1234.5", encoding="utf-8")
    monkeypatch.setenv("CASSETTE_RETENTION_TEST_CLOCK_FILE", str(clock))
    monkeypatch.delenv("CASSETTE_RETENTION_TEST_MODE", raising=False)
    assert manifest.retention_now() != 1234.5

    monkeypatch.setenv("CASSETTE_RETENTION_TEST_MODE", "1")
    assert manifest.retention_now() == 1234.5
