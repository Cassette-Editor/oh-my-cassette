from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import CassetteError
from . import security


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()


def get_asset_root() -> Path:
    try:
        import runtime_config

        if runtime_config.is_mcp_runtime():
            return runtime_config.asset_root()
    except Exception:  # noqa: BLE001 — retain the Hermes default below
        pass
    return Path(os.getenv("CASSETTE_ASSET_ROOT", str(_hermes_home() / "cassette"))).expanduser().resolve()


def session_key(session_id: str | None = None, chat_id: str | None = None, task_id: str | None = None) -> str:
    return str(session_id or chat_id or task_id or "default")


def session_hash(key: str) -> str:
    return security.safe_hash_id(key)


def resolve_session_hash(session_id: str | None = None, chat_id: str | None = None, task_id: str | None = None) -> str:
    key = session_key(session_id, chat_id, task_id)
    hashed = session_hash(key)
    if get_manifest_path(hashed).exists():
        return hashed
    if session_id and get_manifest_path(str(session_id)).exists():
        return str(session_id)
    return hashed


def get_session_dir(session_hash: str) -> Path:
    return get_asset_root() / "sessions" / session_hash


def get_manifest_path(session_hash: str) -> Path:
    return get_session_dir(session_hash) / "manifest.json"


@contextmanager
def manifest_lock(session_hash: str):
    lock_path = get_session_dir(session_hash) / ".manifest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as fh:
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _empty_manifest(key: str, sess_hash: str) -> dict:
    ts = now_iso()
    return {
        "version": 1,
        "session_id": sess_hash,
        "session_hash": sess_hash,
        "chat_hash": security.safe_hash_id(key),
        "user_hash": "",
        "created_at": ts,
        "updated_at": ts,
        "delivery": {},
        "assets": [],
    }


def load_manifest(session_hash: str) -> dict:
    path = get_manifest_path(session_hash)
    if not path.exists():
        return _empty_manifest("default", session_hash)
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        raise CassetteError("manifest_read_failed", "Failed to read session manifest", {"path": str(path)}) from exc


