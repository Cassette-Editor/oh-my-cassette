"""Credential resolution.

0.5.0 keeps this deliberately small: a pre-issued bearer token (``CASSETTE_AUTH_TOKEN``) wins; an
email/password pair is exchanged once through ``POST /api/agent-auth/verify`` and the resulting
access token is cached on disk (0600) until it expires; with neither, requests go out unauthenticated,
which is what the local Cassette-Editor stack accepts when ``AGENT_AUTH_ENABLED=false``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from oh_my_cassette.cassette.errors import CassetteError, HttpError, TransportError
from oh_my_cassette.settings import Settings


class TokenSource:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache_path = settings.home / "credentials.json"
        self._cached: tuple[str, float] | None = None

    async def __call__(self) -> str | None:
        settings = self._settings
        if settings.auth_token:
            return settings.auth_token
        if not (settings.email and settings.password):
            return None
        cached = self._read_cache()
        if cached:
            return cached
        return await self._verify()

    def _read_cache(self) -> str | None:
        if self._cached and self._cached[1] > time.time() + 60:
            return self._cached[0]
        try:
            raw = json.loads(self._cache_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if raw.get("email") != self._settings.email:
            return None
        token, expires = raw.get("access_token"), float(raw.get("expires_at", 0))
        if isinstance(token, str) and token and expires > time.time() + 60:
            self._cached = (token, expires)
            return token
        return None

    def _write_cache(self, token: str, expires_at: float) -> None:
        self._settings.home.mkdir(parents=True, exist_ok=True)
        payload = {"email": self._settings.email, "access_token": token, "expires_at": expires_at}
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._cache_path)
        self._cached = (token, expires_at)

    async def _verify(self) -> str:
        url = f"{self._settings.api_url}/api/agent-auth/verify"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    url, json={"email": self._settings.email, "password": self._settings.password}
                )
        except httpx.HTTPError as exc:
            raise TransportError(f"login failed: {exc}") from exc
        if response.status_code != 200:
            raise HttpError(
                response.status_code, "login_failed", response.text[:300] or "credential verification failed"
            )
        body = response.json()
        session = body.get("session") or {}
        token = session.get("access_token")
        if not isinstance(token, str) or not token:
            raise CassetteError("login_failed", "verify response carried no access token")
        expires_at = time.time() + float(session.get("expires_in") or 3600)
        self._write_cache(token, expires_at)
        return token


def credentials_path(settings: Settings) -> Path:
    return settings.home / "credentials.json"
