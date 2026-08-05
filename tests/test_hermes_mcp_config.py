from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_helper():
    path = ROOT / "scripts" / "configure_hermes_mcp.py"
    spec = importlib.util.spec_from_file_location("configure_hermes_mcp", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_hermes_mcp_config_uses_the_shared_stdio_runtime_and_long_timeout(tmp_path):
    helper = _load_helper()
    plugin_root = tmp_path / "plugin"
    config = {
        "model": {"default": "example/model"},
        "mcp_servers": {
            "other": {"url": "https://example.test/mcp"},
            "cassette": {"url": "https://stale.test", "tools": {"exclude": ["cassette_edit"]}},
        },
    }

    merged = helper.merge_cassette_mcp(config, plugin_root)

    assert merged["model"] == config["model"]
    assert merged["mcp_servers"]["other"] == config["mcp_servers"]["other"]
    cassette = merged["mcp_servers"]["cassette"]
    assert cassette["command"] == "python3"
    assert cassette["args"] == [str(plugin_root / "scripts" / "run_local_mcp.py")]
    assert cassette["env"]["CASSETTE_MCP_HOST"] == "hermes"
    assert cassette["env"]["CASSETTE_AUTH_EMAIL"] == "${CASSETTE_AUTH_EMAIL}"
    assert cassette["env"]["CASSETTE_AUTH_PASSWORD"] == "${CASSETTE_AUTH_PASSWORD}"
    assert cassette["timeout"] == 1800
    assert cassette["connect_timeout"] == 240
    assert cassette["supports_parallel_tool_calls"] is False
    assert cassette["tools"] == {"exclude": ["cassette_edit"]}
    assert "url" not in cassette


def test_hermes_mcp_config_does_not_mutate_the_input(tmp_path):
    helper = _load_helper()
    original = {"mcp_servers": {"cassette": {"url": "https://stale.test"}}}

    helper.merge_cassette_mcp(original, tmp_path)

    assert original == {"mcp_servers": {"cassette": {"url": "https://stale.test"}}}
