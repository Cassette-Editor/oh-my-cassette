from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .errors import CassetteError


DEFAULT_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
}


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()


def get_allowed_source_roots() -> list[Path]:
    raw = os.getenv("CASSETTE_ALLOWED_SOURCE_ROOTS")
    dynamic_roots: list[Path] = []
    try:
        import runtime_config

        if runtime_config.is_mcp_runtime():
            dynamic_roots = runtime_config.all_mcp_media_roots()
    except Exception:  # noqa: BLE001 — callers surface config problems separately
        dynamic_roots = []
    if not raw and not dynamic_roots:
        home = _hermes_home()
        raw = os.pathsep.join(
            str(p) for p in (home / "weixin", home / "qqbot", home / "telegram", home / "cache", home / "tmp")
        )
    roots: list[Path] = []
    for item in (raw or "").split(os.pathsep):
        if item.strip():
            roots.append(Path(os.path.expandvars(item)).expanduser().resolve())
    for root in dynamic_roots:
        if root not in roots:
            roots.append(root)
    return roots


def get_allowed_extensions() -> set[str]:
    raw = os.getenv("CASSETTE_ALLOWED_EXTENSIONS")
    if not raw:
        return set(DEFAULT_EXTENSIONS)
    return {ext.strip().lower() for ext in raw.split(",") if ext.strip()}


def get_max_bytes() -> int:
    raw = os.getenv("CASSETTE_MAX_BYTES", "2147483648")
    try:
        return int(raw)
    except ValueError:
        return 2147483648


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_and_validate_source_path(source_path: str) -> Path:
    if not source_path or not str(source_path).strip():
        raise CassetteError("missing_required_arg", "source_path is required")
    path = Path(os.path.expandvars(source_path)).expanduser().resolve()
    if not path.exists():
        raise CassetteError("source_file_not_found", "Source file was not found")
    if not path.is_file():
        raise CassetteError("source_file_not_found", "Source path is not a file")
    roots = get_allowed_source_roots()
    if not any(_is_relative_to(path, root) for root in roots):
        raise CassetteError(
            "source_path_outside_allowed_roots",
            "Source path is outside CASSETTE_ALLOWED_SOURCE_ROOTS",
            {"allowed_root_count": len(roots)},
        )
    return path


def validate_extension(path: Path) -> str:
    ext = path.suffix.lower()
    if ext not in get_allowed_extensions():
        raise CassetteError(
            "disallowed_extension", f"Extension {ext or '<none>'} is not allowed for Cassette ingestion"
        )
    return ext


def validate_size(path: Path) -> int:
    size = path.stat().st_size
    max_bytes = get_max_bytes()
    if size > max_bytes:
        raise CassetteError(
            "file_too_large",
            "Source file is larger than CASSETTE_MAX_BYTES",
            {"size_bytes": size, "max_bytes": max_bytes},
        )
    return size


def sha256_file(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as exc:
        raise CassetteError("hash_failed", "Failed to hash source file", {"reason": type(exc).__name__}) from exc


def safe_hash_id(value: str | None) -> str:
    if not value:
        value = "default"
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def redact_for_log(value: str | None) -> str:
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"wxid_[A-Za-z0-9_-]+", "wxid_<redacted>", text)
    text = re.sub(r"(?i)(token|secret|key)=([^&\s]+)", r"\1=<redacted>", text)
    if len(text) > 96:
        return f"{text[:32]}...<redacted:{len(text)} chars>...{text[-16:]}"
    return text
