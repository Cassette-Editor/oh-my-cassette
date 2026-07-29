from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

import pytest

import runtime_config
from scripts import setup_local_mcp
from scripts import local_mcp_bootstrap


@pytest.fixture
def local_config(tmp_path, monkeypatch):
    config = tmp_path / "config"
    data = tmp_path / "data"
    monkeypatch.setenv("CASSETTE_CONFIG_HOME", str(config))
    monkeypatch.setenv("CASSETTE_DATA_HOME", str(data))
    monkeypatch.setenv("CASSETTE_RUNTIME_ADAPTER", "mcp")
    for name in (
        "CASSETTE_AUTH_EMAIL",
        "CASSETTE_AUTH_ACCOUNT",
        "CASSETTE_EMAIL",
        "CASSETTE_AUTH_PASSWORD",
        "CASSETTE_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    return config, data


def test_protected_config_permissions_and_environment_precedence(local_config, monkeypatch):
    config, _ = local_config
    runtime_config.write_protected_json(
        runtime_config.credentials_path(),
        {"email": "stored@example.test", "password": "stored-secret", "full_api_access": True},
    )
    assert stat.S_IMODE(config.stat().st_mode) == 0o700
    assert stat.S_IMODE(runtime_config.credentials_path().stat().st_mode) == 0o600
    stored = runtime_config.load_credentials()
    assert stored["email"] == "stored@example.test"
    assert stored["source"] == "local_config"
    # A file written by an older version still loads, and its access-level field stays behind:
    # the plugin serves agent accounts and has no second tier to read.
    assert "full_api_access" not in stored

    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "env@example.test")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "env-secret")
    resolved = runtime_config.load_credentials()
    assert resolved == {
        "email": "env@example.test",
        "password": "env-secret",
        "source": "environment",
    }


def test_rejects_overly_permissive_and_symlinked_credential_files(local_config, tmp_path):
    runtime_config.write_protected_json(runtime_config.credentials_path(), {"email": "a", "password": "b"})
    os.chmod(runtime_config.credentials_path(), 0o644)
    with pytest.raises(runtime_config.RuntimeConfigError, match="0600") as too_open:
        runtime_config.load_credentials()
    assert too_open.value.code == "config_permissions_too_open"

    runtime_config.credentials_path().unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text('{"email":"a","password":"b"}', encoding="utf-8")
    runtime_config.credentials_path().symlink_to(target)
    with pytest.raises(runtime_config.RuntimeConfigError) as linked:
        runtime_config.load_credentials()
    assert linked.value.code == "config_symlink"


def test_rejects_symlinked_config_directory_before_writing(local_config, tmp_path):
    config, _ = local_config
    target = tmp_path / "redirected-config"
    target.mkdir()
    config.symlink_to(target, target_is_directory=True)

    with pytest.raises(runtime_config.RuntimeConfigError) as linked:
        runtime_config.write_protected_json(runtime_config.credentials_path(), {"email": "a", "password": "b"})
    assert linked.value.code == "config_symlink"
    assert not (target / "credentials.json").exists()


def test_bootstrap_rejects_symlinked_runtime_marker(tmp_path):
    target = tmp_path / "marker-target.json"
    target.write_text('{"fingerprint":"forged"}', encoding="utf-8")
    marker = tmp_path / ".mcp-runtime.json"
    marker.symlink_to(target)

    with pytest.raises(local_mcp_bootstrap.BootstrapError, match="security check"):
        local_mcp_bootstrap._read_marker(marker)


def test_failed_verification_does_not_write_credentials(local_config, monkeypatch):
    def fail(*_args, **_kwargs):
        raise setup_local_mcp.SetupError("invalid credentials")

    monkeypatch.setattr(setup_local_mcp, "verify_credentials", fail)
    monkeypatch.setattr(setup_local_mcp.getpass, "getpass", lambda _prompt: "wrong")
    args = argparse.Namespace(
        import_hermes=None,
        email="person@example.test",
        use_environment=False,
        api_url="https://example.test",
        allowed_root=[],
    )
    with pytest.raises(setup_local_mcp.SetupError):
        setup_local_mcp.configure(args)
    assert not runtime_config.credentials_path().exists()
    assert not runtime_config.settings_path().exists()


