"""Database key handling.

The database is encrypted with SQLCipher using a random 256-bit *raw* key
(64 hex characters). A raw key skips SQLCipher's password-based key
derivation, which is what you want for a machine-generated key.

Where the key comes from, in order:

1. The ``WRH_DB_KEY`` environment variable (64 hex chars). Handy for tests,
   CI, or a non-macOS server. Do not put it in a file inside the repo.
2. The OS keyring via the ``keyring`` library: the macOS Keychain on the M1
   server (service "wrh", account "db-key"); Windows Credential Manager or
   Secret Service elsewhere.

If you lose the key, the data is gone. ``wrh key init`` prints a copy for
your password manager; keep it there.
"""

from __future__ import annotations

import os
import re
import secrets

SERVICE = "wrh"
ACCOUNT = "db-key"
ENV_VAR = "WRH_DB_KEY"

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class KeyError_(RuntimeError):
    """Raised when no usable key is available."""


def validate(key: str) -> str:
    key = key.strip()
    if not _HEX64.match(key):
        raise KeyError_("Database key must be exactly 64 hex characters (256 bits).")
    return key.lower()


def generate() -> str:
    return secrets.token_hex(32)


def _keyring():
    import keyring  # imported lazily so collectors never need it

    return keyring


def get_key() -> str:
    env = os.environ.get(ENV_VAR)
    if env:
        return validate(env)
    try:
        stored = _keyring().get_password(SERVICE, ACCOUNT)
    except Exception as e:  # no keyring backend available
        raise KeyError_(
            f"Could not read the key from the OS keyring ({e}). "
            f"Set {ENV_VAR} or run `wrh key init`."
        ) from e
    if not stored:
        raise KeyError_(f"No database key found. Run `wrh key init`, or set {ENV_VAR}.")
    return validate(stored)


def store_key(key: str) -> None:
    _keyring().set_password(SERVICE, ACCOUNT, validate(key))


def has_stored_key() -> bool:
    try:
        return bool(_keyring().get_password(SERVICE, ACCOUNT))
    except Exception:
        return False


def pragma(key: str) -> str:
    """The PRAGMA statement that unlocks a database with a raw key."""
    return f"PRAGMA key = \"x'{validate(key)}'\""