def save_manifest_atomic(session_hash: str, manifest: dict) -> None:
    path = get_manifest_path(session_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = now_iso()
    fd, tmp_name = tempfile.mkstemp(prefix=".manifest.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise CassetteError("manifest_write_failed", "Failed to write session manifest") from exc


def _media_type_from_ext(ext: str) -> str:
    if ext in {".mp4", ".mov", ".m4v", ".webm"}:
        return "video"
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "image"
    if ext in {".mp3", ".wav", ".m4a", ".aac"}:
        return "audio"
    return "file"


def _is_disabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _force_h264_platforms() -> set[str]:
    raw = os.getenv("CASSETTE_FORCE_H264_PLATFORMS", "weixin,qqbot,qq,telegram")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _should_force_h264(platform: str | None, media_type: str, ext: str) -> bool:
    enabled = os.getenv("CASSETTE_FORCE_H264", os.getenv("CASSETTE_WEIXIN_FORCE_H264", "1"))
    if _is_disabled(enabled):
        return False
    platform_name = str(platform or "").lower()
    platforms = _force_h264_platforms()
    if "*" not in platforms and platform_name not in platforms:
        return False
    if media_type != "video":
        return False
    return ext in {".mp4", ".mov", ".m4v", ".webm"}


def _transcode_h264(source: Path, dest: Path) -> None:
    ffmpeg_bin = os.getenv("CASSETTE_FFMPEG_BIN", "ffmpeg")
    fd, tmp_name = tempfile.mkstemp(prefix=".h264.", suffix=".mp4", dir=str(dest.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        os.getenv("CASSETTE_H264_PRESET", "veryfast"),
        "-crf",
        os.getenv("CASSETTE_H264_CRF", "20"),
        "-c:a",
        "aac",
        "-b:a",
        os.getenv("CASSETTE_H264_AUDIO_BITRATE", "160k"),
        "-movflags",
        "+faststart",
        str(tmp_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise CassetteError("transcoder_missing", "ffmpeg is required to normalize gateway video for Cassette") from exc
    if proc.returncode != 0:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        detail = (proc.stderr or "").strip()[-500:]
        raise CassetteError("transcode_failed", "Failed to normalize gateway video for Cassette", {"stderr_tail": detail})
    if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise CassetteError("transcode_failed", "Failed to normalize gateway video for Cassette", {"reason": "empty_output"})
    os.replace(tmp_path, dest)


def ingest_asset(
    source_path: str,
    original_name: str | None = None,
    media_type: str | None = None,
    chat_id: str | None = None,
    user_id: str | None = None,
    message_id: str | None = None,
    chat_type: str | None = None,
    thread_id: str | None = None,
    caption: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    platform: str | None = None,
) -> dict:
    source = security.resolve_and_validate_source_path(source_path)
    ext = security.validate_extension(source)
    size = security.validate_size(source)
    digest = security.sha256_file(source)
    key = session_key(session_id, chat_id, task_id)
    sess_hash = session_hash(key)

    resolved_media_type = media_type if media_type in {"video", "image", "audio", "file", "unknown"} else _media_type_from_ext(ext)
    media_dir = get_session_dir(sess_hash) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    force_h264 = _should_force_h264(platform, resolved_media_type, ext)
    dest = media_dir / (f"{digest}.h264.mp4" if force_h264 else f"{digest}{ext}")
    deduplicated = dest.exists()
    if not deduplicated:
        if force_h264:
            _transcode_h264(source, dest)
        else:
            shutil.copy2(source, dest)

    asset_id = f"asset_{digest[:12]}"
    with manifest_lock(sess_hash):
        manifest = load_manifest(sess_hash)
        if not manifest.get("assets") and manifest.get("session_id") == "default":
            manifest = _empty_manifest(key, sess_hash)
        manifest["session_id"] = sess_hash
        manifest["session_hash"] = sess_hash
        manifest["chat_hash"] = security.safe_hash_id(chat_id or key)
        manifest["user_hash"] = security.safe_hash_id(user_id) if user_id else manifest.get("user_hash", "")
        if chat_id or user_id:
            delivery = dict(manifest.get("delivery") or {})
            delivery.update({
                "platform": platform or delivery.get("platform") or "",
                "chat_id": chat_id or delivery.get("chat_id") or "",
                "user_id": user_id or delivery.get("user_id") or "",
                "message_id": message_id or delivery.get("message_id") or "",
                "chat_type": chat_type or delivery.get("chat_type") or "",
                "thread_id": thread_id or delivery.get("thread_id") or "",
                "updated_at": now_iso(),
            })
            manifest["delivery"] = delivery

        existing = next((a for a in manifest["assets"] if a.get("sha256") == digest), None)
        asset = {
            "asset_id": asset_id,
            "sha256": digest,
            "saved_path": str(dest),
            "original_name": original_name or source.name,
            "extension": dest.suffix.lower(),
            "media_type": resolved_media_type,
            "size_bytes": size,
            "caption": caption or "",
            "message_id": security.safe_hash_id(message_id) if message_id else "",
            "created_at": existing.get("created_at") if existing else now_iso(),
            "exists": dest.exists(),
        }
        if existing:
            existing.update(asset)
        else:
            manifest["assets"].append(asset)
        save_manifest_atomic(sess_hash, manifest)
    return {
        "asset_id": asset_id,
        "saved_path": str(dest),
        "manifest_path": str(get_manifest_path(sess_hash)),
        "sha256": digest,
        "size_bytes": size,
        "session_hash": sess_hash,
        "deduplicated": deduplicated or existing is not None,
    }


def ingest_internal_asset(
    source_path: str,
    session_id: str,
    original_name: str | None = None,
    media_type: str | None = None,
    caption: str | None = None,
    metadata: dict | None = None,
) -> dict:
    source = Path(source_path).expanduser().resolve()
    root = get_asset_root()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise CassetteError("internal_asset_outside_root", "Internal Cassette asset must live under the Cassette asset root") from exc
    if not source.exists() or not source.is_file():
        raise CassetteError("internal_asset_missing", "Internal Cassette asset was not found")
    ext = source.suffix.lower()
    if not ext:
        raise CassetteError("internal_asset_missing_extension", "Internal Cassette asset must have a file extension")
    size = source.stat().st_size
    digest = security.sha256_file(source)
    sess_hash = resolve_session_hash(session_id=session_id)
    media_dir = get_session_dir(sess_hash) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / f"{digest}{ext}"
    deduplicated = dest.exists()
    if source != dest and not deduplicated:
        shutil.copy2(source, dest)

    asset_id = f"asset_{digest[:12]}"
    resolved_media_type = media_type if media_type in {"video", "image", "audio", "file", "unknown"} else _media_type_from_ext(ext)
    with manifest_lock(sess_hash):
        session_manifest = load_manifest(sess_hash)
        if not session_manifest.get("assets") and session_manifest.get("session_id") == "default":
            session_manifest = _empty_manifest(session_id, sess_hash)
        session_manifest["session_id"] = sess_hash
        session_manifest["session_hash"] = sess_hash
        existing = next((a for a in session_manifest["assets"] if a.get("sha256") == digest), None)
        asset = {
            "asset_id": asset_id,
            "sha256": digest,
            "saved_path": str(dest),
            "original_name": original_name or source.name,
            "extension": ext,
            "media_type": resolved_media_type,
            "size_bytes": size,
            "caption": caption or "",
            "message_id": "",
            "created_at": existing.get("created_at") if existing else now_iso(),
            "exists": dest.exists(),
        }
        if metadata:
            asset["metadata"] = metadata
        if existing:
            existing.update(asset)
        else:
            session_manifest["assets"].append(asset)
        save_manifest_atomic(sess_hash, session_manifest)
    return {
        "asset_id": asset_id,
        "saved_path": str(dest),
        "manifest_path": str(get_manifest_path(sess_hash)),
        "sha256": digest,
        "size_bytes": size,
        "session_hash": sess_hash,
        "deduplicated": deduplicated or existing is not None,
    }


def list_assets(session_id: str | None = None, chat_id: str | None = None, task_id: str | None = None) -> dict:
    key = session_key(session_id, chat_id, task_id)
    sess_hash = resolve_session_hash(session_id, chat_id, task_id)
    manifest = load_manifest(sess_hash)
    changed = False
    for asset in manifest.get("assets", []):
        exists = Path(asset.get("saved_path", "")).exists()
        if asset.get("exists") != exists:
            asset["exists"] = exists
            changed = True
    if changed:
        save_manifest_atomic(sess_hash, manifest)
    return {"manifest_path": str(get_manifest_path(sess_hash)), "manifest": manifest}
