from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

from cassette import register


ROOT = Path(__file__).resolve().parents[1]


class FakeContext:
    def __init__(self):
        self.tools = []
        self.commands = []
        self.hooks = []
        self.skills = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands.append({"name": name, "handler": handler, "description": description, "args_hint": args_hint})

    def register_hook(self, hook_name, callback):
        self.hooks.append((hook_name, callback))

    def register_skill(self, name, path, description=""):
        self.skills.append({"name": name, "path": path, "description": description})


def _load_manifest() -> dict:
    return yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


def _load_install_script():
    spec = importlib.util.spec_from_file_location("install_plugin", ROOT / "scripts" / "install_plugin.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_manifest_parses_with_required_fields():
    manifest = _load_manifest()
    assert manifest["name"] == "cassette"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["manifest_version"] == 1
    assert manifest["description"]


def test_manifest_version_line_carries_release_please_annotation():
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    version_lines = [line for line in text.splitlines() if line.startswith("version:")]
    assert len(version_lines) == 1
    assert "x-release-please-version" in version_lines[0]


def test_manifest_version_matches_version_txt():
    version_txt = ROOT / "version.txt"
    if not version_txt.exists():
        return  # seeded by the release automation change; enforced once present
    assert _load_manifest()["version"] == version_txt.read_text(encoding="utf-8").strip()


def test_provides_tools_and_hooks_match_register():
    manifest = _load_manifest()
    ctx = FakeContext()
    register(ctx)
    assert set(manifest["provides_tools"]) == {tool["name"] for tool in ctx.tools}
    assert set(manifest["provides_hooks"]) == {name for name, _ in ctx.hooks}


def test_requires_env_schema_matches_installer():
    manifest = _load_manifest()
    entries = {entry["name"]: entry for entry in manifest["requires_env"]}
    assert set(entries) == {"CASSETTE_AUTH_EMAIL", "CASSETTE_AUTH_PASSWORD"}
    for entry in entries.values():
        assert entry["description"]
        assert entry["url"].startswith("https://")
    assert entries["CASSETTE_AUTH_PASSWORD"]["secret"] is True
    assert "secret" not in entries["CASSETTE_AUTH_EMAIL"]

    install_plugin = _load_install_script()
    assert set(entries) <= set(install_plugin.AUTH_ENV_KEYS)


def test_after_install_mentions_the_setup_flow():
    text = (ROOT / "after-install.md").read_text(encoding="utf-8")
    assert "--setup-only" in text
    assert "hermes plugins enable cassette" in text
    assert "hermes gateway restart" in text
    assert "~/.hermes/.env" in text
