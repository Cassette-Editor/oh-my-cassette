#!/usr/bin/env python3
"""Register this checkout with opencode's native MCP and skill directories.

opencode has no git-based plugin manager — its ``plugin`` array installs npm
packages only — so the native surfaces are the global config file and the
global skills directory.  This script writes both and leaves the checkout it
runs from as the installed plugin tree, the same shape as the Hermes installer.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(PLUGIN_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import runtime_config  # noqa: E402
from install_plugin import remove_existing  # noqa: E402


DEFAULT_REF = "release"
SERVER_NAME = "cassette"
SKILL_NAME = "cassette-video-edit"
OPENCODE_SCHEMA = "https://opencode.ai/config.json"


def opencode_home() -> Path:
    """opencode's global directory; also where it discovers global skills."""
    xdg = str(os.getenv("XDG_CONFIG_HOME", "") or "").strip()
    base = Path(os.path.expandvars(xdg)).expanduser() if xdg else Path.home() / ".config"
    return (base / "opencode").resolve()


def opencode_config_path(override: str | None = None) -> Path:
    """Resolve the config opencode actually reads.

    OPENCODE_CONFIG wins, then an existing opencode.jsonc, then opencode.json.
    Writing opencode.json blindly would leave a second, competing config next to
    a .jsonc the user already has.
    """
    if override:
        return Path(override).expanduser().resolve()
    env_path = str(os.getenv("OPENCODE_CONFIG", "") or "").strip()
    if env_path:
        return Path(os.path.expandvars(env_path)).expanduser().resolve()
    home = opencode_home()
    jsonc = home / "opencode.jsonc"
    return jsonc if jsonc.exists() else home / "opencode.json"


def skill_destination() -> Path:
    # Anchored to the opencode home, not the config file: OPENCODE_CONFIG may
    # point anywhere, but skills are only discovered under the global directory.
    return opencode_home() / "skills" / SKILL_NAME / "SKILL.md"


def mcp_server_entry(plugin_root: Path) -> dict[str, Any]:
    """The `mcp.cassette` block opencode launches; mirrors the repo opencode.json."""
    return {
        "type": "local",
        "command": [runtime_config.python_command(), str(plugin_root / "scripts" / "run_local_mcp.py")],
        "enabled": True,
        "environment": {"CASSETTE_MCP_HOST": "opencode"},
    }


class ManualStep(RuntimeError):
    """The config cannot be rewritten safely; the user must paste the block."""


def read_config(path: Path) -> dict[str, Any]:
    """Read the user's opencode config, tolerating a missing file.

    opencode accepts JSONC, which json.loads rejects. Stripping comments to
    parse would work, but writing the result back would silently delete them —
    so bail out and let the caller print the block to paste instead.
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManualStep(f"could not read {path}: {exc}") from exc
    try:
        value = json.loads(text)
    except ValueError as exc:
        if _looks_like_jsonc(text):
            raise ManualStep(f"{path} uses JSONC comments, which cannot be rewritten without dropping them") from exc
        raise ManualStep(f"could not parse {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManualStep(f"{path} must contain a JSON object")
    return value


def _looks_like_jsonc(text: str) -> bool:
    stripped = [line.strip() for line in text.splitlines()]
    return any(line.startswith("//") for line in stripped) or "/*" in text


def merge_server(config: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Set `mcp.cassette` without disturbing any other key the user owns."""
    merged = dict(config)
    merged.setdefault("$schema", OPENCODE_SCHEMA)
    servers = merged.get("mcp")
    servers = dict(servers) if isinstance(servers, dict) else {}
    servers[SERVER_NAME] = entry
    merged["mcp"] = servers
    return merged


