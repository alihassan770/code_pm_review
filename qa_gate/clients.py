"""The client registry: one client, one managed staging instance.

Revision 3 of the plan removed the database-clone path entirely, so a client has
exactly one staging database and no way to make another. That is why this is a
flat table rather than a client with many environments — modelling environments
we can never create would be inventing flexibility that does not exist.

Credentials live in a separate table (`instance_secrets`) so they can be granted
separately and are never selected by the list and detail views.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import crypto, db

HOSTING_PLATFORMS = [
    ("odoo_sh", "Odoo.sh"),
    ("cloudpepper", "Cloudpepper"),
    ("self", "Self-hosted / VPS"),
    ("other", "Other managed host"),
]
ODOO_VERSIONS = ["17.0", "18.0", "19.0"]
BRANCH_MODES = [
    ("per_task", "Per task — one branch per task (recommended)"),
    ("shared", "Shared — one working branch for everything"),
]


class ClientError(Exception):
    pass


@dataclass(frozen=True)
class Client:
    id: int
    slug: str
    name: str
    github: str
    odoo_version: str
    hosting_platform: str
    staging_url: str
    staging_db: str
    db_name_pattern: str
    capabilities: list[str] = field(default_factory=list)
    branch_mode: str = "per_task"
    base_branch: str = "main"
    active: bool = True
    created_at: datetime | None = None
    has_credentials: bool = False

    @classmethod
    def from_row(cls, row: dict) -> "Client":
        return cls(
            id=row["id"], slug=row["slug"], name=row["name"],
            github=row["github"], odoo_version=row["odoo_version"],
            hosting_platform=row["hosting_platform"],
            staging_url=row["staging_url"], staging_db=row["staging_db"],
            db_name_pattern=row["db_name_pattern"],
            capabilities=list(row["capabilities"] or []),
            branch_mode=row["branch_mode"], base_branch=row["base_branch"],
            active=row["active"], created_at=row.get("created_at"),
            has_credentials=bool(row.get("has_credentials")),
        )

    @property
    def hosting_label(self) -> str:
        return dict(HOSTING_PLATFORMS).get(self.hosting_platform, self.hosting_platform)

    @property
    def can_clone(self) -> bool:
        """Always False, and named so nobody adds it back by accident.

        Kept as a property rather than omitted so that code reading like
        `if client.can_clone:` fails closed instead of raising AttributeError
        somewhere unhelpful.
        """
        return False


_LIST_SQL = """
    SELECT c.*, (s.rpc_api_key_enc <> '') AS has_credentials
    FROM clients c
    LEFT JOIN instance_secrets s ON s.client_id = c.id
