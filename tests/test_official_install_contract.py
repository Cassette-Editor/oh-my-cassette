"""Contract tests for the official `hermes plugins install` channel.

Hermes installs plugins with `git clone --depth 1 <url>` into
`~/.hermes/plugins/<manifest name>`, copies root `*.example` files to their
real names, and later updates with `git pull`. These tests pin the repo
properties that flow depends on, without requiring hermes itself.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (ROOT / ".git").exists() or shutil.which("git") is None,
    reason="requires a git checkout and the git CLI",
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _shallow_fetch_head(destination: Path) -> None:
    # Equivalent to hermes' `git clone --depth 1` but works from a detached
    # HEAD too (CI checkouts), by fetching HEAD explicitly.
    destination.mkdir()
    assert _git(["init", "-q"], destination).returncode == 0
    fetch = _git(["fetch", "-q", "--depth", "1", f"file://{ROOT}", "HEAD"], destination)
    assert fetch.returncode == 0, fetch.stderr
    checkout = _git(["checkout", "-q", "FETCH_HEAD"], destination)
    assert checkout.returncode == 0, checkout.stderr


def test_shallow_clone_yields_installable_plugin(tmp_path):
    clone = tmp_path / "plugin"
    _shallow_fetch_head(clone)

    manifest = yaml.safe_load((clone / "plugin.yaml").read_text(encoding="utf-8"))
    # Hermes derives the install directory from the manifest name.
    assert manifest["name"] == "cassette"
    assert "/" not in manifest["name"] and "\\" not in manifest["name"]
    # The hermes installer refuses manifests newer than it understands.
    assert int(manifest["manifest_version"]) <= 1

    assert (clone / "__init__.py").exists()
    assert (clone / "after-install.md").exists()
    assert (clone / "scripts" / "install_plugin.py").exists()
    assert (clone / "scripts" / "diagnose_install.py").exists()


def test_root_example_file_copy_semantics_are_safe(tmp_path):
    clone = tmp_path / "plugin"
    _shallow_fetch_head(clone)

    # Hermes copies every root *.example file to its stem after install.
    examples = sorted(path.name for path in clone.glob("*.example"))
    assert examples == [".env.example"], (
        "unexpected root *.example files; hermes will materialize their stems "
        "inside the installed plugin directory — make sure each stem is gitignored"
    )

    # The materialized `.env` must be gitignored so `hermes plugins update`
    # (a plain `git pull`) never conflicts with it.
    check = _git(["check-ignore", ".env"], clone)
    assert check.returncode == 0, ".env must stay gitignored"
