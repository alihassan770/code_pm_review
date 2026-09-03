"""Service credentials for our own Odoo.

Reading tasks needs a credential that is not a logged-in person's. A nightly run
or a queued job has no session to borrow from, and the alternative — storing
every staff member's Odoo password so RPC could be made "as them" — trades a
small scoping benefit for a much larger secret to lose.

So one service account, stored once, encrypted with the same key as client
credentials. Staff still authenticate individually; this is only used for the
machine-to-machine reads and (later) chatter writes described in plan §4.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import crypto, db

IDENTITY_RPC = "identity_rpc"
#: A GitHub personal access token. Private repositories return 404 rather than
#: 403 to an unauthenticated caller — GitHub deliberately does not confirm that
#: a private repo exists — so without this a private repo is indistinguishable
#: from a typo in the owner/name.
GITHUB_TOKEN = "github_token"
#: A DeepSeek API key. One key for the whole application rather than one per
#: client: the reviews are run by us, so making each client bring their own
#: provider account would be billing our costs to people who never see the tool.
#: Admin-only to set, like everything else on the settings page — see `ai.py`
#: for the much more important question of what it may be used *for*.
DEEPSEEK_KEY = "deepseek_key"


@dataclass(frozen=True)
class ServiceCredential:
    login: str
    secret: str

    @property
    def configured(self) -> bool:
        return bool(self.login and self.secret)


def get(key: str, secret_key: str) -> ServiceCredential:
    row = db.query_one("SELECT login, secret_enc FROM app_secrets WHERE key = %s", (key,))
    if not row or not row["secret_enc"]:
        return ServiceCredential("", "")
    return ServiceCredential(row["login"], crypto.decrypt(secret_key, row["secret_enc"]))


def set_(key: str, *, login: str, secret: str, secret_key: str,
         updated_by: int | None = None) -> None:
    db.execute(
        """
        INSERT INTO app_secrets (key, login, secret_enc, updated_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (key) DO UPDATE SET
            login = EXCLUDED.login, secret_enc = EXCLUDED.secret_enc,
            updated_by = EXCLUDED.updated_by, updated_at = now()
        """,
        (key, login.strip(), crypto.encrypt(secret_key, secret), updated_by),
    )


def is_configured(key: str) -> bool:
    """Cheap existence check that never decrypts, for rendering a page."""
    row = db.query_one(
        "SELECT (secret_enc <> '') AS ok, login FROM app_secrets WHERE key = %s", (key,))
    return bool(row and row["ok"])


def login_for(key: str) -> str:
    row = db.query_one("SELECT login FROM app_secrets WHERE key = %s", (key,))
    return row["login"] if row else ""
