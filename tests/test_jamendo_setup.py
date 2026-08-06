from __future__ import annotations

import json
import stat

import runtime_config
from cassette.core import tools


def _mcp_config(tmp_path, monkeypatch, *, host: str = "codex") -> None:
    monkeypatch.setenv("CASSETTE_RUNTIME_ADAPTER", "mcp")
    monkeypatch.setenv("CASSETTE_MCP_HOST", host)
    monkeypatch.setenv("CASSETTE_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("JAMENDO_CLIENT_ID", raising=False)


def test_jamendo_setup_validates_before_private_storage_and_masks_output(tmp_path, monkeypatch):
    _mcp_config(tmp_path, monkeypatch)
    client_id = "private-client-id-value"
    observed = []
    monkeypatch.setattr(
        runtime_config,
        "validate_jamendo_client_id",
        lambda value: observed.append(value) or {"status": "success"},
    )

    payload = json.loads(tools.cassette_jamendo_setup({"client_id": client_id}, runtime_host="mcp"))

    assert payload["ok"] is True
    assert observed == [client_id]
    assert payload["data"]["credential_source"] == "local_config"
    assert client_id not in json.dumps(payload)
    assert runtime_config.stored_jamendo()["client_id"] == client_id
    if hasattr(stat, "S_IMODE") and runtime_config.settings_path().exists():
        assert stat.S_IMODE(runtime_config.settings_path().stat().st_mode) & 0o077 == 0


def test_jamendo_setup_failure_preserves_existing_configuration(tmp_path, monkeypatch):
    _mcp_config(tmp_path, monkeypatch)
    runtime_config.store_jamendo_client_id("working-client", verified_at="2026-08-06T00:00:00Z")

    def reject(_value):
        raise runtime_config.JamendoValidationError("jamendo_client_id_invalid", "Jamendo rejected that Client ID.")

    monkeypatch.setattr(runtime_config, "validate_jamendo_client_id", reject)
    payload = json.loads(tools.cassette_jamendo_setup({"client_id": "bad-client"}, runtime_host="mcp"))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "jamendo_client_id_invalid"
    assert payload["error"]["details"]["preserved_existing"] is True
    assert runtime_config.stored_jamendo()["client_id"] == "working-client"
    assert "bad-client" not in json.dumps(payload)


def test_jamendo_setup_refuses_shadowed_environment_value(tmp_path, monkeypatch):
    _mcp_config(tmp_path, monkeypatch)
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "environment-client")
    monkeypatch.setattr(
        runtime_config,
        "validate_jamendo_client_id",
        lambda _value: (_ for _ in ()).throw(AssertionError("shadowed setup must not validate")),
    )

    payload = json.loads(tools.cassette_jamendo_setup({"client_id": "new-client"}, runtime_host="mcp"))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "jamendo_env_precedence"
    assert not runtime_config.settings_path().exists()
    assert "environment-client" not in json.dumps(payload)
    assert "new-client" not in json.dumps(payload)


def test_hermes_chat_setup_updates_only_client_id_in_dotenv(tmp_path, monkeypatch):
    _mcp_config(tmp_path, monkeypatch, host="hermes")
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    env_path = hermes_home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "OTHER_VALUE=keep\nJAMENDO_CLIENT_ID=old-client\nJAMENDO_CLIENT_SECRET=legacy-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "validate_jamendo_client_id", lambda _value: {"status": "success"})

    payload = json.loads(tools.cassette_jamendo_setup({"client_id": "new-client"}, runtime_host="mcp"))

    text = env_path.read_text(encoding="utf-8")
    assert payload["ok"] is True
    assert payload["data"]["credential_source"] == "hermes_env"
    assert "OTHER_VALUE=keep" in text
    assert "JAMENDO_CLIENT_ID=new-client" in text
    assert "JAMENDO_CLIENT_SECRET=legacy-secret" in text
    assert runtime_config.resolve_jamendo_client_id()["client_id"] == "new-client"
