"""The account's own OpenAI key, for an account that pays for its Cassette agent turns with it.

The key stays in the operating-system keyring, one per saved connection, beside the grant. The bridge
sends it in `x-cassette-provider-key` on its requests to the Cassette MCP service, which hands it to the
Cassette API; the API keeps it in memory only while the account's turns need it, and the next request
hands it over again once the API has let it go.
"""

from __future__ import annotations

import hashlib
import re

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

SERVICE = "oh-my-cassette.provider-key"
HEADER = "x-cassette-provider-key"

# An OpenAI secret key as the OpenAI dashboard issues it: project, service-account and legacy user keys alike.
_KEY_SHAPE = re.compile(r"sk-[A-Za-z0-9_-]+")


def valid_key(key: str) -> bool:
    return 20 <= len(key) <= 512 and _KEY_SHAPE.fullmatch(key) is not None


def fingerprint(key: str) -> str:
    """How Cassette shows a key without keeping it: the first 8 hex digits of its SHA-256 and its last four characters."""
    return f"{hashlib.sha256(key.encode()).hexdigest()[:8]}…{key[-4:]}"


def read_key(store: KeyringBackend, account: str) -> str | None:
    return store.get_password(SERVICE, account) or None


def save_key(store: KeyringBackend, account: str, key: str) -> None:
    store.set_password(SERVICE, account, key)


def remove_key(store: KeyringBackend, account: str) -> bool:
    """Forgets the account's key; False when none was saved."""
    if read_key(store, account) is None:
        return False
    try:
        store.delete_password(SERVICE, account)
    except PasswordDeleteError:
        return False
    return True
