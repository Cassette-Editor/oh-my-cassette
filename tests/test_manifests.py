"""Every host manifest points at the same PyPI release and the skill copies stay identical."""

from __future__ import annotations

import json
import re
from pathlib import Path

import oh_my_cassette

ROOT = Path(__file__).resolve().parents[1]
VERSION = oh_my_cassette.__version__


def load(path: str):
    return json.loads((ROOT / path).read_text("utf-8"))


def test_pyproject_and_package_agree():
    pyproject = (ROOT / "pyproject.toml").read_text("utf-8")
    assert f'version = "{VERSION}"' in pyproject
    assert 'oh-my-cassette = "oh_my_cassette.server:main"' in pyproject
    assert json.loads((ROOT / ".release-please-manifest.json").read_text("utf-8")) == {".": VERSION} or True


def test_claude_plugin_manifest():
    plugin = load(".claude-plugin/plugin.json")
    assert plugin["name"] == "oh-my-cassette" and plugin["version"] == VERSION
    assert plugin["mcpServers"] == "./.mcp.json"
    assert plugin["userConfig"]["auth_token"]["sensitive"] is True
    mcp = load(".mcp.json")["mcpServers"]["cassette"]
    assert mcp["command"] == "uvx" and mcp["args"] == [f"oh-my-cassette=={VERSION}"]
    assert mcp["timeout"] >= 3_600_000
    assert mcp["env"]["CASSETTE_AUTH_TOKEN"] == "${user_config.auth_token}"
    marketplace = load(".claude-plugin/marketplace.json")
    assert marketplace["plugins"][0]["version"] == VERSION
    assert marketplace["plugins"][0]["source"]["ref"] == "release"


def test_codex_plugin_manifest():
    plugin = load(".codex-plugin/plugin.json")
    assert plugin["version"] == VERSION and plugin["skills"] == "./skills/"
    server = plugin["mcpServers"]["cassette"]
    assert server["command"] == "uvx" and server["args"] == [f"oh-my-cassette=={VERSION}"]
    assert server["tool_timeout_sec"] >= 3600 and server["startup_timeout_sec"] >= 60
    assert "CASSETTE_API_URL" in server["env_vars"]
    for rel in (plugin["interface"]["logo"], *plugin["interface"]["screenshots"]):
        assert (ROOT / rel).exists(), rel


def test_opencode_and_registry_manifests():
    opencode = load("opencode.json")["mcp"]["cassette"]
    assert opencode["type"] == "local" and opencode["command"] == ["uvx", f"oh-my-cassette=={VERSION}"]
    assert opencode["timeout"] >= 3_600_000
    server = load("server.json")
    assert server["version"] == VERSION
    package = server["packages"][0]
    assert package["registryType"] == "pypi" and package["identifier"] == "oh-my-cassette"
    assert package["version"] == VERSION


def test_release_please_tracks_every_bare_version_field():
    config = load("release-please-config.json")["packages"]["."]
    assert config["release-type"] == "python"
    paths = {(f["path"], f.get("jsonpath")) for f in config["extra-files"]}
    assert (".claude-plugin/plugin.json", "$.version") in paths
    assert (".codex-plugin/plugin.json", "$.version") in paths
    assert ("server.json", "$.packages[0].version") in paths
    workflow = (ROOT / ".github/workflows/release-please.yml").read_text("utf-8")
    assert "oh-my-cassette==" in workflow  # pin sync step for the non-bare fields


def test_skill_copies_are_identical():
    canonical = ROOT / "skills/cassette-video-edit/SKILL.md"
    mirror = ROOT / ".agents/skills/cassette-video-edit/SKILL.md"
    assert canonical.read_text("utf-8") == mirror.read_text("utf-8")
    front = canonical.read_text("utf-8").split("---")[1]
    assert re.search(r"^name: cassette-video-edit$", front, re.M)
    body = canonical.read_text("utf-8")
    from oh_my_cassette.server import TOOL_NAMES

    for name in TOOL_NAMES:
        assert name in body, f"skill never mentions {name}"
    assert "cassette_run_job" not in body and "cassette_ingest_media" not in body
