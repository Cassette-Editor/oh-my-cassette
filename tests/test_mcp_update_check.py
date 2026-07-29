from __future__ import annotations

import json
import time

import pytest

import runtime_config
from mcp_plugin import update_check


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CASSETTE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("CASSETTE_UPDATE_CHECK", raising=False)
    monkeypatch.setenv("CASSETTE_MCP_HOST", "codex")
    monkeypatch.setattr(update_check, "installed_version", lambda: "0.4.4")

    def write(value: dict) -> None:
        runtime_config.write_protected_json(runtime_config.data_root() / "update-check.json", value)

    return write


def test_notice_names_the_release_and_the_host_command(cache):
    cache({"latest": "0.5.0", "checked_at": time.time()})
    notice = update_check.notice()
    assert "0.5.0" in notice and "0.4.4" in notice
    assert "codex plugin add oh-my-cassette@cassette-editor" in notice


def test_notice_is_empty_when_current_or_older(cache):
    cache({"latest": "0.4.4", "checked_at": time.time()})
    assert update_check.notice() == ""
    cache({"latest": "0.4.3", "checked_at": time.time()})
    assert update_check.notice() == ""


def test_notice_is_empty_without_a_cache(cache):
    assert update_check.notice() == ""


def test_opt_out_silences_notice_and_refresh(cache, monkeypatch):
    cache({"latest": "9.9.9", "checked_at": time.time()})
    monkeypatch.setenv("CASSETTE_UPDATE_CHECK", "0")
    monkeypatch.setattr(update_check, "_fetch_latest", _explode)
    assert update_check.notice() == ""
    update_check.refresh()  # must not fetch


def test_unparsable_versions_never_produce_a_notice(cache):
    cache({"latest": "not-a-version", "checked_at": time.time()})
    assert update_check.notice() == ""
    cache({"latest": "0.5.0-rc1", "checked_at": time.time()})
    assert update_check.notice() == ""


def test_corrupt_cache_is_ignored_rather_than_raising(cache):
    cache({"latest": "0.5.0", "checked_at": time.time()})
    path = runtime_config.data_root() / "update-check.json"
    path.write_text("{ not json", encoding="utf-8")
    assert update_check.notice() == ""


def test_refresh_writes_the_cache_and_then_honours_the_ttl(cache, monkeypatch):
    calls: list[int] = []

    def fetch() -> str:
        calls.append(1)
        return "0.5.0\n"

    monkeypatch.setattr(update_check, "_fetch_latest", fetch)
    update_check.refresh()
    payload = json.loads((runtime_config.data_root() / "update-check.json").read_text(encoding="utf-8"))
    assert payload["latest"] == "0.5.0"
    assert update_check.notice()

    update_check.refresh()  # inside the TTL: no second fetch
    assert len(calls) == 1


def test_refresh_survives_a_failing_fetch(cache, monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_latest", _explode)
    update_check.refresh()
    assert update_check.notice() == ""


def test_every_shipped_host_has_an_update_command():
    # The host names come from the shipped configs; a rename must not silently
    # fall back to the generic "reinstall" wording.
    assert set(update_check.UPDATE_COMMANDS) == {"claude", "codex", "opencode"}
    assert update_check.update_command("claude").startswith("claude plugin marketplace update")
    assert update_check.update_command("hermes") == update_check.FALLBACK_COMMAND
    assert update_check.update_command("") == update_check.FALLBACK_COMMAND


@pytest.fixture
def claude_settings(tmp_path, monkeypatch):
    """Point the runtime at a throwaway ~/.claude and write settings.json into it."""
    root = tmp_path / "claude-home"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    monkeypatch.setenv("CASSETTE_MCP_HOST", "claude")
    monkeypatch.delenv("CASSETTE_UPDATE_CHECK", raising=False)

    def write(value: dict) -> None:
        (root / "settings.json").write_text(json.dumps(value), encoding="utf-8")

    return write


def _marketplaces(entry: dict) -> dict:
    return {"extraKnownMarketplaces": {"cassette-editor": entry}}


def test_auto_update_notice_fires_when_the_toggle_is_off(claude_settings):
    claude_settings(_marketplaces({"source": {"source": "github", "repo": "Cassette-Editor/oh-my-cassette"}}))
    notice = update_check.auto_update_notice()
    assert "AUTO-UPDATE IS OFF" in notice
    assert "cassette-editor" in notice
    # The agent must route the user to the toggle, never write their Claude config itself.
    assert "Enable auto-update" in notice
    assert "do not edit their Claude configuration yourself" in notice


def test_auto_update_notice_is_silent_once_enabled(claude_settings):
    claude_settings(_marketplaces({"source": {"source": "github"}, "autoUpdate": True}))
    assert update_check.auto_update_notice() == ""


def test_auto_update_notice_is_silent_without_the_marketplace_entry(claude_settings):
    # No entry means Claude Code is not the host or the plugin arrived another way, so
    # there is no toggle to describe — and inventing one would send the user nowhere.
    claude_settings({"extraKnownMarketplaces": {"someone-else": {"autoUpdate": False}}})
    assert update_check.auto_update_notice() == ""
    claude_settings({})
    assert update_check.auto_update_notice() == ""


@pytest.mark.parametrize("host", ["codex", "opencode", "hermes", ""])
def test_auto_update_notice_is_claude_only(claude_settings, host, monkeypatch):
    # Every other host lacks the toggle entirely; naming it would be a dead end.
    claude_settings(_marketplaces({"source": {"source": "github"}}))
    monkeypatch.setenv("CASSETTE_MCP_HOST", host)
    assert update_check.auto_update_notice() == ""


def test_auto_update_notice_survives_a_hostile_or_missing_settings_file(claude_settings, monkeypatch, tmp_path):
    claude_settings({"extraKnownMarketplaces": "not-a-dict"})
    assert update_check.auto_update_notice() == ""
    (tmp_path / "claude-home" / "settings.json").write_text("{ not json", encoding="utf-8")
    assert update_check.auto_update_notice() == ""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "absent"))
    assert update_check.auto_update_notice() == ""


def test_auto_update_notice_ignores_a_symlinked_settings_file(claude_settings, tmp_path):
    claude_settings(_marketplaces({"source": {"source": "github"}}))
    real = tmp_path / "claude-home" / "settings.json"
    planted = tmp_path / "planted.json"
    planted.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    real.unlink()
    real.symlink_to(planted)
    assert update_check.auto_update_notice() == ""


def test_opt_out_silences_the_auto_update_notice(claude_settings, monkeypatch):
    claude_settings(_marketplaces({"source": {"source": "github"}}))
    monkeypatch.setenv("CASSETTE_UPDATE_CHECK", "0")
    assert update_check.auto_update_notice() == ""


def _explode() -> str:
    raise OSError("network down")
