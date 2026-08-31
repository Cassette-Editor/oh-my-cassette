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
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


RUNTIME_ADAPTER_ENV = "CASSETTE_RUNTIME_ADAPTER"
MCP_ADAPTER = "mcp"
WEB_ADAPTER = "web"
CONFIG_DIR_MODE = 0o700
CONFIG_FILE_MODE = 0o600
JAMENDO_DEVELOPER_PORTAL = "https://devportal.jamendo.com/"
JAMENDO_API_BASE_URL = "https://api.jamendo.com/v3.0"
DEFAULT_CASSETTE_API_URL = "https://cassette-editor-preview.cassette-editor-crimson2077.workers.dev"


def _is_windows() -> bool:
    return sys.platform == "win32"


_REQUEST_MEDIA_ROOTS: contextvars.ContextVar[tuple[Path, ...]] = contextvars.ContextVar(
    "cassette_request_media_roots", default=()
)


class RuntimeConfigError(RuntimeError):
    """A protected local configuration file failed a security check."""

    def __init__(self, code: str, message: str, *, path: Path | None = None):
        super().__init__(message)
        self.code = code
        self.path = path


class JamendoValidationError(RuntimeError):
    """A Client ID could not be verified without exposing it in the error."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


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
    if _is_windows():
        appdata = str(os.getenv("APPDATA", "") or "").strip()
        base = Path(appdata) if appdata else _home() / "AppData" / "Roaming"
        return _absolute_lexical(base / "Oh My Cassette")
    xdg = str(os.getenv("XDG_CONFIG_HOME", "") or "").strip()
    base = Path(os.path.expandvars(xdg)).expanduser() if xdg else _home() / ".config"
    return _absolute_lexical(base / "oh-my-cassette")


def data_root() -> Path:
    override = str(os.getenv("CASSETTE_DATA_HOME", "") or "").strip()
    if override:
        return _absolute_lexical(Path(os.path.expandvars(override)))
    if sys.platform == "darwin":
        return _absolute_lexical(_home() / "Library" / "Application Support" / "Oh My Cassette" / "data")
    if _is_windows():
        local = str(os.getenv("LOCALAPPDATA", "") or "").strip()
        base = Path(local) if local else _home() / "AppData" / "Local"
        return _absolute_lexical(base / "Oh My Cassette" / "data")
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
    if not _is_windows():
        # ponytail: POSIX mode bits are meaningless on NTFS; Windows relies on the
        # user-profile ACLs that %APPDATA%/%LOCALAPPDATA% inherit.
        os.chmod(path, CONFIG_DIR_MODE)
    return path


def _check_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeConfigError(
            "config_directory_missing", "Configuration directory does not exist", path=path
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeConfigError("config_symlink", "Configuration directory must not be a symlink", path=path)
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeConfigError("config_not_directory", "Configuration path is not a directory", path=path)
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeConfigError(
            "config_wrong_owner", "Configuration directory must be owned by the current user", path=path
        )
    if not _is_windows() and stat.S_IMODE(info.st_mode) & 0o077:
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
    if not _is_windows() and stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeConfigError(
            "config_permissions_too_open",
            "Configuration file permissions must be 0600 or stricter",
            path=path,
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeConfigError(
            "config_wrong_owner", "Configuration file must be owned by the current user", path=path
        )
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
        if hasattr(os, "fchmod"):
            os.fchmod(fd, CONFIG_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if not _is_windows():
            os.chmod(path, CONFIG_FILE_MODE)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def load_settings() -> dict[str, Any]:
    return read_protected_json(settings_path())


def stored_jamendo() -> dict[str, Any]:
    settings = load_settings()
    providers = settings.get("providers") or {}
    if not isinstance(providers, dict):
        raise RuntimeConfigError("config_invalid_shape", "settings.providers must be an object", path=settings_path())
    jamendo = providers.get("jamendo") or {}
    if not isinstance(jamendo, dict):
        raise RuntimeConfigError(
            "config_invalid_shape", "settings.providers.jamendo must be an object", path=settings_path()
        )
    return {
        "client_id": str(jamendo.get("client_id") or "").strip(),
        "verified_at": jamendo.get("verified_at"),
    }


def store_jamendo_client_id(client_id: str, *, verified_at: str) -> None:
    settings = load_settings()
    providers = settings.get("providers") or {}
    if not isinstance(providers, dict):
        raise RuntimeConfigError("config_invalid_shape", "settings.providers must be an object", path=settings_path())
    existing = providers.get("jamendo") or {}
    if not isinstance(existing, dict):
        raise RuntimeConfigError(
            "config_invalid_shape", "settings.providers.jamendo must be an object", path=settings_path()
        )
    updated_providers = {
        **providers,
        "jamendo": {**existing, "client_id": client_id.strip(), "verified_at": verified_at},
    }
    write_protected_json(settings_path(), {**settings, "providers": updated_providers})


def hermes_env_path() -> Path:
    explicit = str(os.getenv("HERMES_ENV_FILE", "") or "").strip()
    if explicit:
        return _absolute_lexical(Path(os.path.expandvars(explicit)))
    home = str(os.getenv("HERMES_HOME", "") or "").strip()
    root = Path(os.path.expandvars(home)).expanduser() if home else _home() / ".hermes"
    return _absolute_lexical(root / ".env")


def _dotenv_value(path: Path, name: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if key == name:
            text = value.strip()
            if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
                return text[1:-1]
            return text
    return ""


def stored_hermes_jamendo() -> dict[str, Any]:
    return {"client_id": _dotenv_value(hermes_env_path(), "JAMENDO_CLIENT_ID"), "verified_at": None}


def store_hermes_jamendo_client_id(client_id: str) -> Path:
    """Atomically update only JAMENDO_CLIENT_ID in Hermes's private dotenv file."""
    path = hermes_env_path()
    if path.is_symlink():
        raise RuntimeConfigError("config_symlink", "Hermes environment file must not be a symlink", path=path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise RuntimeConfigError("config_read_failed", "Could not read the Hermes environment file", path=path) from exc
    rendered: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        key = ""
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key.startswith("export "):
                key = key[7:].strip()
        if key == "JAMENDO_CLIENT_ID":
            prefix = "export " if stripped.startswith("export ") else ""
            rendered.append(f"{prefix}JAMENDO_CLIENT_ID={client_id.strip()}")
            replaced = True
        else:
            rendered.append(line)
    if not replaced:
        rendered.append(f"JAMENDO_CLIENT_ID={client_id.strip()}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, CONFIG_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(rendered).rstrip("\n") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if not _is_windows():
            os.chmod(path, CONFIG_FILE_MODE)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return path


def resolve_jamendo_client_id() -> dict[str, Any]:
    direct = str(os.getenv("JAMENDO_CLIENT_ID", "") or "").strip()
    if direct:
        return {"client_id": direct, "source": "environment", "verified_at": None}
    if is_mcp_runtime() and str(os.getenv("CASSETTE_MCP_HOST", "") or "").strip().lower() == "hermes":
        stored = stored_hermes_jamendo()
        client_id = str(stored.get("client_id") or "").strip()
        return {"client_id": client_id, "source": "hermes_env" if client_id else "missing", "verified_at": None}
    stored = stored_jamendo() if is_mcp_runtime() else {}
    return {
        "client_id": str(stored.get("client_id") or "").strip(),
        "source": "local_config" if stored.get("client_id") else "missing",
        "verified_at": stored.get("verified_at"),
    }


def mask_client_id(client_id: str) -> str:
    value = str(client_id or "").strip()
    if not value:
        return ""
    visible = min(4, max(1, len(value) // 3))
    return f"{'*' * max(4, len(value) - visible)}{value[-visible:]}"


def validate_jamendo_client_id(
    client_id: str,
    *,
    base_url: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Verify read-only Jamendo access with a one-row Tracks request."""
    value = str(client_id or "").strip()
    if not value:
        raise JamendoValidationError("jamendo_client_id_missing", "A Jamendo Client ID is required.")
    endpoint = (base_url or os.getenv("JAMENDO_BASE_URL") or JAMENDO_API_BASE_URL).rstrip("/") + "/tracks/"
    query = urlencode({"client_id": value, "format": "json", "limit": 1})
    request = Request(
        f"{endpoint}?{query}",
        headers={"Accept": "application/json", "User-Agent": "oh-my-cassette jamendo-setup/1"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        if exc.code == 429:
            raise JamendoValidationError(
                "jamendo_rate_limited", "Jamendo rate-limited the validation request.", details={"status": 429}
            ) from exc
        if exc.code in {400, 401, 403}:
            raise JamendoValidationError(
                "jamendo_client_id_invalid", "Jamendo rejected that Client ID.", details={"status": exc.code}
            ) from exc
        raise JamendoValidationError(
            "jamendo_validation_http_error",
            "Jamendo could not validate the Client ID.",
            details={"status": exc.code},
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise JamendoValidationError(
            "jamendo_validation_network_error",
            "Jamendo could not be reached, so the Client ID was not stored.",
            details={"type": type(exc).__name__},
        ) from exc
    if status < 200 or status >= 300:
        raise JamendoValidationError(
            "jamendo_validation_http_error", "Jamendo could not validate the Client ID.", details={"status": status}
        )
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise JamendoValidationError(
            "jamendo_validation_invalid_json", "Jamendo returned an invalid validation response."
        ) from exc
    headers = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(headers, dict) or str(headers.get("status") or "").lower() != "success":
        details: dict[str, Any] = {}
        if isinstance(headers, dict) and headers.get("code") is not None:
            details["jamendo_code"] = headers.get("code")
        raise JamendoValidationError("jamendo_client_id_invalid", "Jamendo rejected that Client ID.", details=details)
    return {"status": "success", "results_count": int(headers.get("results_count") or 0)}


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
        }
    stored = read_protected_json(credentials_path())
    # Access level is deliberately not read back: the plugin serves agent accounts and treats
    # every Cassette operation, export included, as one they are entitled to. Files written by
    # older versions may still carry a full_api_access key; it is ignored, not migrated.
    return {
        "email": str(stored.get("email") or "").strip(),
        "password": str(stored.get("password") or "").strip(),
        "source": "local_config" if stored else "missing",
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


def python_command() -> str:
    """The interpreter command a user types in their terminal on this platform."""
    return "python" if _is_windows() else "python3"


def _quote_for_shell(value: str) -> str:
    if _is_windows():
        return f'"{value}"' if " " in value else value
    return shlex.quote(value)


def setup_command(plugin_root: Path | None = None) -> str:
    override = str(os.getenv("CASSETTE_MCP_SETUP_COMMAND", "") or "").strip()
    if override:
        return override
    root = plugin_root or Path(__file__).resolve().parent
    return f"{python_command()} {_quote_for_shell(str(root / 'scripts' / 'setup_local_mcp.py'))}"


def reset_password_command(plugin_root: Path | None = None) -> str:
    return setup_command(plugin_root) + " --reset-password"


def jamendo_setup_command(plugin_root: Path | None = None) -> str:
    command = setup_command(plugin_root) + " --jamendo"
    if str(os.getenv("CASSETTE_MCP_HOST", "") or "").strip().lower() == "hermes":
        command += " --host hermes"
    return command


def configure_mcp_process_environment() -> list[RuntimeConfigError]:
    """Set MCP-only process defaults without preventing server initialization.

    Security errors are returned to the runtime and surfaced by affected tools;
    initialization itself remains successful so clients can discover the setup
    command.
    """
    os.environ[RUNTIME_ADAPTER_ENV] = MCP_ADAPTER
    os.environ.setdefault("CASSETTE_ASSET_ROOT", str(asset_root()))
    errors: list[RuntimeConfigError] = []
    for path in (config_root(), data_root(), asset_root()):
        try:
            ensure_private_dir(path)
        except RuntimeConfigError as exc:
            errors.append(exc)
    try:
        # A stored transport is read but no longer honored — there is one transport. Loading
        # settings still matters: it surfaces a permissions/ownership problem with the file.
        load_settings()
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
    if name == "JAMENDO_CLIENT_ID":
        return str(resolve_jamendo_client_id().get("client_id") or "").strip()
    return ""
