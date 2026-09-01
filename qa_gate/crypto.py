"""Encryption for client instance credentials, and session token hashing.

Fernet (AES-128-CBC + HMAC) keyed by `config.secret_key`. The threat being
addressed is a Postgres backup or a dump landing somewhere it should not: the
gate holds an API key for every client's staging instance, and those are not our
credentials to lose.

It deliberately does NOT protect against someone who already has the config file
— at that point they have the key. Defending against that needs a KMS, which is
the right upgrade when this leaves a single host.
"""
from __future__ import annotations

import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken

SESSION_TOKEN_BYTES = 32


class DecryptError(Exception):
    """Raised when stored ciphertext will not open with the current key.

    Almost always means `secret_key` in the config was changed or regenerated.
    Worth saying so explicitly, because the alternative reading — "the database
    is corrupt" — sends people looking in the wrong place.
    """


def _fernet(secret_key: str) -> Fernet:
    if not secret_key:
        raise DecryptError("config.secret_key is empty; cannot encrypt or decrypt.")
    return Fernet(secret_key.encode() if isinstance(secret_key, str) else secret_key)


def encrypt(secret_key: str, plaintext: str) -> str:
    if plaintext == "":
        return ""
    return _fernet(secret_key).encrypt(plaintext.encode()).decode()


def decrypt(secret_key: str, token: str) -> str:
    if token == "":
        return ""
    try:
        return _fernet(secret_key).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise DecryptError(
            "Stored credential will not decrypt with the current secret_key. "
            "If the key was rotated or regenerated, every client credential "
            "must be re-entered."
        ) from exc


# ---- session tokens --------------------------------------------------------

def new_session_token() -> str:
    """The value that goes in the cookie. Never stored."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> bytes:
    """What goes in the database.

    Plain SHA-256 rather than a password hash: the token is 256 bits of CSPRNG
    output, so there is nothing to brute-force and a slow KDF would only add
    latency to every request.
    """
    return hashlib.sha256(token.encode()).digest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def constant_time_equals(a: str, b: str) -> bool:
    return secrets.compare_digest(a or "", b or "")
