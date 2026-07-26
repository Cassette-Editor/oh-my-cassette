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


def _explode() -> str:
    raise OSError("network down")
