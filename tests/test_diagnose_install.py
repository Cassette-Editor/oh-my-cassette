from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_diagnose_script():
    spec = importlib.util.spec_from_file_location("diagnose_install", ROOT / "scripts" / "diagnose_install.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_diagnose_redacts_secret_env_values():
    diagnose = _load_diagnose_script()

    snapshot = diagnose._redacted_env_snapshot({
        "CASSETTE_AUTH_EMAIL": "operator@example.com",
        "CASSETTE_AUTH_PASSWORD": "secret-password",
        "JAMENDO_CLIENT_ID": "client-id",
        "JAMENDO_CLIENT_SECRET": "secret-client",
    })

    assert snapshot["CASSETTE_AUTH_EMAIL"] == "<set>"
    assert snapshot["CASSETTE_AUTH_PASSWORD"] == "<set>"
    assert snapshot["JAMENDO_CLIENT_ID"] == "<set>"
    assert snapshot["JAMENDO_CLIENT_SECRET"] == "<set>"
    assert "operator@example.com" not in str(snapshot)
    assert "secret-password" not in str(snapshot)
    assert "secret-client" not in str(snapshot)


def test_diagnose_sanitizes_command_output():
    diagnose = _load_diagnose_script()

    text = diagnose._sanitize_text("user@example.com wxid_secret token=abc 1904003326")

    assert "user@example.com" not in text
    assert "wxid_secret" not in text
    assert "abc" not in text
    assert "1904003326" not in text


def test_diagnose_plugin_reports_missing_install(tmp_path):
    diagnose = _load_diagnose_script()

    result = diagnose._check_plugin(tmp_path / ".hermes", ROOT)

    assert result["status"] == "fail"
    assert result["name"] == "plugin"


def test_diagnose_plugin_accepts_dir_that_is_this_checkout(tmp_path):
    diagnose = _load_diagnose_script()
    plugin_dir = tmp_path / ".hermes" / "plugins" / "cassette"
    plugin_dir.mkdir(parents=True)

    result = diagnose._check_plugin(tmp_path / ".hermes", plugin_dir)

    assert result["status"] == "ok"
    assert "this checkout" in result["message"]


def test_diagnose_plugin_recognizes_cli_managed_git_clone(tmp_path, monkeypatch):
    diagnose = _load_diagnose_script()
    plugin_dir = tmp_path / ".hermes" / "plugins" / "cassette"
    (plugin_dir / ".git").mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: cassette\nversion: 0.1.0\n", encoding="utf-8")
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "plugin.yaml").write_text("name: cassette\nversion: 0.1.0\n", encoding="utf-8")
    monkeypatch.setattr(diagnose, "_run", lambda cmd, timeout=20: (0, "https://github.com/Cassette-Editor/oh-my-cassette.git"))

    result = diagnose._check_plugin(tmp_path / ".hermes", repo)

    assert result["status"] == "ok"
    assert "hermes plugins update cassette" in result["message"]


def test_diagnose_plugin_warns_on_version_drift_in_git_clone(tmp_path, monkeypatch):
    diagnose = _load_diagnose_script()
    plugin_dir = tmp_path / ".hermes" / "plugins" / "cassette"
    (plugin_dir / ".git").mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: cassette\nversion: 0.1.0\n", encoding="utf-8")
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "plugin.yaml").write_text("name: cassette\nversion: 0.2.0 # x-release-please-version\n", encoding="utf-8")
    # Pre-org-transfer installs have the old-owner remote; they must stay recognized.
    monkeypatch.setattr(diagnose, "_run", lambda cmd, timeout=20: (0, "https://github.com/epc314/oh-my-cassette.git"))

    result = diagnose._check_plugin(tmp_path / ".hermes", repo)

    assert result["status"] == "warn"
    assert "0.1.0" in result["message"] and "0.2.0" in result["message"]


def test_diagnose_plugin_warns_on_foreign_clone_and_unknown_dir(tmp_path, monkeypatch):
    diagnose = _load_diagnose_script()
    plugin_dir = tmp_path / ".hermes" / "plugins" / "cassette"
    (plugin_dir / ".git").mkdir(parents=True)
    repo = tmp_path / "checkout"
    repo.mkdir()
    monkeypatch.setattr(diagnose, "_run", lambda cmd, timeout=20: (0, "https://github.com/someone/other-plugin.git"))

    foreign = diagnose._check_plugin(tmp_path / ".hermes", repo)
    assert foreign["status"] == "warn"
    assert "different repository" in foreign["message"]

    plain = tmp_path / "plain-home"
    (plain / "plugins" / "cassette").mkdir(parents=True)
    unknown = diagnose._check_plugin(plain, repo)
    assert unknown["status"] == "warn"
    assert "neither a symlink nor a git clone" in unknown["message"]


def test_diagnose_detects_enabled_plugin_from_hermes_list(tmp_path, monkeypatch):
    diagnose = _load_diagnose_script()
    python = tmp_path / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    class Proc:
        returncode = 0
        stdout = "│ cassette │ enabled │ 0.1.0 │ Cassette video-editing automation │ git │"

    def fake_run(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(diagnose.subprocess, "run", fake_run)

    result = diagnose._check_plugin_enabled(tmp_path / ".hermes")

    assert result["status"] == "ok"
    assert result["name"] == "plugin_enabled"


def test_diagnose_warns_when_cassette_login_credentials_missing(tmp_path):
    diagnose = _load_diagnose_script()

    result = diagnose._check_cassette_login(tmp_path / ".hermes", "https://example.test/agent", "", "")

    assert result["status"] == "warn"
    assert result["name"] == "cassette_login"


def test_diagnose_login_check_redacts_subprocess_output(tmp_path, monkeypatch):
    diagnose = _load_diagnose_script()
    python = tmp_path / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    class Proc:
        returncode = 0
        stdout = '{"status":"fail","code":"cassette_auth_failed","message":"operator@example.com password=secret"}'

    def fake_run(*args, **kwargs):
        assert kwargs["env"]["CASSETTE_DIAG_EMAIL"] == "operator@example.com"
        assert kwargs["env"]["CASSETTE_DIAG_PASSWORD"] == "secret"
        return Proc()

    monkeypatch.setattr(diagnose.subprocess, "run", fake_run)

    result = diagnose._check_cassette_login(
        tmp_path / ".hermes",
        "https://example.test/agent",
        "operator@example.com",
        "secret",
    )

    assert result["status"] == "fail"
    assert "operator@example.com" not in str(result)
    assert "secret" not in str(result)


def test_diagnose_warns_when_auth_form_stays_visible_after_page_submit(tmp_path, monkeypatch):
    diagnose = _load_diagnose_script()
    python = tmp_path / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    class Proc:
        returncode = 0
        stdout = '{"status":"fail","code":"cassette_auth_form_still_visible","auth_selectors":["#agent-auth-password"]}'

    monkeypatch.setattr(diagnose.subprocess, "run", lambda *args, **kwargs: Proc())

    result = diagnose._check_cassette_login(
        tmp_path / ".hermes",
        "https://example.test/agent",
        "operator@example.com",
        "secret",
    )

    assert result["status"] == "warn"
    assert result["details"]["code"] == "cassette_auth_form_still_visible"
