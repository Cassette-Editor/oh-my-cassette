from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_install_script():
    spec = importlib.util.spec_from_file_location("install_plugin", ROOT / "scripts" / "install_plugin.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_install_script_dry_run_uses_hermes_home(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "install_plugin.py"),
            "--hermes-home",
            str(tmp_path / ".hermes"),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert str(tmp_path / ".hermes" / "plugins" / "cassette") in result.stdout


def test_install_script_configures_cassette_auth_interactively(tmp_path):
    install_plugin = _load_install_script()
    answers = iter(["", "operator@example.com"])

    def fake_input(prompt):
        return next(answers)

    configured = install_plugin.configure_cassette_auth(
        tmp_path / ".hermes",
        input_func=fake_input,
        password_func=lambda prompt: "generated-password-1234",
        interactive=True,
    )

    env_path = tmp_path / ".hermes" / ".env"
    assert configured is True
    assert "CASSETTE_AUTH_EMAIL=operator@example.com" in env_path.read_text(encoding="utf-8")
    assert "CASSETTE_AUTH_PASSWORD=generated-password-1234" in env_path.read_text(encoding="utf-8")


def test_install_script_configures_cassette_url_default_asia(tmp_path):
    install_plugin = _load_install_script()

    configured = install_plugin.configure_cassette_url(
        tmp_path / ".hermes",
        input_func=lambda prompt: "",
        interactive=True,
    )

    env_path = tmp_path / ".hermes" / ".env"
    assert configured is True
    assert "CASSETTE_URL=https://sg.trycassette.online/agent" in env_path.read_text(encoding="utf-8")


def test_install_script_configures_cassette_url_america(tmp_path):
    install_plugin = _load_install_script()

    configured = install_plugin.configure_cassette_url(
        tmp_path / ".hermes",
        input_func=lambda prompt: "2",
        interactive=True,
    )

    env_path = tmp_path / ".hermes" / ".env"
    assert configured is True
    assert "CASSETTE_URL=https://trycassette.online/agent" in env_path.read_text(encoding="utf-8")


def test_install_script_keeps_existing_cassette_url_on_blank_choice(tmp_path):
    install_plugin = _load_install_script()
    env_path = tmp_path / ".hermes" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("OTHER_VALUE=keep\nCASSETTE_URL=https://trycassette.online/agent\n", encoding="utf-8")

    configured = install_plugin.configure_cassette_url(
        tmp_path / ".hermes",
        input_func=lambda prompt: "",
        interactive=True,
    )

    text = env_path.read_text(encoding="utf-8")
    assert configured is True
    assert "OTHER_VALUE=keep" in text
    assert "CASSETTE_URL=https://trycassette.online/agent" in text
    assert "https://sg.trycassette.online/agent" not in text


def test_install_script_updates_existing_cassette_auth_without_clobbering(tmp_path):
    install_plugin = _load_install_script()
    env_path = tmp_path / ".hermes" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "OTHER_VALUE=keep\n"
        "CASSETTE_AUTH_EMAIL=old@example.com\n"
        "export CASSETTE_AUTH_PASSWORD=old-password\n",
        encoding="utf-8",
    )
    answers = iter(["y", "new@example.com"])

    configured = install_plugin.configure_cassette_auth(
        tmp_path / ".hermes",
        input_func=lambda prompt: next(answers),
        password_func=lambda prompt: "new-password",
        interactive=True,
    )

    text = env_path.read_text(encoding="utf-8")
    assert configured is True
    assert "OTHER_VALUE=keep" in text
    assert "CASSETTE_AUTH_EMAIL=new@example.com" in text
    assert "export CASSETTE_AUTH_PASSWORD=new-password" in text
    assert "old@example.com" not in text


def test_install_script_configures_jamendo_auth_interactively(tmp_path):
    install_plugin = _load_install_script()
    answers = iter(["y", "client-id-placeholder"])

    configured = install_plugin.configure_jamendo_auth(
        tmp_path / ".hermes",
        input_func=lambda prompt: next(answers),
        password_func=lambda prompt: "client-secret-placeholder",
        interactive=True,
    )

    env_path = tmp_path / ".hermes" / ".env"
    text = env_path.read_text(encoding="utf-8")
    assert configured is True
    assert "JAMENDO_CLIENT_ID=client-id-placeholder" in text
    assert "JAMENDO_CLIENT_SECRET=client-secret-placeholder" in text


