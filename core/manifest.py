from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import CassetteError
from . import security


MEDIA_RETENTION_SECONDS = 24 * 60 * 60
_RETENTION_TEST_MODE = "CASSETTE_RETENTION_TEST_MODE"
_RETENTION_TEST_CLOCK_FILE = "CASSETTE_RETENTION_TEST_CLOCK_FILE"


def retention_now() -> float:
    """Return the wall clock, with an explicit file-backed clock for acceptance tests.

    The override is deliberately double-gated and read on every call so a long-lived MCP
    process can be advanced without sleeping for 24 hours. Production deployments that do
    not opt into test mode always use the system clock.
    """
    if os.getenv(_RETENTION_TEST_MODE) != "1":
        return time.time()
    raw_path = str(os.getenv(_RETENTION_TEST_CLOCK_FILE, "") or "").strip()
    if not raw_path:
        return time.time()
    try:
        return float(Path(raw_path).expanduser().read_text("utf-8").strip())
    except (OSError, ValueError):
        return time.time()


def now_iso(now: float | None = None) -> str:
    value = datetime.fromtimestamp(now if now is not None else retention_now(), tz=timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def retention_deadline(now: float | None = None) -> str:
    current = datetime.fromtimestamp(now if now is not None else retention_now(), tz=timezone.utc)
    return (
        (current + timedelta(seconds=MEDIA_RETENTION_SECONDS)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _retention_fields(created_at: object, expires_at: object = None, *, now: float | None = None) -> tuple[str, str]:
    created = _parse_iso(created_at)
    if created is None:
        created = datetime.fromtimestamp(now if now is not None else retention_now(), tz=timezone.utc)
    expires = _parse_iso(expires_at)
    if expires is None:
        expires = created + timedelta(seconds=MEDIA_RETENTION_SECONDS)
    return (
        created.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        expires.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )


def is_expired(expires_at: object, *, now: float | None = None) -> bool:
    deadline = _parse_iso(expires_at)
    if deadline is None:
        return False
    current = datetime.fromtimestamp(now if now is not None else retention_now(), tz=timezone.utc)
    return current >= deadline


def earliest_expiry(*values: object) -> str | None:
    parsed = [value for value in (_parse_iso(item) for item in values) if value is not None]
    if not parsed:
        return None
    return min(parsed).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seconds_until_expiry(expires_at: object, *, now: float | None = None) -> float | None:
    deadline = _parse_iso(expires_at)
    if deadline is None:
        return None
    current = datetime.fromtimestamp(now if now is not None else retention_now(), tz=timezone.utc)
    return max(0.0, (deadline - current).total_seconds())


def get_asset_root() -> Path:
    try:
        import runtime_config

        if runtime_config.is_mcp_runtime():
            return runtime_config.asset_root()
    except Exception:  # noqa: BLE001 — retain the Hermes default below
        pass
    return Path(os.getenv("CASSETTE_ASSET_ROOT", str(security._hermes_home() / "cassette"))).expanduser().resolve()


_MANAGED_CLASSES = ("previews", "api_uploads", "screenshots", "exports", "jobs")


def _remove_managed_path(path: Path) -> int:
    """Remove one app-owned path without following symlinks; return files removed."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return 1
        if not path.is_dir():
            return 0
        count = sum(1 for item in path.rglob("*") if item.is_file() or item.is_symlink())
        shutil.rmtree(path)
        return count
    except OSError:
        return 0


def _safe_session_name(value: object) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(value or ""))[:120]


def _state_path(session_id: str) -> Path:
    import runtime_config

    digest = __import__("hashlib").sha256(session_id.encode("utf-8")).hexdigest()
    return runtime_config.data_root() / "mcp-state" / "sessions" / f"{digest}.json"


def _tombstone_path(session_hash: str) -> Path:
    digest = security.safe_hash_id(session_hash)
    return get_asset_root() / "session_tombstones" / f"{digest}.json"


def _load_tombstone(session_hash: str) -> dict | None:
    path = _tombstone_path(session_hash)
    if path.is_symlink():
        return {"expires_at": None, "corrupt": True}
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        # A tombstone is fail-closed: corruption must not silently resurrect media.
        return {"expires_at": None, "corrupt": True}
    return value if isinstance(value, dict) else {"expires_at": None, "corrupt": True}


def _write_tombstone(session_hash: str, expires_at: object, *, now: float) -> bool:
    """Persist a non-media session tombstone before removing managed session data."""
    path = _tombstone_path(session_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        return False
    if path.exists():
        return True
    payload = {
        "version": 1,
        "session_digest": security.safe_hash_id(session_hash),
        "expires_at": str(expires_at or "") or None,
        "deleted_at": now_iso(now),
    }
    fd, temporary = tempfile.mkstemp(prefix=".tombstone.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


def _raise_if_tombstoned(session_hash: str) -> None:
    tombstone = _load_tombstone(session_hash)
    if tombstone is None:
        return
    raise CassetteError(
        "session_expired",
        "This Cassette media session reached its 24-hour retention deadline. Ingest the source into a new session.",
        {"expires_at": tombstone.get("expires_at")},
        recoverable=True,
    )


def sweep_stale_artifacts(now: float | None = None) -> dict[str, int]:
    """Delete every plugin-managed object at its immutable first-ingest +24h deadline.

    Session manifests and job records are the authoritative links between otherwise separate
    directories. A final mtime pass removes unlinked/orphaned managed files after 24 hours.
    User source paths are never traversed or deleted. Best-effort by design: cleanup must not
    prevent startup or a tool call from returning."""
    current = now if now is not None else retention_now()
    root = get_asset_root()
    removed: dict[str, int] = {}
    jobs_dir = root / "jobs"
    job_records: list[tuple[Path, dict]] = []
    if jobs_dir.is_dir():
        for path in jobs_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text("utf-8"))
                if isinstance(payload, dict):
                    job_records.append((path, payload))
            except (OSError, ValueError):
                continue

    sessions_root = root / "sessions"
    if sessions_root.is_dir():
        for session_dir in list(sessions_root.iterdir()):
            if not session_dir.is_dir() or session_dir.is_symlink():
                continue
            manifest_path = session_dir / "manifest.json"
            try:
                session_manifest = json.loads(manifest_path.read_text("utf-8"))
            except (OSError, ValueError):
                try:
                    stale = current - session_dir.stat().st_mtime >= MEDIA_RETENTION_SECONDS
                except OSError:
                    stale = False
                if stale and _write_tombstone(session_dir.name, None, now=current):
                    count = _remove_managed_path(session_dir)
                    if count:
                        removed["sessions"] = removed.get("sessions", 0) + count
                continue
            if not isinstance(session_manifest, dict):
                try:
                    stale = current - session_dir.stat().st_mtime >= MEDIA_RETENTION_SECONDS
                except OSError:
                    stale = False
                if stale and _write_tombstone(session_dir.name, None, now=current):
                    count = _remove_managed_path(session_dir)
                    if count:
                        removed["sessions"] = removed.get("sessions", 0) + count
                continue
            created_at, expires_at = _retention_fields(
                session_manifest.get("created_at"), session_manifest.get("expires_at"), now=current
            )
            if session_manifest.get("created_at") != created_at or session_manifest.get("expires_at") != expires_at:
                session_manifest["created_at"] = created_at
                session_manifest["expires_at"] = expires_at
                try:
                    save_manifest_atomic(session_dir.name, session_manifest)
                except Exception:  # noqa: BLE001 - cleanup remains best effort
                    pass
            if not is_expired(expires_at, now=current):
                continue
            if not _write_tombstone(session_dir.name, expires_at, now=current):
                continue

            aliases = {session_dir.name}
            for job_path, job in job_records:
                if str(job.get("session_hash") or "") != session_dir.name:
                    continue
                alias = str(job.get("cassette_session_id") or "").strip()
                if alias:
                    aliases.add(alias)
                job_id = str(job.get("job_id") or job_path.stem)
                count = _remove_managed_path(root / "exports" / job_id)
                if count:
                    removed["exports"] = removed.get("exports", 0) + count
                count = _remove_managed_path(root / "screenshots" / job_id)
                if count:
                    removed["screenshots"] = removed.get("screenshots", 0) + count
                if _remove_managed_path(job_path):
                    removed["jobs"] = removed.get("jobs", 0) + 1
            for alias in aliases:
                safe = _safe_session_name(alias)
                for class_name in ("previews", "screenshots"):
                    count = _remove_managed_path(root / class_name / safe)
                    if count:
                        removed[class_name] = removed.get(class_name, 0) + count
                count = _remove_managed_path(root / "api_uploads" / f"{safe[:96] or 'default'}.json")
                if count:
                    removed["api_uploads"] = removed.get("api_uploads", 0) + count
                count = _remove_managed_path(_state_path(alias))
                if count:
                    removed["state"] = removed.get("state", 0) + count
            count = _remove_managed_path(session_dir)
            if count:
                removed["sessions"] = removed.get("sessions", 0) + count

    # Jobs can outlive a missing/corrupt session manifest. Their own copied deadline is enough
    # to remove the job and export without guessing from prose or status.
    for job_path, job in job_records:
        if not job_path.exists() or not is_expired(job.get("expires_at"), now=current):
            continue
        job_id = str(job.get("job_id") or job_path.stem)
        count = _remove_managed_path(root / "exports" / job_id)
        if count:
            removed["exports"] = removed.get("exports", 0) + count
        count = _remove_managed_path(root / "screenshots" / job_id)
        if count:
            removed["screenshots"] = removed.get("screenshots", 0) + count
        if _remove_managed_path(job_path):
            removed["jobs"] = removed.get("jobs", 0) + 1

    try:
        import runtime_config

        state_root = runtime_config.data_root() / "mcp-state" / "sessions"
        if state_root.is_dir():
            for state_path in state_root.glob("*.json"):
                try:
                    state = json.loads(state_path.read_text("utf-8"))
                except (OSError, ValueError):
                    try:
                        if current - state_path.stat().st_mtime >= MEDIA_RETENTION_SECONDS:
                            if _remove_managed_path(state_path):
                                removed["state"] = removed.get("state", 0) + 1
                    except OSError:
                        pass
                    continue
                if isinstance(state, dict) and is_expired(state.get("expires_at"), now=current):
                    if _remove_managed_path(state_path):
                        removed["state"] = removed.get("state", 0) + 1
                elif current - state_path.stat().st_mtime >= MEDIA_RETENTION_SECONDS:
                    if _remove_managed_path(state_path):
                        removed["state"] = removed.get("state", 0) + 1
    except Exception:  # noqa: BLE001 - cleanup remains best effort
        pass

    # Orphan backstop. This includes caches created before retention metadata existed. It does
    # not extend on access and never walks outside the application-owned roots.
    for class_name in _MANAGED_CLASSES:
        base = root / class_name
        if not base.is_dir():
            continue
        count = 0
        for path in sorted(base.rglob("*"), reverse=True):
            try:
                if path.is_symlink():
                    continue
                if path.is_file() and current - path.stat().st_mtime >= MEDIA_RETENTION_SECONDS:
                    path.unlink()
                    count += 1
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                continue
        if count:
            removed[class_name] = removed.get(class_name, 0) + count
    return removed


def session_key(session_id: str | None = None, chat_id: str | None = None, task_id: str | None = None) -> str:
    return str(session_id or chat_id or task_id or "default")


def resolve_session_hash(session_id: str | None = None, chat_id: str | None = None, task_id: str | None = None) -> str:
    key = session_key(session_id, chat_id, task_id)
    hashed = security.safe_hash_id(key)
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
            if os.name == "nt":
                # ponytail: msvcrt retries ~10s then raises; manifest writes are
                # short, so that bound is acceptable contention behavior.
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _empty_manifest(key: str, sess_hash: str, *, now: float | None = None) -> dict:
    ts = now_iso(now)
    _, expires_at = _retention_fields(ts, now=now)
    return {
        "version": 2,
        "session_id": sess_hash,
        "session_hash": sess_hash,
        "chat_hash": security.safe_hash_id(key),
        "user_hash": "",
        "created_at": ts,
        "expires_at": expires_at,
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
            manifest = json.load(fh)
        if isinstance(manifest, dict):
            created_at, expires_at = _retention_fields(manifest.get("created_at"), manifest.get("expires_at"))
            manifest["created_at"] = created_at
            manifest["expires_at"] = expires_at
        return manifest
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


def session_expires_at(
    session_id: str | None = None, chat_id: str | None = None, task_id: str | None = None
) -> str | None:
    session_hash = resolve_session_hash(session_id, chat_id, task_id)
    path = get_manifest_path(session_hash)
    if not path.exists():
        return None
    manifest = load_manifest(session_hash)
    return str(manifest.get("expires_at") or "") or None


def require_active_session(
    session_id: str | None = None, chat_id: str | None = None, task_id: str | None = None
) -> dict:
    session_hash = resolve_session_hash(session_id, chat_id, task_id)
    _raise_if_tombstoned(session_hash)
    manifest = load_manifest(session_hash)
    if get_manifest_path(session_hash).exists() and is_expired(manifest.get("expires_at")):
        raise CassetteError(
            "session_expired",
            "This Cassette media session reached its 24-hour retention deadline. Ingest the source into a new session.",
            {"expires_at": manifest.get("expires_at")},
            recoverable=True,
        )
    return manifest


def remote_media_ids(session_hash: str) -> list[str]:
    manifest = load_manifest(session_hash)
    if is_expired(manifest.get("expires_at")):
        return []
    return [
        str(asset.get("media_file_id"))
        for asset in manifest.get("assets") or []
        if isinstance(asset, dict) and asset.get("media_file_id")
    ]


def mark_managed_asset_uploaded(
    session_id: str,
    saved_path: str,
    media_file_id: str,
    *,
    fingerprint: str,
    remote_expires_at: str | None = None,
) -> str | None:
    """Persist the remote binding and remove the now-redundant app-owned upload copy.

    The original user path is never stored or touched. Only a file inside this session's
    ``media`` directory is eligible for removal.
    """
    session_hash = resolve_session_hash(session_id=session_id)
    manifest_path = get_manifest_path(session_hash)
    if not manifest_path.exists():
        return None
    with manifest_lock(session_hash):
        manifest = load_manifest(session_hash)
        if is_expired(manifest.get("expires_at")):
            return str(manifest.get("expires_at") or "") or None
        local_deadline = str(manifest.get("expires_at") or "") or None
        remote_deadline = _parse_iso(remote_expires_at)
        local_dt = _parse_iso(local_deadline)
        selected_deadline = local_deadline
        if remote_deadline is not None and (local_dt is None or remote_deadline < local_dt):
            selected_deadline = remote_deadline.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            manifest["expires_at"] = selected_deadline
        target = str(Path(saved_path).expanduser())
        for asset in manifest.get("assets") or []:
            if not isinstance(asset, dict) or str(asset.get("saved_path") or "") != target:
                continue
            asset.update(
                {
                    "media_file_id": str(media_file_id),
                    "upload_fingerprint": str(fingerprint),
                    "uploaded_at": now_iso(),
                    "remote_expires_at": selected_deadline,
                    "exists": False,
                }
            )
            break
        save_manifest_atomic(session_hash, manifest)

    path = Path(saved_path).expanduser()
    managed_media = get_session_dir(session_hash) / "media"
    try:
        lexical = Path(os.path.abspath(str(path)))
        if lexical.is_file() and lexical.is_relative_to(managed_media.resolve(strict=False)):
            lexical.unlink()
    except (OSError, ValueError):
        pass
    return selected_deadline


def session_thread_path(session_hash: str) -> Path:
    return get_session_dir(session_hash) / "session_thread.json"


def load_session_thread(session_hash: str) -> dict:
    path = session_thread_path(session_hash)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — a corrupt map just means minting a fresh thread
        return {}


def save_session_thread(session_hash: str, thread_id: str) -> None:
    path = session_thread_path(session_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"thread_id": thread_id, "updated_at": now_iso()}
    fd, tmp_name = tempfile.mkstemp(prefix=".session_thread.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:  # noqa: BLE001 — losing the map is recoverable (new thread next run)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


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
        raise CassetteError(
            "transcode_failed", "Failed to normalize gateway video for Cassette", {"stderr_tail": detail}
        )
    if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise CassetteError(
            "transcode_failed", "Failed to normalize gateway video for Cassette", {"reason": "empty_output"}
        )
    os.replace(tmp_path, dest)


def _register_asset(
    sess_hash: str,
    empty_manifest_key: str,
    digest: str,
    dest: Path,
    size: int,
    resolved_media_type: str,
    original_name: str,
    caption: str | None,
    message_id_hash: str,
    deduplicated: bool,
    *,
    on_manifest=None,
    asset_extra: dict | None = None,
) -> dict:
    """Shared manifest-lock tail for ingest_asset / ingest_internal_asset: upsert the asset into
    the session manifest and return the ingestion result. ``on_manifest`` runs inside the lock to
    apply caller-specific manifest fields (e.g. gateway delivery); ``asset_extra`` adds
    caller-specific asset fields (e.g. internal metadata)."""
    _raise_if_tombstoned(sess_hash)
    asset_id = f"asset_{digest[:12]}"
    with manifest_lock(sess_hash):
        _raise_if_tombstoned(sess_hash)
        manifest = load_manifest(sess_hash)
        if not manifest.get("assets") and manifest.get("session_id") == "default":
            manifest = _empty_manifest(empty_manifest_key, sess_hash)
        if is_expired(manifest.get("expires_at")):
            raise CassetteError(
                "session_expired",
                "This Cassette media session reached its 24-hour retention deadline. Ingest the source into a new session.",
                {"expires_at": manifest.get("expires_at")},
            )
        manifest["session_id"] = sess_hash
        manifest["session_hash"] = sess_hash
        if on_manifest is not None:
            on_manifest(manifest)
        existing = next((a for a in manifest["assets"] if a.get("sha256") == digest), None)
        asset = {
            "asset_id": asset_id,
            "sha256": digest,
            "saved_path": str(dest),
            "original_name": original_name,
            "extension": dest.suffix.lower(),
            "media_type": resolved_media_type,
            "size_bytes": size,
            "caption": caption or "",
            "message_id": message_id_hash,
            "created_at": existing.get("created_at") if existing else now_iso(),
            "exists": dest.exists(),
            **(asset_extra or {}),
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
        "expires_at": manifest.get("expires_at"),
    }


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
    sess_hash = security.safe_hash_id(key)
    require_active_session(session_id=session_id, chat_id=chat_id, task_id=task_id)

    resolved_media_type = (
        media_type if media_type in {"video", "image", "audio", "file", "unknown"} else _media_type_from_ext(ext)
    )
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

    def _apply_delivery(manifest: dict) -> None:
        manifest["chat_hash"] = security.safe_hash_id(chat_id or key)
        manifest["user_hash"] = security.safe_hash_id(user_id) if user_id else manifest.get("user_hash", "")
        if chat_id or user_id:
            delivery = dict(manifest.get("delivery") or {})
            delivery.update(
                {
                    "platform": platform or delivery.get("platform") or "",
                    "chat_id": chat_id or delivery.get("chat_id") or "",
                    "user_id": user_id or delivery.get("user_id") or "",
                    "message_id": message_id or delivery.get("message_id") or "",
                    "chat_type": chat_type or delivery.get("chat_type") or "",
                    "thread_id": thread_id or delivery.get("thread_id") or "",
                    "updated_at": now_iso(),
                }
            )
            manifest["delivery"] = delivery

    return _register_asset(
        sess_hash,
        key,
        digest,
        dest,
        size,
        resolved_media_type,
        original_name or source.name,
        caption,
        security.safe_hash_id(message_id) if message_id else "",
        deduplicated,
        on_manifest=_apply_delivery,
    )


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
        raise CassetteError(
            "internal_asset_outside_root", "Internal Cassette asset must live under the Cassette asset root"
        ) from exc
    if not source.exists() or not source.is_file():
        raise CassetteError("internal_asset_missing", "Internal Cassette asset was not found")
    ext = source.suffix.lower()
    if not ext:
        raise CassetteError("internal_asset_missing_extension", "Internal Cassette asset must have a file extension")
    size = source.stat().st_size
    digest = security.sha256_file(source)
    sess_hash = resolve_session_hash(session_id=session_id)
    require_active_session(session_id=session_id)
    media_dir = get_session_dir(sess_hash) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / f"{digest}{ext}"
    deduplicated = dest.exists()
    if source != dest and not deduplicated:
        shutil.copy2(source, dest)

    resolved_media_type = (
        media_type if media_type in {"video", "image", "audio", "file", "unknown"} else _media_type_from_ext(ext)
    )
    return _register_asset(
        sess_hash,
        session_id,
        digest,
        dest,
        size,
        resolved_media_type,
        original_name or source.name,
        caption,
        "",
        deduplicated,
        asset_extra={"metadata": metadata} if metadata else None,
    )


def list_assets(session_id: str | None = None, chat_id: str | None = None, task_id: str | None = None) -> dict:
    sess_hash = resolve_session_hash(session_id, chat_id, task_id)
    manifest = require_active_session(session_id, chat_id, task_id)
    changed = False
    for asset in manifest.get("assets", []):
        exists = Path(asset.get("saved_path", "")).exists()
        if asset.get("exists") != exists:
            asset["exists"] = exists
            changed = True
    if changed:
        save_manifest_atomic(sess_hash, manifest)
    return {"manifest_path": str(get_manifest_path(sess_hash)), "manifest": manifest}
