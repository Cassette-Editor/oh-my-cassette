"""Locked, idempotent virtual-environment bootstrap for the local MCP plugin."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import runtime_config  # noqa: E402


MIN_PYTHON_MINOR = 11
MAX_PYTHON_MINOR = 13


class BootstrapError(RuntimeError):
    pass


def _python_version(executable: str) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [executable, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        major, minor, patch = (int(value) for value in result.stdout.strip().split("."))
        return major, minor, patch
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _supported(version: tuple[int, int, int] | None) -> bool:
    return bool(version and version[0] == 3 and MIN_PYTHON_MINOR <= version[1] <= MAX_PYTHON_MINOR)


def select_python() -> tuple[str, tuple[int, int, int]]:
    candidates: list[str] = []
    override = str(os.getenv("CASSETTE_MCP_PYTHON", "") or "").strip()
    if override:
        candidates.append(str(Path(override).expanduser()))
    candidates.append(sys.executable)
    for name in ("python3.13", "python3.12", "python3.11"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        version = _python_version(candidate)
        if _supported(version):
            return candidate, version  # type: ignore[return-value]
    raise BootstrapError(
        "Oh My Cassette local MCP requires Python 3.11, 3.12, or 3.13. "
        "Install one of those versions or set CASSETTE_MCP_PYTHON to its executable."
    )


def _venv_python(venv: Path) -> Path:
    return venv / "bin" / "python"


def _fingerprint(lock_path: Path, version: tuple[int, int, int]) -> str:
    digest = hashlib.sha256()
    digest.update(lock_path.read_bytes())
    digest.update(f"python-{version[0]}.{version[1]}.{version[2]}".encode("ascii"))
    return digest.hexdigest()


def _run(command: list[str], *, environment: dict[str, str] | None = None, output: TextIO | None = None) -> None:
    sink = output or sys.stderr
    try:
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            env=environment,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BootstrapError(f"Runtime bootstrap command failed: {command[0]} ({type(exc).__name__})") from exc


def _read_marker(path: Path) -> dict:
    try:
        value = runtime_config.read_protected_json(path)
        return value if isinstance(value, dict) else {}
    except runtime_config.RuntimeConfigError as exc:
        raise BootstrapError(f"Runtime marker failed a security check: {exc}") from exc


def _write_marker(path: Path, value: dict) -> None:
    runtime_config.write_protected_json(path, value)


def bootstrap_runtime(*, with_browser: bool = False, output: TextIO | None = None) -> Path:
    if sys.platform not in {"darwin", "linux"}:
        raise BootstrapError("The local MCP launcher currently supports macOS and Linux only.")
    selected, version = select_python()
    data = runtime_config.ensure_private_dir(runtime_config.data_root())
    root = runtime_config.ensure_private_dir(data / "runtime")
    venv = root / f"python-{version[0]}.{version[1]}"
    lock_file = root / ".bootstrap.lock"
    if venv.is_symlink():
        raise BootstrapError("The plugin-managed virtual environment must not be a symlink.")
    if lock_file.is_symlink():
        raise BootstrapError("The runtime bootstrap lock must not be a symlink.")

    import fcntl

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_file, flags, 0o600)
    except OSError as exc:
        raise BootstrapError("Could not open the protected runtime bootstrap lock.") from exc
    os.fchmod(lock_fd, 0o600)
    with os.fdopen(lock_fd, "r+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        python = _venv_python(venv)
        if not python.exists():
            _run([selected, "-m", "venv", str(venv)], output=output)
        if not python.exists():
            raise BootstrapError("Python created no executable in the plugin-managed virtual environment.")

        base_lock = PLUGIN_ROOT / "requirements-mcp.lock"
        base_marker = venv / ".mcp-runtime.json"
        base_fingerprint = _fingerprint(base_lock, version)
        if _read_marker(base_marker).get("fingerprint") != base_fingerprint:
            _run([str(python), "-m", "ensurepip", "--upgrade"], output=output)
            _run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--requirement",
                    str(base_lock),
                ],
                output=output,
            )
            _write_marker(
                base_marker,
                {
                    "fingerprint": base_fingerprint,
                    "python": ".".join(str(value) for value in version),
                    "mcp": "1.12.4",
                },
            )

        if with_browser:
            browser_lock = PLUGIN_ROOT / "requirements-browser.lock"
            browser_marker = venv / ".browser-runtime.json"
            browser_fingerprint = _fingerprint(browser_lock, version)
            if _read_marker(browser_marker).get("fingerprint") != browser_fingerprint:
                _run(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--no-input",
                        "--requirement",
                        str(browser_lock),
                    ],
                    output=output,
                )
                browser_path = runtime_config.ensure_private_dir(runtime_config.data_root() / "browsers")
                environment = os.environ.copy()
                environment["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_path)
                _run([str(python), "-m", "playwright", "install", "chromium"], environment=environment, output=output)
                _write_marker(browser_marker, {"fingerprint": browser_fingerprint, "playwright": "1.60.0"})
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return python