def test_install_script_updates_existing_jamendo_auth_without_clobbering(tmp_path):
    install_plugin = _load_install_script()
    env_path = tmp_path / ".hermes" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "OTHER_VALUE=keep\n"
        "JAMENDO_CLIENT_ID=old-client\n"
        "export JAMENDO_CLIENT_SECRET=old-secret\n",
        encoding="utf-8",
    )
    answers = iter(["y", "new-client"])

    configured = install_plugin.configure_jamendo_auth(
        tmp_path / ".hermes",
        input_func=lambda prompt: next(answers),
        password_func=lambda prompt: "new-secret",
        interactive=True,
    )

    text = env_path.read_text(encoding="utf-8")
    assert configured is True
    assert "OTHER_VALUE=keep" in text
    assert "JAMENDO_CLIENT_ID=new-client" in text
    assert "export JAMENDO_CLIENT_SECRET=new-secret" in text
    assert "old-client" not in text


def test_install_script_detects_and_saves_transcoder_paths(tmp_path, monkeypatch):
    install_plugin = _load_install_script()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ffmpeg = bin_dir / "ffmpeg"
    ffprobe = bin_dir / "ffprobe"
    ffmpeg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ffprobe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ffmpeg.chmod(0o755)
    ffprobe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    configured = install_plugin.configure_transcoder_paths(tmp_path / ".hermes")

    text = (tmp_path / ".hermes" / ".env").read_text(encoding="utf-8")
    assert configured is True
    assert f"CASSETTE_FFMPEG_BIN={ffmpeg}" in text
    assert f"CASSETTE_FFPROBE_BIN={ffprobe}" in text


def test_install_script_installs_playwright_in_hermes_venv(tmp_path, monkeypatch):
    install_plugin = _load_install_script()
    python = tmp_path / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    observed = []

    def fake_run(cmd, *, dry_run=False):
        observed.append((cmd, dry_run))
        return 0

    monkeypatch.setattr(install_plugin, "_run_command", fake_run)

    assert install_plugin.install_hermes_playwright(tmp_path / ".hermes") is True
    assert observed == [
        ([str(python), "-m", "pip", "--version"], False),
        ([str(python), "-m", "pip", "install", "playwright"], False),
        ([str(python), "-m", "playwright", "install", "chromium"], False),
    ]


def test_install_script_bootstraps_missing_pip_before_playwright(tmp_path, monkeypatch):
    install_plugin = _load_install_script()
    python = tmp_path / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    observed = []

    def fake_run(cmd, *, dry_run=False):
        observed.append((cmd, dry_run))
        if cmd == [str(python), "-m", "pip", "--version"]:
            return 1
        return 0

    monkeypatch.setattr(install_plugin, "_run_command", fake_run)

    assert install_plugin.install_hermes_playwright(tmp_path / ".hermes") is True
    assert observed == [
        ([str(python), "-m", "pip", "--version"], False),
        ([str(python), "-m", "ensurepip", "--upgrade"], False),
        ([str(python), "-m", "pip", "install", "playwright"], False),
        ([str(python), "-m", "playwright", "install", "chromium"], False),
    ]


def test_install_script_restart_gateway_uses_hermes_cli(tmp_path, monkeypatch):
    install_plugin = _load_install_script()
    python = tmp_path / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    observed = {}

    class Proc:
        returncode = 0

    def fake_run(cmd, check=False, env=None):
        observed["cmd"] = cmd
        observed["check"] = check
        observed["env"] = env
        return Proc()

    monkeypatch.setattr(install_plugin.subprocess, "run", fake_run)

    assert install_plugin.restart_gateway(tmp_path / ".hermes") is True
    assert observed["cmd"] == [str(python), "-m", "hermes_cli.main", "gateway", "restart"]
    assert observed["check"] is False
    assert observed["env"]["HERMES_ACCEPT_HOOKS"] == "1"


def test_install_script_enables_cassette_plugin_interactively(tmp_path, monkeypatch):
    install_plugin = _load_install_script()
    python = tmp_path / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    observed = {}

    class Proc:
        returncode = 0

    def fake_run(cmd, check=False, env=None):
        observed["cmd"] = cmd
        observed["check"] = check
        observed["env"] = env
        return Proc()

    monkeypatch.setattr(install_plugin.subprocess, "run", fake_run)

    assert install_plugin.enable_cassette_plugin(
        tmp_path / ".hermes",
        input_func=lambda prompt: "",
        interactive=True,
    ) is True
    assert observed["cmd"] == [str(python), "-m", "hermes_cli.main", "plugins", "enable", "cassette"]
    assert observed["check"] is False
    assert observed["env"]["HERMES_ACCEPT_HOOKS"] == "1"


def test_install_script_can_skip_cassette_plugin_enable(tmp_path):
    install_plugin = _load_install_script()

    assert install_plugin.enable_cassette_plugin(
        tmp_path / ".hermes",
        input_func=lambda prompt: "n",
        interactive=True,
    ) is False
