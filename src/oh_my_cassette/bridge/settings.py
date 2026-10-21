"""Bridge configuration, read from environment variables so every host configures it the same way."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from oh_my_cassette.auth import DEFAULT_RESOURCE, saved_connection

MB = 1024 * 1024


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _env_number(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def _env_flag(name: str) -> bool:
    return (_env(name) or "").lower() in ("1", "true", "yes", "on")


def _env_path(name: str) -> Path | None:
    raw = _env(name)
    return Path(raw).expanduser().resolve() if raw else None


@dataclass(frozen=True)
class BridgeSettings:
    mcp_url: str
    workspace: Path
    # False when the workspace is a host's starting directory that spans the whole disk or the home directory:
    # files then come only from CASSETTE_ALLOWED_ROOTS, never from everything a host happened to start in.
    workspace_readable: bool = True
    auth_token: str | None = None
    allowed_roots: tuple[Path, ...] = ()
    download_dir: Path | None = None
    temp_dir: Path | None = None
    max_upload_bytes: int = 4096 * MB
    max_download_bytes: int = 16384 * MB
    upload_any_type: bool = False
    local_wait_sec: float = 240.0
    connect_timeout_sec: float = 10.0
    # The backend sends an SSE keepalive at least every 15 s, so a silent minute is a dead connection.
    read_timeout_sec: float = 60.0
    ffmpeg: str | None = None
    ffprobe: str | None = None
    host_name: str | None = None

    @property
    def downloads(self) -> Path:
        return self.download_dir or self.workspace / "cassette-exports"

    @property
    def temp_root(self) -> Path:
        """Where each local job makes its own private directory (`mkdtemp`: a fresh name, mode 0700), never a
        fixed path another user of the machine could create first."""
        return self.temp_dir or Path(tempfile.gettempdir())

    @property
    def roots(self) -> tuple[Path, ...]:
        """Directories local files may come from, symlinks resolved."""
        workspace = (self.workspace,) if self.workspace_readable else ()
        return tuple(root.resolve() for root in (*workspace, *self.allowed_roots))

    @classmethod
    def from_env(cls, cwd: str | os.PathLike[str] | None = None) -> BridgeSettings:
        explicit = _env("CASSETTE_WORKSPACE")
        workspace = Path(explicit or cwd or os.getcwd()).expanduser().resolve()
        broad = workspace in (Path(workspace.anchor), Path.home().resolve())
        roots = tuple(
            Path(item).expanduser()
            for item in (_env("CASSETTE_ALLOWED_ROOTS") or "").split(os.pathsep)
            if item
        )
        saved = saved_connection() if not _env("CASSETTE_MCP_URL") else None
        return cls(
            mcp_url=_env("CASSETTE_MCP_URL") or (saved.resource if saved else DEFAULT_RESOURCE),
            workspace=workspace,
            workspace_readable=bool(explicit) or not broad,
            auth_token=_env("CASSETTE_AUTH_TOKEN"),
            allowed_roots=roots,
            download_dir=_env_path("CASSETTE_DOWNLOAD_DIR"),
            temp_dir=_env_path("CASSETTE_TEMP_DIR"),
            max_upload_bytes=int(_env_number("CASSETTE_MAX_UPLOAD_MB", 4096) * MB),
            max_download_bytes=int(_env_number("CASSETTE_MAX_DOWNLOAD_MB", 16384) * MB),
            upload_any_type=_env_flag("CASSETTE_UPLOAD_ANY_TYPE"),
            local_wait_sec=_env_number("CASSETTE_LOCAL_WAIT_SEC", 240.0),
            connect_timeout_sec=_env_number("CASSETTE_CONNECT_TIMEOUT_SEC", 10.0),
            ffmpeg=_env("CASSETTE_FFMPEG"),
            ffprobe=_env("CASSETTE_FFPROBE"),
            host_name=_env("CASSETTE_MCP_HOST"),
        )
