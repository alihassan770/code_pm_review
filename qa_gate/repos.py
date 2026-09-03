"""The repositories that hold a client's addons.

A client routinely has more than one: the custom addons, a theme, a fork of an
OCA module. Each carries its own branch policy, because a theme repo sitting on
a shared branch while the addons repo is per-task is a real configuration and
not a mistake.

`github` (`owner/name`) is the join key for finding a local checkout — never the
client slug. Inherited from odoo-dev-loop, where matching on a config id meant
two independently edited files had to agree and drift produced a 404 telling you
to go edit YAML.

Every branch stored here passes through `branches`, so a client repository can
never be configured to write to `main`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from . import branches, db

GITHUB_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_MODES = [
    ("per_task", "Per task — one branch per task (recommended)"),
    ("shared", "Shared — one working branch for everything"),
]


class RepoError(Exception):
    pass


@dataclass(frozen=True)
class Repo:
    id: int
    client_id: int
    github: str
    base_branch: str
    branch_mode: str
    label: str = ""
    active: bool = True
    position: int = 0
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict) -> "Repo":
        return cls(
            id=row["id"], client_id=row["client_id"], github=row["github"],
            base_branch=row["base_branch"], branch_mode=row["branch_mode"],
            label=row.get("label") or "", active=row["active"],
            position=row.get("position") or 0, created_at=row.get("created_at"),
        )

    @property
    def owner(self) -> str:
        return self.github.split("/")[0] if "/" in self.github else ""

    @property
    def name(self) -> str:
        return self.github.split("/")[-1]

    @property
    def url(self) -> str:
        return f"https://github.com/{self.github}"

    @property
    def is_shared(self) -> bool:
        return self.branch_mode == "shared"


def for_client(client_id: int, *, include_inactive: bool = False) -> list[Repo]:
    sql = "SELECT * FROM client_repos WHERE client_id = %s"
    if not include_inactive:
        sql += " AND active"
    sql += " ORDER BY position, id"
    return [Repo.from_row(r) for r in db.query(sql, (client_id,))]


def get(repo_id: int) -> Repo | None:
    row = db.query_one("SELECT * FROM client_repos WHERE id = %s", (repo_id,))
    return Repo.from_row(row) if row else None


def add(client_id: int, *, github: str, base_branch: str = branches.MANAGED_ROOT,
        branch_mode: str = "per_task", label: str = "") -> Repo:
    github = normalize_github(github)
    base_branch = validate_base_branch(base_branch)
    if branch_mode not in dict(BRANCH_MODES):
        raise RepoError(f"Unknown branch mode {branch_mode!r}.")

    existing = db.query_one(
        "SELECT id FROM client_repos WHERE client_id = %s AND github = %s",
        (client_id, github))
    if existing:
        raise RepoError(f"{github} is already attached to this client.")

    row = db.query_one(
        """
        INSERT INTO client_repos (client_id, github, base_branch, branch_mode, label, position)
        VALUES (%s, %s, %s, %s, %s,
                COALESCE((SELECT max(position) + 1 FROM client_repos WHERE client_id = %s), 0))
        RETURNING *
        """,
        (client_id, github, base_branch, branch_mode, label.strip(), client_id),
    )
    _mirror_primary(client_id)
    return Repo.from_row(row)


def remove(repo_id: int) -> None:
    db.execute("DELETE FROM client_repos WHERE id = %s", (repo_id,))


def replace_all(client_id: int, entries: list[dict]) -> list[Repo]:
    """Set the whole list at once, which is what a multi-row form submits.

    Done inside one transaction: a half-applied repository list would leave a
    client pointing at addons it does not have, and the knowledge base would
    then confidently report a smaller codebase than really exists.
    """
    cleaned = []
    seen: set[str] = set()
    for e in entries:
        gh = (e.get("github") or "").strip()
        if not gh:
            continue
        gh = normalize_github(gh)
        if gh in seen:
            raise RepoError(f"{gh} is listed twice.")
        seen.add(gh)
        cleaned.append({
            "github": gh,
            "base_branch": validate_base_branch(e.get("base_branch") or branches.MANAGED_ROOT),
            "branch_mode": e.get("branch_mode") or "per_task",
            "label": (e.get("label") or "").strip(),
        })
    for c in cleaned:
        if c["branch_mode"] not in dict(BRANCH_MODES):
            raise RepoError(f"Unknown branch mode {c['branch_mode']!r}.")

    with db.pool().connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM client_repos WHERE client_id = %s", (client_id,))
        for i, c in enumerate(cleaned):
            cur.execute(
                """
                INSERT INTO client_repos
                    (client_id, github, base_branch, branch_mode, label, position)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (client_id, c["github"], c["base_branch"], c["branch_mode"], c["label"], i),
            )
    _mirror_primary(client_id)
    return for_client(client_id)


def _mirror_primary(client_id: int) -> None:
    """Keep `clients.github` pointing at the first repository.

    Phase C's repo_sync still reads the single client-level column. Rather than
    rewrite that while it is being worked on, the column is maintained as a
    mirror of the primary repo so existing behaviour is unchanged. Reading it
    anywhere new is a mistake — `for_client()` is the real list, and the
    knowledge base should walk every repo, not just this one.
    """
    row = db.query_one(
        "SELECT github, base_branch, branch_mode FROM client_repos "
        "WHERE client_id = %s ORDER BY position, id LIMIT 1", (client_id,))
    db.execute(
        "UPDATE clients SET github = %s, base_branch = %s, branch_mode = %s WHERE id = %s",
        (row["github"] if row else "",
         row["base_branch"] if row else "staging",
         row["branch_mode"] if row else "per_task",
         client_id),
    )


def counts_by_client() -> dict[int, int]:
    rows = db.query(
        "SELECT client_id, count(*) AS n FROM client_repos WHERE active GROUP BY client_id")
    return {r["client_id"]: int(r["n"]) for r in rows}


# ---- validation ------------------------------------------------------------

def normalize_github(value: str) -> str:
    """Accept a full clone URL or `owner/name`, return `owner/name`.

    People paste the browser URL far more often than they type the slug, and
    rejecting that is friction with no upside.
    """
    v = (value or "").strip()
    v = re.sub(r"^(https?://|git@)(www\.)?github\.com[:/]", "", v, flags=re.I)
    v = re.sub(r"\.git/?$", "", v).strip("/")
    if not GITHUB_RE.match(v):
        raise RepoError(
            f"{value!r} is not a GitHub repository. Use owner/name, for example "
            "yourorg/yourproject, or paste the repository URL."
        )
    return v


def validate_base_branch(branch: str) -> str:
    """Normalise the branch this repo is read and diffed against.

    Protected names are **allowed** here, and that is deliberate. The base
    branch is a read reference: it is where a client's code actually lives, what
    the knowledge base is built from, and what a task branch is diffed against.
    Refusing `main` here would make the common case unconfigurable.

    The write ban is a separate concern and lives at the write itself —
    `branches.assert_writable`, which permits only branches this app creates
    under `staging`. Reading `main` is normal; committing to it is never allowed,
    and no value stored in this column can change that.
    """
    return branches.normalize(branch) or branches.MANAGED_ROOT
