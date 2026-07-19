from __future__ import annotations

import argparse
import os
import stat

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

    monkeypatch.setenv("CASSETTE_AUTH_EMAIL", "env@example.test")
    monkeypatch.setenv("CASSETTE_AUTH_PASSWORD", "env-secret")
    resolved = runtime_config.load_credentials()
    assert resolved == {
        "email": "env@example.test",
        "password": "env-secret",
        "source": "environment",
        "full_api_access": None,
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
        with_browser=False,
        transport="api",
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
        lambda *_args, **_kwargs: {"full_api_access": True},
    )
    monkeypatch.setattr(setup_local_mcp.getpass, "getpass", lambda _prompt: "secret")
    args = argparse.Namespace(
        import_hermes=None,
        email="person@example.test",
        use_environment=False,
        api_url="https://example.test",
        allowed_root=[str(media)],
        with_browser=False,
        transport="api",
    )
    result = setup_local_mcp.configure(args)
    stored = runtime_config.read_protected_json(runtime_config.credentials_path())
    assert result["full_api_access"] is True
    assert stored["email"] == "person@example.test"
    assert stored["password"] == "secret"
    assert "access_token" not in stored and "refresh_token" not in stored
    assert runtime_config.configured_media_roots() == [media.resolve()]
