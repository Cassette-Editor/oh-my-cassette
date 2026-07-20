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
    assert _json(".codex-plugin/plugin.json")["mcpServers"] == "./.codex-plugin/mcp.json"
    codex = _json(".codex-plugin/mcp.json")["mcpServers"]
    # Claude external MCP files are the server map itself, not the generic
    # project-level {"mcpServers": ...} wrapper.
    claude = _json(".claude-plugin/mcp.json")
    project = _json(".mcp.json")["mcpServers"]
    assert set(codex) == set(claude) == set(project) == {"cassette"}
    for config in (codex["cassette"], claude["cassette"], project["cassette"]):
        assert config["command"] == "python3"
        assert any("run_local_mcp.py" in item for item in config["args"])
        assert "url" not in config and "port" not in config
    assert "cwd" not in codex["cassette"]
    assert {"CODEX_HOME", "CASSETTE_CONFIG_HOME", "CASSETTE_DATA_HOME"} <= set(codex["cassette"]["env_vars"])
    assert "${CLAUDE_PLUGIN_ROOT}" in claude["cassette"]["args"][0]
    assert claude["cassette"]["env"]["CASSETTE_PROJECT_ROOT"] == "${CLAUDE_PROJECT_DIR}"


def test_repo_root_mcp_config_is_claude_project_scoped_not_codex():
    # Claude Code auto-loads a repo-root .mcp.json for everyone who opens this
    # checkout, so it must be the Claude project shape and must never contain
    # the Codex cache-glob launcher or Codex-only fields (the exact regression
    # that made the MCP fail inside Claude).
    project = _json(".mcp.json")["mcpServers"]["cassette"]
    assert "CODEX_HOME" not in json.dumps(project)
    for codex_only in ("env_vars", "startup_timeout_sec", "tool_timeout_sec"):
        assert codex_only not in project
    assert project["args"] == ["${CLAUDE_PROJECT_DIR:-.}/scripts/run_local_mcp.py"]


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


def test_opencode_project_config_and_agents_skill_copy_stay_in_sync():
    config = _json("opencode.json")["mcp"]["cassette"]
    assert config["type"] == "local"
    assert config["command"] == ["python3", "scripts/run_local_mcp.py"]
    assert config["environment"]["CASSETTE_MCP_HOST"] == "opencode"

    neutral = (ROOT / "skills" / "cassette-video-edit" / "SKILL.md").read_text("utf-8")
    agents_copy = (ROOT / ".agents" / "skills" / "cassette-video-edit" / "SKILL.md").read_text("utf-8")
    assert agents_copy == neutral