"""


def list_all(*, include_inactive: bool = False) -> list[Client]:
    sql = _LIST_SQL + ("" if include_inactive else " WHERE c.active") + " ORDER BY c.name"
    return [Client.from_row(r) for r in db.query(sql)]


def list_for_user(user_id: int, *, include_inactive: bool = False) -> list[Client]:
    """Clients this person is attached to.

    Staff-only access means this is scoping rather than a security boundary: it
    decides what the dashboard shows by default. An admin sees everything.
    """
    sql = _LIST_SQL + """
        JOIN user_clients uc ON uc.client_id = c.id AND uc.user_id = %s
    """ + ("" if include_inactive else " WHERE c.active") + " ORDER BY c.name"
    return [Client.from_row(r) for r in db.query(sql, (user_id,))]


def get(client_id: int) -> Client | None:
    row = db.query_one(_LIST_SQL + " WHERE c.id = %s", (client_id,))
    return Client.from_row(row) if row else None


def get_by_slug(slug: str) -> Client | None:
    row = db.query_one(_LIST_SQL + " WHERE c.slug = %s", (slug,))
    return Client.from_row(row) if row else None


def create(*, slug: str, name: str, created_by: int, **fields) -> Client:
    if get_by_slug(slug):
        raise ClientError(f"A client with the slug {slug!r} already exists.")
    row = db.query_one(
        """
        INSERT INTO clients (slug, name, github, odoo_version, hosting_platform,
                             staging_url, staging_db, db_name_pattern,
                             branch_mode, base_branch, created_by)
        VALUES (%(slug)s, %(name)s, %(github)s, %(odoo_version)s, %(hosting_platform)s,
                %(staging_url)s, %(staging_db)s, %(db_name_pattern)s,
                %(branch_mode)s, %(base_branch)s, %(created_by)s)
        RETURNING *
        """,
        _params(slug=slug, name=name, created_by=created_by, **fields),
    )
    client = Client.from_row({**row, "has_credentials": False})
    attach_user(created_by, client.id, access="owner")
    return client


def update(client_id: int, **fields) -> Client:
    existing = get(client_id)
    if not existing:
        raise ClientError(f"No client with id {client_id}.")
    # Pulled out of **fields before it is splatted into _params, which also
    # takes `name` positionally — leaving it in raises "got multiple values for
    # keyword argument 'name'" and made every client edit a 500.
    name = fields.pop("name", existing.name)
    active = fields.pop("active", existing.active)
    # Unspecified columns keep their current value. The edit form always posts
    # every field, so this changes nothing there — but it means a caller that
    # wants to change one thing (`update(id, github=...)`) does not silently
    # blank the staging URL, which is the kind of data loss that only shows up
    # as an audit failing for a reason nobody can explain.
    fields = {**_current(existing), **fields}
    db.execute(
        """
        UPDATE clients SET
            name = %(name)s, github = %(github)s, odoo_version = %(odoo_version)s,
            hosting_platform = %(hosting_platform)s, staging_url = %(staging_url)s,
            staging_db = %(staging_db)s, db_name_pattern = %(db_name_pattern)s,
            branch_mode = %(branch_mode)s, base_branch = %(base_branch)s,
            active = %(active)s, updated_at = now()
        WHERE id = %(id)s
        """,
        {**_params(slug=existing.slug, name=name, created_by=None, **fields),
         "id": client_id, "active": active},
    )
    return get(client_id)  # type: ignore[return-value]


def _current(client: Client) -> dict:
    """The updatable columns of an existing client, as _params expects them."""
    return {
        "github": client.github, "odoo_version": client.odoo_version,
        "hosting_platform": client.hosting_platform,
        "staging_url": client.staging_url, "staging_db": client.staging_db,
        "db_name_pattern": client.db_name_pattern,
        "branch_mode": client.branch_mode, "base_branch": client.base_branch,
    }


def _params(*, slug: str, name: str, created_by: int | None, **fields) -> dict:
    version = fields.get("odoo_version") or "17.0"
    if version not in ODOO_VERSIONS:
        raise ClientError(f"Unsupported Odoo version {version!r}.")
    platform = fields.get("hosting_platform") or "other"
    if platform not in dict(HOSTING_PLATFORMS):
        raise ClientError(f"Unknown hosting platform {platform!r}.")
    mode = fields.get("branch_mode") or "per_task"
    if mode not in dict(BRANCH_MODES):
        raise ClientError(f"Unknown branch mode {mode!r}.")
    return {
        "slug": slug,
        "name": name,
        "github": (fields.get("github") or "").strip().lower(),
        "odoo_version": version,
        "hosting_platform": platform,
        "staging_url": (fields.get("staging_url") or "").rstrip("/"),
        "staging_db": (fields.get("staging_db") or "").strip(),
        "db_name_pattern": (fields.get("db_name_pattern") or "%_staging").strip(),
        "branch_mode": mode,
        "base_branch": (fields.get("base_branch") or "main").strip(),
        "created_by": created_by,
    }


def team_of(client_id: int) -> list[dict]:
    """Who is attached to this client, for the team page.

    Returns rows rather than `User` objects because the access level lives on
    the join and a User dataclass carrying a per-client field would be wrong
    everywhere else it is used.
    """
    return db.query(
        """
        SELECT u.id, u.name, u.login, u.email, u.is_admin, uc.access
        FROM user_clients uc JOIN users u ON u.id = uc.user_id
        WHERE uc.client_id = %s
        ORDER BY uc.access DESC, u.name
        """,
        (client_id,),
    )


def detach_user(user_id: int, client_id: int) -> None:
    """Remove someone's attachment.

    Scoping, not revocation: staff-only access means an admin still sees every
    client, and this only changes whose dashboard the client appears on.
    """
    db.execute(
        "DELETE FROM user_clients WHERE user_id = %s AND client_id = %s",
        (user_id, client_id),
    )


def attach_user(user_id: int, client_id: int, *, access: str = "member") -> None:
    db.execute(
        """
        INSERT INTO user_clients (user_id, client_id, access) VALUES (%s, %s, %s)
        ON CONFLICT (user_id, client_id) DO UPDATE SET access = EXCLUDED.access
        """,
        (user_id, client_id, access),
    )


# ---- credentials -----------------------------------------------------------
#
# Kept in this module rather than a separate one so the encryption boundary is
# visible next to the thing it protects, but read by nothing that renders a page.

def set_rpc_credentials(client_id: int, *, login: str, api_key: str,
                        secret_key: str, updated_by: int) -> None:
    """Store the RPC credential for a client's staging instance.

    An API key, not a password: it is what Odoo requires for RPC when the
    account has 2FA, and it can be revoked in Odoo without changing a password
    a human also uses. It deliberately cannot open a browser session — those
    need real persona passwords, which arrive in phase B.
    """
    db.execute(
        """
        INSERT INTO instance_secrets (client_id, rpc_login, rpc_api_key_enc, updated_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (client_id) DO UPDATE SET
            rpc_login       = EXCLUDED.rpc_login,
            rpc_api_key_enc = EXCLUDED.rpc_api_key_enc,
            updated_by      = EXCLUDED.updated_by,
            updated_at      = now()
        """,
        (client_id, login.strip(), crypto.encrypt(secret_key, api_key), updated_by),
    )


def get_rpc_credentials(client_id: int, secret_key: str) -> tuple[str, str]:
    """(login, api_key). Never call this from a request that renders a page."""
    row = db.query_one(
        "SELECT rpc_login, rpc_api_key_enc FROM instance_secrets WHERE client_id = %s",
        (client_id,),
    )
    if not row or not row["rpc_api_key_enc"]:
        return "", ""
    return row["rpc_login"], crypto.decrypt(secret_key, row["rpc_api_key_enc"])