def test_successful_setup_stores_no_access_or_refresh_tokens(local_config, monkeypatch, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(
        setup_local_mcp,
        "verify_credentials",
        lambda *_args, **_kwargs: {"access_token": "tok"},
    )
    monkeypatch.setattr(setup_local_mcp.getpass, "getpass", lambda _prompt: "secret")
    args = argparse.Namespace(
        import_hermes=None,
        email="person@example.test",
        use_environment=False,
        api_url="https://example.test",
        allowed_root=[str(media)],
    )
    setup_local_mcp.configure(args)
    stored = runtime_config.read_protected_json(runtime_config.credentials_path())
    assert stored["email"] == "person@example.test"
    assert stored["password"] == "secret"
    assert "access_token" not in stored and "refresh_token" not in stored
    assert runtime_config.configured_media_roots() == [media.resolve()]


def test_windows_config_roots_and_terminal_commands(tmp_path, monkeypatch):
    monkeypatch.delenv("CASSETTE_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CASSETTE_DATA_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(runtime_config.sys, "platform", "win32")

    assert runtime_config.config_root() == (tmp_path / "Roaming" / "Oh My Cassette").resolve()
    assert runtime_config.data_root() == (tmp_path / "Local" / "Oh My Cassette" / "data").resolve()
    assert runtime_config.python_command() == "python"
    assert runtime_config.setup_command().startswith("python ")


def test_windows_venv_python_uses_scripts_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(local_mcp_bootstrap.sys, "platform", "win32")
    assert local_mcp_bootstrap._venv_python(tmp_path) == tmp_path / "Scripts" / "python.exe"
    monkeypatch.setattr(local_mcp_bootstrap.sys, "platform", "linux")
    assert local_mcp_bootstrap._venv_python(tmp_path) == tmp_path / "bin" / "python"


def test_windows_skips_posix_permission_enforcement(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setenv("CASSETTE_CONFIG_HOME", str(config))
    for name in (
        "CASSETTE_AUTH_EMAIL",
        "CASSETTE_AUTH_ACCOUNT",
        "CASSETTE_EMAIL",
        "CASSETTE_AUTH_PASSWORD",
        "CASSETTE_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime_config.sys, "platform", "win32")
    runtime_config.write_protected_json(runtime_config.credentials_path(), {"email": "a", "password": "b"})
    # Wide-open POSIX bits must not fail the read on Windows, where they are
    # an artifact of the emulated stat rather than a real ACL.
    os.chmod(runtime_config.credentials_path(), 0o666)
    os.chmod(config, 0o777)
    assert runtime_config.load_credentials()["email"] == "a"


def _reset_args(**overrides) -> argparse.Namespace:
    base = dict(
        email=None,
        api_url=None,
        allowed_root=[],
        import_hermes=None,
        use_environment=False,
        reset_password=True,
        no_auto_update=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _seed_credentials(password: str = "stored-secret") -> None:
    runtime_config.write_protected_json(
        runtime_config.credentials_path(),
        {
            "email": "person@example.test",
            "password": password,
            "verified_at": "2026-01-01T00:00:00Z",
            "api_url": "https://stale.test",
        },
    )


def _explode(*_args, **_kwargs):
    raise AssertionError("must not be reached")


def test_reset_password_refuses_environment_sourced_credentials(local_config, monkeypatch):
    # The setup script sees the terminal env; the MCP host has its own. Writing the private
    # config here would leave these variables shadowing the new password, so refuse before
    # anything destructive happens.
    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "person@example.test")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "from-env")
    monkeypatch.setattr(setup_local_mcp, "_post_json", _explode)
    monkeypatch.setattr(setup_local_mcp, "verify_credentials", _explode)

    with pytest.raises(setup_local_mcp.SetupError) as excinfo:
        setup_local_mcp.reset_password(_reset_args())

    assert "CASSETTE_AUTH_PASSWORD" in str(excinfo.value)
    assert not runtime_config.credentials_path().exists()


def test_reset_password_rotates_in_place_without_prompting(local_config, monkeypatch):
    _seed_credentials("still-works")
    monkeypatch.setattr(
        setup_local_mcp,
        "verify_credentials",
        lambda *_a, **_k: {"access_token": "tok-123"},
    )
    calls = []

    def post(api_url, path, payload, *, token=None, **_kwargs):
        calls.append((path, token))
        return 200, {"password": "  rotated-secret  ", "email": "person@example.test"}, None

    monkeypatch.setattr(setup_local_mcp, "_post_json", post)
    monkeypatch.setattr(setup_local_mcp.getpass, "getpass", _explode)

    result = setup_local_mcp.reset_password(_reset_args())

    assert result == "rotated"
    assert calls == [("/api/agent-auth/rotate-password", "tok-123")]
    stored = runtime_config.read_protected_json(runtime_config.credentials_path())
    assert stored["password"] == "rotated-secret"


def test_reset_password_falls_back_to_email_when_the_stored_password_is_dead(local_config, monkeypatch):
    _seed_credentials("already-rotated-elsewhere")

    def verify(_api_url, _email, password, **_kwargs):
        if password == "already-rotated-elsewhere":
            raise setup_local_mcp.CredentialsRejected("nope")
        return {"access_token": "tok-456"}

    monkeypatch.setattr(setup_local_mcp, "verify_credentials", verify)
    paths = []

    def post(_api_url, path, _payload, **_kwargs):
        paths.append(path)
        return 200, {"sent": True}, None

    monkeypatch.setattr(setup_local_mcp, "_post_json", post)
    monkeypatch.setattr(setup_local_mcp.getpass, "getpass", lambda _prompt: "  emailed-secret  ")

    result = setup_local_mcp.reset_password(_reset_args())

    assert result == "emailed"
    # The rotate route needs a session the dead password cannot mint, so it is never tried.
    assert paths == ["/api/agent-auth/request-code"]
    stored = runtime_config.read_protected_json(runtime_config.credentials_path())
    assert stored["password"] == "emailed-secret"


def test_reset_password_does_not_fall_back_when_the_api_is_unreachable(local_config, monkeypatch):
    # A transport failure must not be read as "password is dead" — that would replace a
    # perfectly good account password because the network blipped.
    _seed_credentials("still-works")

    def verify(*_a, **_k):
        raise setup_local_mcp.SetupError("Could not reach the Cassette API (URLError).")

    monkeypatch.setattr(setup_local_mcp, "verify_credentials", verify)
    monkeypatch.setattr(setup_local_mcp, "_post_json", _explode)
    monkeypatch.setattr(setup_local_mcp.getpass, "getpass", _explode)

    with pytest.raises(setup_local_mcp.SetupError):
        setup_local_mcp.reset_password(_reset_args())

    assert runtime_config.load_credentials()["password"] == "still-works"


def test_reset_password_stops_when_the_email_is_not_allowlisted(local_config, monkeypatch):
    _seed_credentials("")
    monkeypatch.setattr(setup_local_mcp, "verify_credentials", _explode)
    # The server answers 200 with sent=false so it never confirms who holds an account.
    monkeypatch.setattr(
        setup_local_mcp, "_post_json", lambda *_a, **_k: (200, {"sent": False, "reason": "not_allowed"}, None)
    )
    monkeypatch.setattr(setup_local_mcp.getpass, "getpass", _explode)

    with pytest.raises(setup_local_mcp.SetupError, match="not authorised"):
        setup_local_mcp.reset_password(_reset_args())


def test_reset_password_stores_no_session_tokens(local_config, monkeypatch):
    _seed_credentials("still-works")
    monkeypatch.setattr(
        setup_local_mcp,
        "verify_credentials",
        lambda *_a, **_k: {"access_token": "tok-123"},
    )
    monkeypatch.setattr(setup_local_mcp, "_post_json", lambda *_a, **_k: (200, {"password": "rotated-secret"}, None))

    setup_local_mcp.reset_password(_reset_args())

    stored = runtime_config.read_protected_json(runtime_config.credentials_path())
    # An exact key set, not two `not in` checks: this also fails if some future field starts
    # smuggling a secret onto disk.
    assert set(stored) == {"email", "password", "verified_at", "api_url"}


def test_reset_password_uses_the_settings_api_url_not_the_stored_copy(local_config, monkeypatch):
    _seed_credentials("still-works")  # carries api_url https://stale.test
    runtime_config.write_protected_json(
        runtime_config.settings_path(), {"media_roots": ["/srv/footage"], "api_url": "https://settings.test"}
    )
    seen = {}

    def verify(api_url, *_a, **_k):
        seen["api_url"] = api_url
        return {"access_token": "tok-123"}

    monkeypatch.setattr(setup_local_mcp, "verify_credentials", verify)
    monkeypatch.setattr(setup_local_mcp, "_post_json", lambda *_a, **_k: (200, {"password": "rotated-secret"}, None))

    setup_local_mcp.reset_password(_reset_args())

    assert seen["api_url"] == "https://settings.test"
    # A reset replaces a password; it must not rewrite settings it was not asked about.
    assert runtime_config.load_settings()["media_roots"] == ["/srv/footage"]


def _claude_settings(tmp_path, monkeypatch, payload: dict) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    path = setup_local_mcp.claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _installed_marketplace() -> dict:
    return {"source": {"source": "github", "repo": "Cassette-Editor/oh-my-cassette"}}


def test_claude_auto_update_opt_in_edits_only_the_marketplace_entry(tmp_path, monkeypatch):
    path = _claude_settings(
        tmp_path,
        monkeypatch,
        {
            "theme": "dark",
            "extraKnownMarketplaces": {
                "other": _installed_marketplace(),
                "cassette-editor": _installed_marketplace(),
            },
        },
    )

    message = setup_local_mcp.enable_claude_auto_update(skip=False, assume_yes=True)

    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["extraKnownMarketplaces"]["cassette-editor"]["autoUpdate"] is True
    assert settings["extraKnownMarketplaces"]["cassette-editor"]["source"] == _installed_marketplace()["source"]
    # Every unrelated setting survives the read-modify-write.
    assert settings["theme"] == "dark"
    assert settings["extraKnownMarketplaces"]["other"] == _installed_marketplace()
    assert "cassette-editor" in message


def test_claude_auto_update_opt_in_skips_when_the_marketplace_is_absent(tmp_path, monkeypatch):
    # No cassette-editor entry means Claude Code is not the host, or the plugin came from
    # somewhere else; the setup script must never create the marketplace itself.
    path = _claude_settings(tmp_path, monkeypatch, {"extraKnownMarketplaces": {"other": _installed_marketplace()}})
    before = path.read_text(encoding="utf-8")

    assert setup_local_mcp.enable_claude_auto_update(skip=False, assume_yes=True) == ""
    assert path.read_text(encoding="utf-8") == before


def test_claude_auto_update_opt_in_is_a_no_op_without_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    assert setup_local_mcp.enable_claude_auto_update(skip=False, assume_yes=True) == ""
    assert not setup_local_mcp.claude_settings_path().exists()


def test_claude_auto_update_opt_out_leaves_settings_untouched(tmp_path, monkeypatch):
    path = _claude_settings(
        tmp_path, monkeypatch, {"extraKnownMarketplaces": {"cassette-editor": _installed_marketplace()}}
    )
    before = path.read_text(encoding="utf-8")

    message = setup_local_mcp.enable_claude_auto_update(skip=True, assume_yes=True)

    assert "--no-auto-update" in message
    assert path.read_text(encoding="utf-8") == before


def test_reset_password_rejects_the_auto_update_flag(local_config):
    args = _reset_args()
    args.no_auto_update = True
    with pytest.raises(setup_local_mcp.SetupError):
        setup_local_mcp._reject_reset_password_conflicts(args)
