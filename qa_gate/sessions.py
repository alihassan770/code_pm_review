"""Server-side sessions for the staff web UI.

The cookie holds a random token; the database holds only its SHA-256. A leaked
dump therefore does not hand over live sessions.

Note the deliberate asymmetry with the runner API described in CLAUDE.md: the
runner authenticates by header token with `allow_credentials=False`, precisely
so CSRF cannot apply to it. This module is the opposite surface — a browser
session with a cookie — so it carries its own CSRF token and every mutating form
must present it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import crypto, db
from .users import User

log = logging.getLogger(__name__)

COOKIE_NAME = "qa_gate_session"


@dataclass(frozen=True)
class Session:
    user: User
    csrf_token: str
    expires_at: datetime


def create(user: User, hours: int, *, ip: str | None = None,
           user_agent: str | None = None) -> tuple[str, Session]:
    """Returns (cookie_value, session). The cookie value is never stored."""
    token = crypto.new_session_token()
    csrf = crypto.new_csrf_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    db.execute(
        """
        INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at, ip, user_agent)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (crypto.hash_session_token(token), user.id, csrf, expires, ip, user_agent),
    )
    return token, Session(user=user, csrf_token=csrf, expires_at=expires)


def load(token: str) -> Session | None:
    """Resolve a cookie value to a live session, or None.

    An expired or unknown token returns None rather than raising: the caller's
    job is to redirect to the login page, and an exception would make that the
    unusual path rather than the normal one.
    """
    if not token:
        return None
    row = db.query_one(
        """
        SELECT s.csrf_token, s.expires_at, u.*
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.expires_at > now() AND u.active
        """,
        (crypto.hash_session_token(token),),
    )
    if not row:
        return None
    return Session(
        user=User.from_row(row),
        csrf_token=row["csrf_token"],
        expires_at=row["expires_at"],
    )


def destroy(token: str) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = %s",
                   (crypto.hash_session_token(token),))


def destroy_all_for_user(user_id: int) -> int:
    return db.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


def purge_expired() -> int:
    """Called at startup. Cheap, and stops the table growing without bound."""
    n = db.execute("DELETE FROM sessions WHERE expires_at <= now()")
    if n:
        log.info("Purged %d expired session(s)", n)
    return n
