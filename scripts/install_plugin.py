#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    ".env.e2e",
    "*.pyc",
)
CASSETTE_DEFAULT_URL = "https://sg.trycassette.online/agent"
CASSETTE_URL_OPTIONS = (
    ("1", "https://sg.trycassette.online/agent", "Asia"),
    ("2", "https://trycassette.online/agent", "America"),
)
AUTH_ENV_KEYS = (
    "CASSETTE_URL",
    "CASSETTE_FFMPEG_BIN",
    "CASSETTE_FFPROBE_BIN",
    "CASSETTE_AUTH_EMAIL",
    "CASSETTE_AUTH_PASSWORD",
    "JAMENDO_CLIENT_ID",
    "JAMENDO_CLIENT_SECRET",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def hermes_home(value: str | None) -> Path:
    return Path(value or os.getenv("HERMES_HOME", "~/.hermes")).expanduser().resolve()


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def remove_existing(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_env_values(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for key in AUTH_ENV_KEYS:
            prefix = f"{key}="
            export_prefix = f"export {key}="
            if stripped.startswith(export_prefix):
                values[key] = _unquote_env_value(stripped[len(export_prefix) :])
            elif stripped.startswith(prefix):
                values[key] = _unquote_env_value(stripped[len(prefix) :])
    return values


def _format_env_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("environment values must be single-line")
    if value == "":
        return '""'
    if any(ch.isspace() for ch in value) or "#" in value:
        if "'" not in value:
            return f"'{value}'"
        if '"' not in value:
            return f'"{value}"'
    return value


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        replaced = False
        for key, value in updates.items():
            prefix = f"{key}="
            export_prefix = f"export {key}="
            if stripped.startswith(export_prefix):
                new_lines.append(f"export {key}={_format_env_value(value)}")
                seen.add(key)
                replaced = True
                break
            if stripped.startswith(prefix):
                new_lines.append(f"{key}={_format_env_value(value)}")
                seen.add(key)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)
    if new_lines and new_lines[-1].strip():
        new_lines.append("")
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={_format_env_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(new_lines).rstrip() + "\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def hermes_python(home: Path) -> Path:
    return home / "hermes-agent" / "venv" / "bin" / "python"


def _path_is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _find_executable(name: str, existing: str = "") -> str:
    candidates: list[str] = []
    if existing:
        candidates.append(existing)
    found = shutil.which(name)
    if found:
        candidates.append(found)
    candidates.extend(
        [
            f"/opt/homebrew/bin/{name}",
            f"/usr/local/bin/{name}",
            f"/usr/bin/{name}",
        ]
    )
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        path = Path(candidate).expanduser()
        if _path_is_executable(path):
            return str(path)
    return ""


def configure_transcoder_paths(home: Path) -> bool:
    env_path = home / ".env"
    existing = read_env_values(env_path)
    updates: dict[str, str] = {}
    ffmpeg_bin = _find_executable("ffmpeg", existing.get("CASSETTE_FFMPEG_BIN", ""))
    ffprobe_bin = _find_executable("ffprobe", existing.get("CASSETTE_FFPROBE_BIN", ""))
    if ffmpeg_bin:
        updates["CASSETTE_FFMPEG_BIN"] = ffmpeg_bin
    if ffprobe_bin:
        updates["CASSETTE_FFPROBE_BIN"] = ffprobe_bin
    if updates:
        write_env_values(env_path, updates)
        if ffmpeg_bin:
            print(f"saved ffmpeg path to {env_path}: {ffmpeg_bin}")
        if ffprobe_bin:
            print(f"saved ffprobe path to {env_path}: {ffprobe_bin}")
        return True
    print(
        "ffmpeg was not found. Install it with `brew install ffmpeg` on macOS or `sudo apt-get install -y ffmpeg` on Debian/Ubuntu."
    )
    return False


def _run_command(cmd: list[str], *, dry_run: bool = False) -> int:
    printable = " ".join(cmd)
    if dry_run:
        print(f"would run: {printable}")
        return 0
    print(f"running: {printable}")
    return subprocess.run(cmd, check=False).returncode


def _ensure_pip(python: Path, *, dry_run: bool = False) -> bool:
    pip_check_code = _run_command([str(python), "-m", "pip", "--version"], dry_run=dry_run)
    if pip_check_code == 0:
        return True

    print("pip was not found in the Hermes Python environment; bootstrapping it with ensurepip...")
    ensurepip_code = _run_command([str(python), "-m", "ensurepip", "--upgrade"], dry_run=dry_run)
    if ensurepip_code != 0:
        print("pip bootstrap failed. Install pip in the Hermes Python environment, then rerun this installer.")
        return False
    return True


def install_hermes_playwright(home: Path, *, dry_run: bool = False) -> bool:
    python = hermes_python(home)
    if not python.exists():
        print(f"skip Playwright setup; Hermes Python was not found: {python}")
        return False
    if not _ensure_pip(python, dry_run=dry_run):
        return False
    pip_code = _run_command([str(python), "-m", "pip", "install", "playwright"], dry_run=dry_run)
    if pip_code != 0:
        print("Playwright Python package installation failed.")
        return False
    browser_code = _run_command([str(python), "-m", "playwright", "install", "chromium"], dry_run=dry_run)
    if browser_code != 0:
        print("Playwright Chromium installation failed.")
        return False
    return True


def restart_gateway(home: Path, *, dry_run: bool = False) -> bool:
    python = hermes_python(home)
    if not python.exists():
        print(f"skip gateway restart; Hermes Python was not found: {python}")
        return False
    env = os.environ.copy()
    env.setdefault("HERMES_ACCEPT_HOOKS", "1")
    cmd = [str(python), "-m", "hermes_cli.main", "gateway", "restart"]
    if dry_run:
        print(f"would run: {' '.join(cmd)}")
        return True
    print("restarting Hermes gateway...")
    proc = subprocess.run(cmd, check=False, env=env)
    if proc.returncode != 0:
        print("Hermes gateway restart failed. Run `hermes gateway restart` after fixing the reported issue.")
        return False
    return True


def enable_cassette_plugin(
    home: Path,
    *,
    input_func=input,
    interactive: bool | None = None,
    dry_run: bool = False,
) -> bool:
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        print("skip interactive Cassette plugin enable; run `hermes plugins enable cassette` if needed.")
        return False
    if not _yes(input_func("Enable Cassette plugin in Hermes Agent now? [Y/n]: "), default=True):
        print("skipped Cassette plugin enable; run `hermes plugins enable cassette` later if needed.")
        return False

    python = hermes_python(home)
    if not python.exists():
        print(f"skip Cassette plugin enable; Hermes Python was not found: {python}")
        return False
    env = os.environ.copy()
    env.setdefault("HERMES_ACCEPT_HOOKS", "1")
    cmd = [str(python), "-m", "hermes_cli.main", "plugins", "enable", "cassette"]
    if dry_run:
        print(f"would run: {' '.join(cmd)}")
        return True
    print("enabling Cassette plugin in Hermes Agent...")
    proc = subprocess.run(cmd, check=False, env=env)
    if proc.returncode != 0:
        print("Cassette plugin enable failed. Run `hermes plugins enable cassette` after fixing the reported issue.")
        return False
    return True


def _yes(value: str, default: bool = True) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    return normalized in {"y", "yes", "true", "1", "是", "好", "确认"}


def configure_cassette_auth(
    home: Path,
    *,
    input_func=input,
    password_func=getpass.getpass,
    interactive: bool | None = None,
) -> bool:
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    env_path = home / ".env"
    if not interactive:
        print(
            f"skip interactive Cassette auth setup; edit {env_path} to set CASSETTE_AUTH_EMAIL and CASSETTE_AUTH_PASSWORD."
        )
        return False

    existing = read_env_values(env_path)
    current_email = existing.get("CASSETTE_AUTH_EMAIL", "")
    current_password = existing.get("CASSETTE_AUTH_PASSWORD", "")
    prompt = "Configure Cassette login credentials now? [Y/n]: "
    if not _yes(input_func(prompt), default=True):
        print(f"skipped Cassette auth setup; edit {env_path} later if needed.")
        return False

    email_prompt = f"Cassette account email [{current_email}]: " if current_email else "Cassette account email: "
    email = input_func(email_prompt).strip() or current_email
    password_prompt = (
        "Cassette generated password [leave blank to keep existing]: "
        if current_password
        else "Cassette generated password: "
    )
    password = password_func(password_prompt).strip() or current_password
    if not email or not password:
        print("Cassette auth setup skipped because email or password was empty.")
        return False

    write_env_values(
        env_path,
        {
            "CASSETTE_AUTH_EMAIL": email,
            "CASSETTE_AUTH_PASSWORD": password,
        },
    )
    print(f"saved Cassette login credentials to {env_path}")
    return True


def configure_cassette_url(
    home: Path,
    *,
    input_func=input,
    interactive: bool | None = None,
) -> bool:
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    env_path = home / ".env"
    if not interactive:
        print(f"skip interactive Cassette URL setup; edit {env_path} to set CASSETTE_URL.")
        return False

    existing = read_env_values(env_path)
    current_url = existing.get("CASSETTE_URL", "")
    print("Select Cassette agent URL:")
    for key, url, region in CASSETTE_URL_OPTIONS:
        default_marker = " [default]" if url == CASSETTE_DEFAULT_URL else ""
        current_marker = " [current]" if current_url == url else ""
        print(f"  {key}) {url} ({region}){default_marker}{current_marker}")
    if current_url and current_url not in {url for _, url, _ in CASSETTE_URL_OPTIONS}:
        print(f"  current custom value: {current_url}")
    choice = input_func("Cassette URL region [1=Asia, 2=America, Enter=Asia/current]: ").strip()
    if not choice and current_url:
        selected_url = current_url
    elif not choice:
        selected_url = CASSETTE_DEFAULT_URL
    else:
        selected_url = ""
        for key, url, _region in CASSETTE_URL_OPTIONS:
            if choice == key:
                selected_url = url
                break
        if not selected_url:
            print(f"invalid Cassette URL choice: {choice}; skipped URL setup.")
            return False

    write_env_values(env_path, {"CASSETTE_URL": selected_url})
    print(f"saved Cassette URL to {env_path}: {selected_url}")
    return True


def configure_jamendo_auth(
    home: Path,
    *,
    input_func=input,
    password_func=getpass.getpass,
    interactive: bool | None = None,
) -> bool:
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    env_path = home / ".env"
    if not interactive:
        print(f"skip interactive Jamendo setup; edit {env_path} to set JAMENDO_CLIENT_ID and JAMENDO_CLIENT_SECRET.")
        return False

    existing = read_env_values(env_path)
    current_client_id = existing.get("JAMENDO_CLIENT_ID", "")
    current_client_secret = existing.get("JAMENDO_CLIENT_SECRET", "")
    prompt = "Configure Jamendo API credentials now? [y/N]: "
    if not _yes(input_func(prompt), default=False):
        print(f"skipped Jamendo setup; edit {env_path} later if needed.")
        return False

    client_id_prompt = f"Jamendo Client ID [{current_client_id}]: " if current_client_id else "Jamendo Client ID: "
    client_id = input_func(client_id_prompt).strip() or current_client_id
    secret_prompt = (
        "Jamendo Client Secret [leave blank to keep existing]: " if current_client_secret else "Jamendo Client Secret: "
    )
    client_secret = password_func(secret_prompt).strip() or current_client_secret
    if not client_id:
        print("Jamendo setup skipped because Client ID was empty.")
        return False

    updates = {"JAMENDO_CLIENT_ID": client_id}
    if client_secret:
        updates["JAMENDO_CLIENT_SECRET"] = client_secret
    write_env_values(env_path, updates)
    print(f"saved Jamendo API credentials to {env_path}")
    return True


def install_plugin(source: Path, dest: Path, *, copy: bool, force: bool, dry_run: bool) -> int:
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() and same_path(dest, source):
            print(f"cassette plugin already installed: {dest}")
            return 0
        if not force:
            print(f"refusing to replace existing path: {dest}", file=sys.stderr)
            print("rerun with --force, or choose --plugin-dir PATH", file=sys.stderr)
            return 2
        if not dry_run:
            remove_existing(dest)

    if dry_run:
        mode = "copy" if copy else "symlink"
        print(f"would install cassette plugin by {mode}: {source} -> {dest}")
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copytree(source, dest, ignore=COPY_IGNORE)
    else:
        dest.symlink_to(source, target_is_directory=True)

    print(f"installed cassette plugin: {dest}")
    print("restart Hermes after installation so it reloads plugins.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Cassette plugin into Hermes.")
    parser.add_argument("--hermes-home", help="Hermes home directory; defaults to $HERMES_HOME or ~/.hermes")
    parser.add_argument("--plugin-dir", help="Plugin destination; defaults to <Hermes home>/plugins/cassette")
    parser.add_argument("--copy", action="store_true", help="copy files instead of creating a symlink")
    parser.add_argument("--force", action="store_true", help="replace an existing destination")
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="skip plugin file installation and run only the setup steps (use after `hermes plugins install`)",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the destination without changing files")
    parser.add_argument(
        "--skip-plugin-enable", action="store_true", help="do not prompt to enable the Cassette plugin in Hermes"
    )
    parser.add_argument("--skip-cassette-url", action="store_true", help="do not prompt for Cassette agent URL")
    parser.add_argument(
        "--skip-cassette-auth", action="store_true", help="do not prompt for Cassette login credentials"
    )
    parser.add_argument("--skip-jamendo-auth", action="store_true", help="do not prompt for Jamendo API credentials")
    parser.add_argument(
        "--skip-playwright-install",
        action="store_true",
        help="do not install Playwright into the Hermes Python environment",
    )
    parser.add_argument("--skip-ffmpeg-detect", action="store_true", help="do not detect and save ffmpeg/ffprobe paths")
    parser.add_argument(
        "--skip-gateway-restart", action="store_true", help="do not restart Hermes gateway after installation"
    )
    args = parser.parse_args()

    source = repo_root()
    home = hermes_home(args.hermes_home)
    if args.setup_only:
        for flag, value in (("--copy", args.copy), ("--force", args.force), ("--plugin-dir", args.plugin_dir)):
            if value:
                print(f"--setup-only ignores {flag}", file=sys.stderr)
        code = 0
    else:
        dest = Path(args.plugin_dir).expanduser().resolve() if args.plugin_dir else home / "plugins" / "cassette"
        code = install_plugin(source, dest, copy=args.copy, force=args.force, dry_run=args.dry_run)
    if code == 0 and not args.dry_run and not args.skip_plugin_enable:
        enable_cassette_plugin(home)
    if code == 0 and not args.dry_run and not args.skip_cassette_url:
        configure_cassette_url(home)
    if code == 0 and not args.dry_run and not args.skip_cassette_auth:
        configure_cassette_auth(home)
    if code == 0 and not args.dry_run and not args.skip_jamendo_auth:
        configure_jamendo_auth(home)
    if code == 0 and not args.dry_run and not args.skip_ffmpeg_detect:
        configure_transcoder_paths(home)
    if code == 0 and not args.skip_playwright_install:
        install_hermes_playwright(home, dry_run=args.dry_run)
    if code == 0 and not args.skip_gateway_restart:
        restart_gateway(home, dry_run=args.dry_run)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
