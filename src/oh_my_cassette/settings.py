"""Process configuration. Everything comes from environment variables so every host configures the
server the same way (Claude `.mcp.json` env, Codex `env`, OpenCode `environment`, Hermes `env`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_API_URL = "http://127.0.0.1:8787"
DEFAULT_WEB_URL = "http://127.0.0.1:8080"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    api_url: str
    web_url: str
    home: Path
    auth_token: str | None = None
    email: str | None = None
    password: str | None = None
    locale: str = "en"
    model_id: str | None = None
    reasoning_effort: str | None = None
    run_timeout_sec: int = 3300
    import_ready_timeout_sec: int = 900
    export_timeout_sec: int = 1800
    http_timeout_sec: int = 60
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def anonymous(self) -> bool:
        """True when no credential is configured: projects are demo `try-session-*` ids."""
        return not (self.auth_token or (self.email and self.password))

    @classmethod
    def from_env(cls) -> Settings:
        api_url = (_env("CASSETTE_API_URL", DEFAULT_API_URL) or DEFAULT_API_URL).rstrip("/")
        web_url = _env("CASSETTE_WEB_URL")
        if web_url is None:
            web_url = DEFAULT_WEB_URL if api_url == DEFAULT_API_URL else api_url
        home = Path(_env("OH_MY_CASSETTE_HOME") or Path.home() / ".oh-my-cassette").expanduser()
        return cls(
            api_url=api_url,
            web_url=web_url.rstrip("/"),
            home=home,
            auth_token=_env("CASSETTE_AUTH_TOKEN"),
            email=_env("CASSETTE_EMAIL"),
            password=_env("CASSETTE_PASSWORD"),
            locale=_env("CASSETTE_LOCALE", "en") or "en",
            model_id=_env("CASSETTE_MODEL"),
            reasoning_effort=_env("CASSETTE_REASONING_EFFORT"),
            run_timeout_sec=_env_int("CASSETTE_RUN_TIMEOUT_SEC", 3300),
            import_ready_timeout_sec=_env_int("CASSETTE_IMPORT_READY_TIMEOUT_SEC", 900),
            export_timeout_sec=_env_int("CASSETTE_EXPORT_TIMEOUT_SEC", 1800),
            http_timeout_sec=_env_int("CASSETTE_HTTP_TIMEOUT_SEC", 60),
            ffmpeg=_env("CASSETTE_FFMPEG", "ffmpeg") or "ffmpeg",
            ffprobe=_env("CASSETTE_FFPROBE", "ffprobe") or "ffprobe",
        )
