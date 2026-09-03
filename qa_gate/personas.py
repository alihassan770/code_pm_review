"""Browser logins for a client's staging instance.

These exist because of one verified Odoo behaviour: in `res_users.py` the
API-key branch of `_check_credentials` sits behind `if not interactive:`. An API
key authenticates RPC and **cannot open a web session**. Screenshot evidence is
produced by a real browser, so it needs a real password.

Two consequences worth holding on to:

* **These accounts must not have two-factor enabled.** With 2FA, a web login
  additionally demands a TOTP code that no automated run can supply.
* **Never reuse a real employee's account.** Their password rotates and their
  group membership drifts, and both failures arrive looking exactly like a
  regression in the code under test.

More than one persona is the point, not an afterthought. Plan §2 decision 7:
access-rights regressions are among the most common in Odoo work and are
completely invisible to an admin session, so flows are replayed as several
users. Tier 2 gets this free — the probe runs inside Odoo and switches user with
`env(user=…)` — but tier 3 drives a browser and therefore needs credentials per
persona.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from . import crypto, db
from .clients import Client
from .odoo_client import OdooAuthError, OdooClient, OdooError

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

#: Suggested personas. Keys match what scenarios will reference, and the set is
#: drawn from the roles §10's persona matrix actually exercises.
SUGGESTED = [
    ("primary", "Primary — the everyday user flows are recorded as"),
    ("sales_user", "Sales user"),
    ("sales_manager", "Sales manager"),
    ("production_user", "Production user"),
    ("stock_user", "Inventory user"),
    ("accountant", "Accountant"),
    ("portal", "Portal user — the external-visibility check"),
]


class PersonaError(Exception):
    pass


@dataclass(frozen=True)
class Persona:
    id: int
    client_id: int
    key: str
    label: str
    login: str
    has_password: bool
    verified_at: datetime | None
    verify_error: str
    active: bool

    @classmethod
    def from_row(cls, row: dict) -> "Persona":
        return cls(
            id=row["id"], client_id=row["client_id"], key=row["key"],
            label=row.get("label") or "", login=row["login"],
            has_password=bool(row.get("has_password")),
            verified_at=row.get("verified_at"),
            verify_error=row.get("verify_error") or "", active=row["active"],
        )

    @property
    def state(self) -> str:
        """`verified` / `unproven` / `failed` — drives the status chip."""
        if self.verify_error:
            return "failed"
        return "verified" if self.verified_at else "unproven"


_SELECT = """
    SELECT id, client_id, key, label, login, verified_at, verify_error, active,
           (password_enc <> '') AS has_password
    FROM client_personas
"""


def for_client(client_id: int, *, include_inactive: bool = False) -> list[Persona]:
    sql = _SELECT + " WHERE client_id = %s"
    if not include_inactive:
        sql += " AND active"
    sql += " ORDER BY (key = 'primary') DESC, key"
    return [Persona.from_row(r) for r in db.query(sql, (client_id,))]


def get(persona_id: int) -> Persona | None:
    row = db.query_one(_SELECT + " WHERE id = %s", (persona_id,))
    return Persona.from_row(row) if row else None


def save(client_id: int, *, key: str, login: str, password: str, secret_key: str,
         label: str = "", updated_by: int | None = None) -> Persona:
    """Create or update a persona. An empty password keeps the stored one."""
    key = (key or "").strip().lower()
    if not KEY_RE.match(key):
        raise PersonaError(
            f"{key!r} is not a valid handle. Use lowercase letters, digits and "
            "underscores, e.g. sales_user.")
    if not login.strip():
        raise PersonaError("A login is required.")

    if password:
        row = db.query_one(
            """
            INSERT INTO client_personas (client_id, key, label, login, password_enc, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (client_id, key) DO UPDATE SET
                label = EXCLUDED.label, login = EXCLUDED.login,
                password_enc = EXCLUDED.password_enc, updated_by = EXCLUDED.updated_by,
                verified_at = NULL, verify_error = ''
            RETURNING id
            """,
            (client_id, key, label.strip(), login.strip(),
             crypto.encrypt(secret_key, password), updated_by),
        )
    else:
        row = db.query_one(
            """
            INSERT INTO client_personas (client_id, key, label, login, updated_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (client_id, key) DO UPDATE SET
                label = EXCLUDED.label, login = EXCLUDED.login,
                updated_by = EXCLUDED.updated_by
            RETURNING id
            """,
            (client_id, key, label.strip(), login.strip(), updated_by),
        )
    return get(row["id"])  # type: ignore[return-value]


def remove(persona_id: int) -> None:
    db.execute("DELETE FROM client_personas WHERE id = %s", (persona_id,))


def password_of(persona_id: int, secret_key: str) -> str:
    """Never call this from a request that renders a page."""
    row = db.query_one(
        "SELECT password_enc FROM client_personas WHERE id = %s", (persona_id,))
    if not row or not row["password_enc"]:
        return ""
    return crypto.decrypt(secret_key, row["password_enc"])


def verify(persona_id: int, client: Client, secret_key: str) -> Persona:
    """Prove the credential can open a real web session, and record the result.

    Deliberately uses `open_session` rather than `authenticate`: an API key would
    pass the second and fail the first, and finding that out mid-run — as a
    screenshot flow that cannot log in — is the failure this check exists to
    prevent.
    """
    persona = get(persona_id)
    if not persona:
        raise PersonaError("No such persona.")
    password = password_of(persona_id, secret_key)
    if not password:
        raise PersonaError("No password stored for this persona yet.")
    if not client.staging_url or not client.staging_db:
        raise PersonaError("Set the staging URL and database before verifying a login.")

    odoo = OdooClient(client.staging_url, client.staging_db)
    try:
        odoo.open_session(persona.login, password)
    except (OdooAuthError, OdooError) as exc:
        db.execute(
            "UPDATE client_personas SET verified_at = NULL, verify_error = %s WHERE id = %s",
            (str(exc)[:500], persona_id))
        return get(persona_id)  # type: ignore[return-value]

    db.execute(
        "UPDATE client_personas SET verified_at = now(), verify_error = '' WHERE id = %s",
        (persona_id,))
    return get(persona_id)  # type: ignore[return-value]


def record_failure(persona_id: int, reason: str) -> None:
    """Mark a persona as no longer able to sign in.

    Verification is a snapshot, and a stale one is worse than none: a password
    that worked this morning and was rotated at lunchtime leaves the client page
    claiming "Signs in" while every run fails. So any later refusal writes back
    here, and the badge tells the truth without anyone re-pressing Verify.
    """
    db.execute(
        "UPDATE client_personas SET verified_at = NULL, verify_error = %s "
        "WHERE id = %s", (str(reason)[:500], persona_id))


def counts_by_client() -> dict[int, int]:
    rows = db.query(
        "SELECT client_id, count(*) AS n FROM client_personas WHERE active GROUP BY client_id")
    return {r["client_id"]: int(r["n"]) for r in rows}
