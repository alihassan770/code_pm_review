"""The local mirror of an Odoo user.

We store a row per person so that runs, clients, and audit trails have something
stable to reference, but Odoo remains the authority: this row is created on
first login and refreshed on every login. Nothing here grants access — being
absent from Odoo is what removes it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import db
from .odoo_client import OdooUser


@dataclass(frozen=True)
class User:
    id: int
    odoo_uid: int
    login: str
    name: str
    email: str
    is_admin: bool
    active: bool
    last_seen_at: datetime | None

    @classmethod
    def from_row(cls, row: dict) -> "User":
        return cls(
            id=row["id"], odoo_uid=row["odoo_uid"], login=row["login"],
            name=row["name"], email=row["email"], is_admin=row["is_admin"],
            active=row["active"], last_seen_at=row.get("last_seen_at"),
        )


def upsert_from_odoo(odoo_user: OdooUser) -> User:
    """Create or refresh the local row for someone who just authenticated.

    Keyed on `odoo_uid` rather than login, because a login can be edited in Odoo
    and we would otherwise create a second row for the same person.

    ## Who is an administrator here

    Being an administrator of *this app* is not the same as holding
    `base.group_system` in Odoo, and treating them as one thing locks the actual
    operator out of their own tool: the lead who runs the gate is very often not
    an Odoo sysadmin. So admin is granted by either of:

      * holding `base.group_system` in Odoo, or
      * **being the first person to sign in** — somebody has to be able to
        configure a fresh install, and there is no one else to ask.

    The flag is also never taken away by a later login. Odoo group membership
    drifts, and silently demoting the only administrator would leave nobody able
    to reach the settings that would fix it.
    """
    first_user = count() == 0
    row = db.query_one(
        """
        INSERT INTO users (odoo_uid, login, name, email, is_admin, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (odoo_uid) DO UPDATE SET
            login        = EXCLUDED.login,
            name         = EXCLUDED.name,
            email        = EXCLUDED.email,
            -- never demote: see the docstring
            is_admin     = users.is_admin OR EXCLUDED.is_admin,
            last_seen_at = now()
        RETURNING *
        """,
        (odoo_user.uid, odoo_user.login, odoo_user.name,
         odoo_user.email, odoo_user.is_admin or first_user),
    )
    return User.from_row(row)


def grant_admin(login: str) -> bool:
    """Promote by login. The escape hatch for `qa-gate grant-admin`."""
    return bool(db.execute(
        "UPDATE users SET is_admin = true WHERE login = %s", (login.strip(),)))


def set_admin(user_id: int, admin: bool) -> None:
    """Move one person between the two roles.

    The caller is responsible for refusing to demote the last administrator,
    because it is the caller that can explain why. See
    `web/routes/settings.py:set_role`.
    """
    db.execute("UPDATE users SET is_admin = %s WHERE id = %s", (admin, user_id))


def admin_count() -> int:
    """How many administrators exist, for the last-administrator guard."""
    row = db.query_one(
        "SELECT count(*) AS n FROM users WHERE is_admin AND coalesce(active, true)")
    return int(row["n"]) if row else 0


def get(user_id: int) -> User | None:
    row = db.query_one("SELECT * FROM users WHERE id = %s", (user_id,))
    return User.from_row(row) if row else None


def list_all(*, include_inactive: bool = False) -> list[User]:
    """Everyone who has ever logged in.

    There is no user directory to browse: a person appears here on their first
    login and not before, because Odoo is the authority and inventing rows for
    people who have never used the tool would create accounts we do not manage.
    The team page says so rather than showing an empty list with no explanation.
    """
    sql = "SELECT * FROM users"
    if not include_inactive:
        sql += " WHERE active"
    return [User.from_row(r) for r in db.query(sql + " ORDER BY name, login")]


def count() -> int:
    row = db.query_one("SELECT count(*) AS n FROM users")
    return int(row["n"]) if row else 0
