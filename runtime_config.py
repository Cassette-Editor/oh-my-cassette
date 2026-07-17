"""Adapter-aware local configuration for Oh My Cassette.

Hermes historically resolves values from its process environment and
``~/.hermes/.env``.  The local MCP runtime deliberately uses a separate,
host-neutral configuration directory shared by Codex and Claude.  The web demo
continues to use only its process environment.

This module is intentionally standard-library-only so the bootstrap and setup
commands can use it before the MCP virtual environment exists.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator


RUNTIME_ADAPTER_ENV = "CASSETTE_RUNTIME_ADAPTER"
MCP_ADAPTER = "mcp"
WEB_ADAPTER = "web"
CONFIG_DIR_MODE = 0o700
CONFIG_FILE_MODE = 0o600

_REQUEST_MEDIA_ROOTS: contextvars.ContextVar[tuple[Path, ...]] = contextvars.ContextVar(
    "cassette_request_media_roots", default=()
)


class RuntimeConfigError(RuntimeError):
    """A protected local configuration file failed a security check."""

    def __init__(self, code: str, message: str, *, path: Path | None = None):
        super().__init__(message)
        self.code = code
        self.path = path


def runtime_adapter() -> str:
    return str(os.getenv(RUNTIME_ADAPTER_ENV, "") or "").strip().lower()


def is_mcp_runtime() -> bool:
    return runtime_adapter() == MCP_ADAPTER


def _absolute_lexical(path: Path) -> Path:
    """Return an absolute normalized path without following symlinks."""
    return Path(os.path.abspath(str(path.expanduser())))


def _home() -> Path:
    return _absolute_lexical(Path.home())


def config_root() -> Path:
    override = str(os.getenv("CASSETTE_CONFIG_HOME", "") or "").strip()
    if override:
        return _absolute_lexical(Path(os.path.expandvars(override)))
    if sys.platform == "darwin":
        return _absolute_lexical(_home() / "Library" / "Application Support" / "Oh My Cassette")
    xdg = str(os.getenv("XDG_CONFIG_HOME", "") or "").strip()
    base = Path(os.path.expandvars(xdg)).expanduser() if xdg else _home() / ".config"
    return _absolute_lexical(base / "oh-my-cassette")


def data_root() -> Path:
    override = str(os.getenv("CASSETTE_DATA_HOME", "") or "").strip()
    if override:
        return _absolute_lexical(Path(os.path.expandvars(override)))
    if sys.platform == "darwin":
        return _absolute_lexical(
            _home() / "Library" / "Application Support" / "Oh My Cassette" / "data"
        )
    xdg = str(os.getenv("XDG_DATA_HOME", "") or "").strip()
    base = Path(os.path.expandvars(xdg)).expanduser() if xdg else _home() / ".local" / "share"
    return _absolute_lexical(base / "oh-my-cassette")


def credentials_path() -> Path:
    return config_root() / "credentials.json"


def settings_path() -> Path:
    return config_root() / "settings.json"


def asset_root() -> Path:
    override = str(os.getenv("CASSETTE_ASSET_ROOT", "") or "").strip()
    if override:
        return _absolute_lexical(Path(os.path.expandvars(override)))
    return _absolute_lexical(data_root() / "cassette")


def runtime_venv_root() -> Path:
    return _absolute_lexical(data_root() / "runtime")


def ensure_private_dir(path: Path) -> Path:
    """Create a private app-owned directory and reject a symlink target."""
    path = _absolute_lexical(path)
    # The platform-owned ancestors may have ordinary permissions; the app-owned
    # directory itself must not be a symlink and is always tightened to 0700.
    if path.is_symlink():
        raise RuntimeConfigError("config_symlink", "Configuration directory must not be a symlink", path=path)
    path.mkdir(parents=True, exist_ok=True, mode=CONFIG_DIR_MODE)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeConfigError("config_symlink", "Configuration directory must not be a symlink", path=path)
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeConfigError("config_not_directory", "Configuration path is not a private directory", path=path)
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeConfigError(
            "config_wrong_owner", "Configuration directory must be owned by the current user", path=path
        )
    os.chmod(path, CONFIG_DIR_MODE)
    return path


def _check_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeConfigError("config_directory_missing", "Configuration directory does not exist", path=path) from exc
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeConfigError("config_symlink", "Configuration directory must not be a symlink", path=path)
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeConfigError("config_not_directory", "Configuration path is not a directory", path=path)
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeConfigError(
            "config_wrong_owner", "Configuration directory must be owned by the current user", path=path
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeConfigError(
            "config_permissions_too_open",
            "Configuration directory permissions must be 0700 or stricter",
            path=path,
        )


def read_protected_json(path: Path, *, missing_ok: bool = True) -> dict[str, Any]:
    """Read an owner-private regular JSON file without following a symlink."""
    path = _absolute_lexical(path)
    if path.is_symlink():
        raise RuntimeConfigError("config_symlink", "Configuration file must not be a symlink", path=path)
    if not path.exists():
        if missing_ok:
            return {}
        raise RuntimeConfigError("config_file_missing", "Configuration file does not exist", path=path)
    _check_private_directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeConfigError("config_symlink", "Configuration file must not be a symlink", path=path)
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeConfigError("config_not_regular", "Configuration file must be a regular file", path=path)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeConfigError(
            "config_permissions_too_open",
            "Configuration file permissions must be 0600 or stricter",
            path=path,
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeConfigError("config_wrong_owner", "Configuration file must be owned by the current user", path=path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeConfigError("config_invalid_json", "Configuration file contains invalid JSON", path=path) from exc
    if not isinstance(value, dict):
        raise RuntimeConfigError("config_invalid_shape", "Configuration file must contain a JSON object", path=path)
    return value


def write_protected_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write a private JSON file without following a destination symlink."""
    path = _absolute_lexical(path)
    parent = ensure_private_dir(path.parent)
    if path.is_symlink():
        raise RuntimeConfigError("config_symlink", "Configuration file must not be a symlink", path=path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
    try:
        os.fchmod(fd, CONFIG_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, CONFIG_FILE_MODE)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def load_settings() -> dict[str, Any]:
    return read_protected_json(settings_path())


def load_credentials() -> dict[str, Any]:
    """Resolve credentials with process-environment precedence."""
    email = (
        str(os.getenv("CASSETTE_AUTH_EMAIL", "") or "").strip()
        or str(os.getenv("CASSETTE_AUTH_ACCOUNT", "") or "").strip()
        or str(os.getenv("CASSETTE_EMAIL", "") or "").strip()
    )
    password = (
        str(os.getenv("CASSETTE_AUTH_PASSWORD", "") or "").strip()
        or str(os.getenv("CASSETTE_PASSWORD", "") or "").strip()
    )
    if email or password:
        return {
            "email": email,
            "password": password,
            "source": "environment",
            "full_api_access": None,
        }
    stored = read_protected_json(credentials_path())
    return {
        "email": str(stored.get("email") or "").strip(),
        "password": str(stored.get("password") or "").strip(),
        "source": "local_config" if stored else "missing",
        "full_api_access": stored.get("full_api_access"),
        "verified_at": stored.get("verified_at"),
    }


def configured_media_roots() -> list[Path]:
    settings = load_settings()
    values = settings.get("media_roots") or []
    if not isinstance(values, list):
        raise RuntimeConfigError("config_invalid_shape", "settings.media_roots must be an array", path=settings_path())
    roots: list[Path] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            roots.append(Path(os.path.expandvars(text)).expanduser().resolve())
    return roots


def environment_project_roots() -> list[Path]:
    raw = str(os.getenv("CASSETTE_PROJECT_ROOTS", "") or os.getenv("CASSETTE_PROJECT_ROOT", "") or "")
    return [Path(os.path.expandvars(item)).expanduser().resolve() for item in raw.split(os.pathsep) if item.strip()]


def request_media_roots() -> list[Path]:
    return list(_REQUEST_MEDIA_ROOTS.get())


@contextlib.contextmanager
def temporary_media_roots(roots: list[Path] | tuple[Path, ...]) -> Iterator[None]:
    canonical = tuple(path.expanduser().resolve() for path in roots)
    token = _REQUEST_MEDIA_ROOTS.set(canonical)
    try:
        yield
    finally:
        _REQUEST_MEDIA_ROOTS.reset(token)


def all_mcp_media_roots() -> list[Path]:
    values = [*configured_media_roots(), *environment_project_roots(), *request_media_roots()]
    unique: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = str(value)
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def setup_command(plugin_root: Path | None = None) -> str:
    override = str(os.getenv("CASSETTE_MCP_SETUP_COMMAND", "") or "").strip()
    if override:
        return override
    root = plugin_root or Path(__file__).resolve().parent
    return f"python3 {shlex.quote(str(root / 'scripts' / 'setup_local_mcp.py'))}"


def browser_setup_command(plugin_root: Path | None = None) -> str:
    return setup_command(plugin_root) + " --with-browser"


def configure_mcp_process_environment() -> list[RuntimeConfigError]:
    """Set MCP-only process defaults without preventing server initialization.

    Security errors are returned to the runtime and surfaced by affected tools;
    initialization itself remains successful so clients can discover the setup
    command.
    """
    os.environ[RUNTIME_ADAPTER_ENV] = MCP_ADAPTER
    os.environ.setdefault("CASSETTE_ASSET_ROOT", str(asset_root()))
    browser_store = data_root() / "browsers"
    if browser_store.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_store))
    errors: list[RuntimeConfigError] = []
    for path in (config_root(), data_root(), asset_root()):
        try:
            ensure_private_dir(path)
        except RuntimeConfigError as exc:
            errors.append(exc)
    try:
        settings = load_settings()
        transport = str(settings.get("transport") or "").strip().lower()
        if transport in {"api", "browser"}:
            os.environ.setdefault("CASSETTE_TRANSPORT", transport)
    except RuntimeConfigError as exc:
        errors.append(exc)
    try:
        load_credentials()
    except RuntimeConfigError as exc:
        errors.append(exc)
    return errors


def mcp_env_value(name: str) -> str:
    """Resolve an MCP environment value; never imports Hermes configuration."""
    direct = str(os.getenv(name, "") or "").strip()
    if direct:
        return direct
    if not is_mcp_runtime():
        return ""
    if name in {"CASSETTE_AUTH_EMAIL", "CASSETTE_AUTH_ACCOUNT", "CASSETTE_EMAIL"}:
        return str(load_credentials().get("email") or "").strip()
    if name in {"CASSETTE_AUTH_PASSWORD", "CASSETTE_PASSWORD"}:
        return str(load_credentials().get("password") or "").strip()
    if name in {"CASSETTE_API_URL", "CASSETTE_API_BASE_URL"}:
        return str(load_settings().get("api_url") or "").strip()
    return ""
