"""Non-secret application settings, set by an administrator.

The sibling of `app_secrets`: that module holds things that are encrypted
because losing them matters, this one holds the choices that decide *which* of
them to use. The AI provider is the first: "deepseek" is not a secret, it is a
selection, and encrypting it would have obscured a value whose whole job is to
be read.

Everything here is application-wide on purpose. An administrator sets the Odoo
connection and the AI provider once and every user works against them, so these
are deliberately not per-user rows. See `users.py` for who may write them.
"""
from __future__ import annotations

from . import db

#: Which provider the AI calls go to. One of `ai.PROVIDERS`.
AI_PROVIDER = "ai_provider"


def get(key: str, default: str = "") -> str:
    row = db.query_one("SELECT value FROM app_settings WHERE key = %s", (key,))
    return (row["value"] if row and row["value"] else default)


def set_(key: str, value: str, *, updated_by: int | None = None) -> None:
    db.execute(
        """
        INSERT INTO app_settings (key, value, updated_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
            updated_at = now()
        """,
        (key, (value or "").strip(), updated_by),
    )
