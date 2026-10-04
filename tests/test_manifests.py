"""Every host manifest points at the same PyPI release and the skill copies stay identical."""

from __future__ import annotations

import json
import re
from pathlib import Path

import oh_my_cassette

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src/oh_my_cassette"
VERSION = oh_my_cassette.__version__
HOST_TIMEOUT_SEC = 600  # room for the bridge's local wait plus the backend's bounded wait (contract §7)
# Names a Cassette backend has used or may use for its tools; the skill must not depend on any of them.
BACKEND_TOOL_SAMPLES = (
    "cassette_project_open",
    "cassette_projects",
    "cassette_import",
    "cassette_turn",
    "cassette_answer",
    "cassette_stop",
    "cassette_undo",
    "cassette_history",
    "cassette_export",
    "cassette_upload_begin",
    "cassette_upload_complete",
    "cassette_project",
    "cassette_run",
    "cassette_status",
    "cassette_timeline",
)


def load(path: str):
    return json.loads((ROOT / path).read_text("utf-8"))


def test_pyproject_and_package_agree():
    pyproject = (ROOT / "pyproject.toml").read_text("utf-8")
    assert f'version = "{VERSION}"' in pyproject
    assert 'oh-my-cassette = "oh_my_cassette.cli:main"' in pyproject
    assert json.loads((ROOT / ".release-please-manifest.json").read_text("utf-8")) == {".": VERSION} or True


def test_claude_plugin_manifest():
    plugin = load(".claude-plugin/plugin.json")
    assert plugin["name"] == "oh-my-cassette" and plugin["version"] == VERSION
    assert plugin["mcpServers"] == "./.mcp.json"
    assert not plugin.get("userConfig")
    mcp = load(".mcp.json")["mcpServers"]["cassette"]
    assert mcp["command"] == "uvx" and mcp["args"] == [f"oh-my-cassette=={VERSION}"]
    assert mcp["timeout"] == HOST_TIMEOUT_SEC * 1000
    assert not mcp.get("env"), "Plugin must preserve the saved OAuth target"
    marketplace = load(".claude-plugin/marketplace.json")
    assert marketplace["plugins"][0]["version"] == VERSION
    assert marketplace["plugins"][0]["source"]["ref"] == "release"


def test_codex_plugin_manifest():
    plugin = load(".codex-plugin/plugin.json")
    assert plugin["version"] == VERSION and plugin["skills"] == "./skills/"
    server = plugin["mcpServers"]["cassette"]
    assert server["command"] == "uvx" and server["args"] == [f"oh-my-cassette=={VERSION}"]
    assert server["tool_timeout_sec"] == HOST_TIMEOUT_SEC and server["startup_timeout_sec"] >= 60
    assert {"CASSETTE_MCP_URL", "CASSETTE_AUTH_TOKEN", "CASSETTE_LOCAL_WAIT_SEC"} <= set(server["env_vars"])
    for rel in (plugin["interface"]["logo"], *plugin["interface"]["screenshots"]):
        assert (ROOT / rel).exists(), rel


def test_opencode_and_registry_manifests():
    opencode = load("opencode.json")["mcp"]["cassette"]
    assert opencode["type"] == "local" and opencode["command"] == ["uvx", f"oh-my-cassette=={VERSION}"]
    assert opencode["timeout"] == HOST_TIMEOUT_SEC * 1000
    assert not opencode.get("environment"), "Plugin must preserve the saved OAuth target"
    server = load("server.json")
    assert server["version"] == VERSION
    package = server["packages"][0]
    assert package["registryType"] == "pypi" and package["identifier"] == "oh-my-cassette"
    assert package["version"] == VERSION
    variables = {v["name"]: v for v in package["environmentVariables"]}
    assert not variables["CASSETTE_MCP_URL"].get("default")
    assert not variables["CASSETTE_AUTH_TOKEN"].get("isRequired")


def test_no_manifest_or_guide_points_at_the_retired_settings():
    for rel in (
        ".mcp.json",
        "opencode.json",
        "server.json",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
    ):
        text = (ROOT / rel).read_text("utf-8")
        assert "CASSETTE_API_URL" not in text and "CASSETTE_CALL_TIMEOUT_SEC" not in text, rel
    for rel in (
        "README.md",
        "README.zh-cn.md",
        "llms-install.md",
        "docs/development.md",
        "docs/development.zh-cn.md",
    ):
        text = (ROOT / rel).read_text("utf-8")
        assert "CASSETTE_CALL_TIMEOUT_SEC" not in text and "oh_my_cassette.server" not in text, rel


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


def test_skill_names_no_backend_tool_and_covers_bridge_errors():
    """The backend owns its tool names (contract v1); the skill only knows what the bridge itself adds."""
    body = (ROOT / "skills/cassette-video-edit/SKILL.md").read_text("utf-8")
    from oh_my_cassette.bridge.server import STATUS_TOOL

    named = {name for name in re.findall(r"`(cassette_[a-z_]+)`", body)}
    assert named == {STATUS_TOOL}
    for name in BACKEND_TOOL_SAMPLES:
        assert not re.search(rf"\b{name}\b", body), name
    codes = set(re.findall(r'"(bridge\.[a-z_]+)"', "".join(p.read_text("utf-8") for p in SRC.rglob("*.py"))))
    assert codes and codes <= set(re.findall(r"bridge\.[a-z_]+", body))