def write_config(path: Path, config: dict[str, Any]) -> None:
    """Atomically replace the config, keeping the original file mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def install_skill(source: Path, dest: Path, *, dry_run: bool) -> None:
    if not source.is_file():
        raise SystemExit(f"skill source is missing: {source}")
    if dry_run:
        print(f"would install skill: {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        remove_existing(dest)
    shutil.copyfile(source, dest)
    print(f"installed skill: {dest}")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def sync_checkout(plugin_root: Path, ref: str, *, dry_run: bool) -> bool:
    """Fast-forward the checkout to the tip of `ref` on its own remote."""
    if not (plugin_root / ".git").exists():
        print(f"skip --sync; not a git checkout: {plugin_root}", file=sys.stderr)
        return False
    if dry_run:
        print(f"would sync {plugin_root} to origin/{ref}")
        return True
    fetch = _git(["fetch", "--depth", "1", "origin", ref], plugin_root)
    if fetch.returncode != 0:
        print(f"git fetch failed: {fetch.stderr.strip()}", file=sys.stderr)
        return False
    reset = _git(["reset", "--hard", "FETCH_HEAD"], plugin_root)
    if reset.returncode != 0:
        print(f"git reset failed: {reset.stderr.strip()}", file=sys.stderr)
        return False
    print(f"synced {plugin_root} to origin/{ref}")
    return True


def report_credentials(plugin_root: Path) -> bool:
    """Credentials live in the shared config root, so Codex/Claude users are already set up."""
    try:
        credentials = runtime_config.load_credentials()
    except runtime_config.RuntimeConfigError as exc:
        print(f"could not read stored credentials: {exc}", file=sys.stderr)
        return False
    if str(credentials.get("source") or "") == "missing":
        print("no Cassette credentials found yet. Finish setup with:")
        print(f"  {runtime_config.setup_command(plugin_root)}")
        return False
    print(f"using existing Cassette credentials ({credentials.get('source')})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Register Oh My Cassette with opencode.")
    parser.add_argument(
        "--opencode-config",
        help="opencode config file; defaults to $OPENCODE_CONFIG, else the existing "
        "~/.config/opencode/opencode.jsonc, else opencode.json",
    )
    parser.add_argument("--plugin-dir", help="plugin tree to register; defaults to this checkout")
    parser.add_argument("--ref", default=DEFAULT_REF, help=f"branch used by --sync (default: {DEFAULT_REF})")
    parser.add_argument("--sync", action="store_true", help="fast-forward the checkout to the release channel first")
    parser.add_argument("--dry-run", action="store_true", help="show what would change without writing")
    args = parser.parse_args()

    plugin_root = Path(args.plugin_dir).expanduser().resolve() if args.plugin_dir else PLUGIN_ROOT
    launcher = plugin_root / "scripts" / "run_local_mcp.py"
    if not launcher.is_file():
        print(f"not an Oh My Cassette checkout: {plugin_root}", file=sys.stderr)
        return 2

    if args.sync:
        sync_checkout(plugin_root, args.ref, dry_run=args.dry_run)

    entry = mcp_server_entry(plugin_root)
    config_path = opencode_config_path(args.opencode_config)
    manual = False
    try:
        config = merge_server(read_config(config_path), entry)
    except ManualStep as exc:
        # Never rewrite a config we cannot round-trip; show the exact block.
        manual = True
        print(f"{exc}", file=sys.stderr)
        print(f"\nAdd this to {config_path} by hand, then re-run with --opencode-config if you move it:\n")
        print(json.dumps({"mcp": {SERVER_NAME: entry}}, indent=2))
        print()
    else:
        if args.dry_run:
            print(f"would write {config_path}:")
            print(json.dumps(config["mcp"][SERVER_NAME], indent=2))
        else:
            write_config(config_path, config)
            print(f"registered MCP server '{SERVER_NAME}' in {config_path}")

    install_skill(
        plugin_root / "skills" / SKILL_NAME / "SKILL.md",
        skill_destination(),
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        report_credentials(plugin_root)
        print("restart opencode so it reloads the MCP server and skills.")
    return 1 if manual else 0


if __name__ == "__main__":
    raise SystemExit(main())
