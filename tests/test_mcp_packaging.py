from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from cassette import register


ROOT = Path(__file__).resolve().parents[1]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text("utf-8"))


def test_dual_manifests_and_marketplaces_have_matching_identity_and_version():
    codex = _json(".codex-plugin/plugin.json")
    claude = _json(".claude-plugin/plugin.json")
    claude_market = _json(".claude-plugin/marketplace.json")
    codex_market = _json(".agents/plugins/marketplace.json")
    hermes = yaml.safe_load((ROOT / "plugin.yaml").read_text("utf-8"))

    assert codex["name"] == claude["name"] == "oh-my-cassette"
    assert codex_market["name"] == claude_market["name"] == "cassette-editor"
    assert codex_market["plugins"][0]["name"] == claude_market["plugins"][0]["name"] == codex["name"]
    versions = {codex["version"], claude["version"], claude_market["plugins"][0]["version"], hermes["version"]}
    assert len(versions) == 1
    assert re.fullmatch(r"\d+\.\d+\.\d+", versions.pop())


def test_host_configs_use_one_stdio_server_and_no_network_listener():
    assert _json(".claude-plugin/plugin.json")["mcpServers"] == "./.claude-plugin/mcp.json"
    codex = _json(".mcp.json")["mcpServers"]
    # Claude external MCP files are the server map itself, not the generic
    # project-level {"mcpServers": ...} wrapper.
    claude = _json(".claude-plugin/mcp.json")
    assert set(codex) == set(claude) == {"cassette"}
    for config in (codex["cassette"], claude["cassette"]):
        assert config["command"] == "python3"
        assert any("run_local_mcp.py" in item for item in config["args"])
        assert "url" not in config and "port" not in config
    assert "cwd" not in codex["cassette"]
    assert {"CODEX_HOME", "CASSETTE_CONFIG_HOME", "CASSETTE_DATA_HOME"} <= set(
        codex["cassette"]["env_vars"]
    )
    assert "${CLAUDE_PLUGIN_ROOT}" in claude["cassette"]["args"][0]
    assert claude["cassette"]["env"]["CASSETTE_PROJECT_ROOT"] == "${CLAUDE_PROJECT_DIR}"


def test_native_hosts_load_only_host_neutral_skill_and_hermes_keeps_its_skill():
    neutral = (ROOT / "skills" / "cassette-video-edit" / "SKILL.md").read_text("utf-8")
    hermes = (ROOT / "hermes" / "skills" / "cassette-video-edit" / "SKILL.md").read_text("utf-8")
    assert "Codex or Claude" in neutral
    assert "gateway user" not in neutral
    assert "Hermes" in hermes

    class Context:
        def __init__(self):
            self.skills = []

        def register_tool(self, **_kwargs):
            pass

        def register_command(self, *_args, **_kwargs):
            pass

        def register_hook(self, *_args, **_kwargs):
            pass

        def register_skill(self, name, path, description=""):
            self.skills.append((name, Path(path), description))

    context = Context()
    register(context)
    assert context.skills[0][1] == ROOT / "hermes" / "skills" / "cassette-video-edit" / "SKILL.md"


def test_release_please_updates_all_host_version_fields():
    config = _json("release-please-config.json")
    entries = config["packages"]["."]["extra-files"]
    entries_by_path = {entry["path"]: entry for entry in entries}
    assert {
        "plugin.yaml",
        ".codex-plugin/plugin.json",
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        "mcp_plugin/__init__.py",
    } <= entries_by_path.keys()
    assert entries_by_path["plugin.yaml"] == {
        "type": "generic",
        "path": "plugin.yaml",
    }
