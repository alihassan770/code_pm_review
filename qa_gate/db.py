"""Postgres access and schema migration.

A connection pool plus numbered `.sql` files applied in order. Alembic was the
obvious alternative and was rejected for now: the schema is young, every change
so far is additive, and a directory of plain SQL is easier to read in a review
than generated Python. Revisit if we ever need a data migration with branching.

Migrations are applied inside a transaction and recorded in `schema_migrations`,
so `migrate()` is idempotent and safe to call on every boot.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

_pool: ConnectionPool | None = None


def init_pool(database_url: str, *, min_size: int = 1, max_size: int = 8) -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            database_url, min_size=min_size, max_size=max_size,
            kwargs={"row_factory": dict_row}, open=True,
        )
        log.info("Postgres pool opened")
    return _pool


def pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("db.init_pool() has not been called")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# ---- query helpers ---------------------------------------------------------
#
# Thin on purpose. Anything that wants a transaction spanning several statements
# should take a connection with `pool().connection()` directly rather than
# stringing these together.

Params = Mapping[str, Any] | Iterable[Any]


def _bind(params: Params) -> Mapping[str, Any] | tuple:
    """psycopg needs a mapping for `%(name)s` and a sequence for `%s`.

    Passing a dict through `tuple()` silently yields its keys, which fails much
    later with 'named placeholders require a mapping of parameters'. Keep the
    distinction here so callers can use whichever style reads better.
    """
    return params if isinstance(params, Mapping) else tuple(params)


def query(sql: str, params: Params = ()) -> list[dict]:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, _bind(params))
        return cur.fetchall()


def query_one(sql: str, params: Params = ()) -> dict | None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, _bind(params))
        return cur.fetchone()


def execute(sql: str, params: Params = ()) -> int:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, _bind(params))
        return cur.rowcount


# ---- migrations ------------------------------------------------------------

def discover_migrations() -> list[tuple[str, Path]]:
    """Numbered migrations in filename order. A file that does not match the
    naming convention is an error rather than a silent skip — a migration that
    never runs is worse than one that fails loudly."""
    found: list[tuple[str, Path]] = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if not m:
            raise RuntimeError(
                f"Migration {p.name!r} does not match NNN_lower_snake.sql. "
                "Rename it, or it will never be applied."
            )
        found.append((m.group(1), p))
    return found


def migrate(database_url: str | None = None) -> list[str]:
    """Apply any migration not yet recorded. Returns the versions applied."""
    conn_ctx = (
        psycopg.connect(database_url, row_factory=dict_row)
        if database_url else pool().connection()
    )
    applied: list[str] = []
    with conn_ctx as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  version text PRIMARY KEY,"
                "  applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cur.execute("SELECT version FROM schema_migrations")
            done = {r["version"] for r in cur.fetchall()}

        for version, path in discover_migrations():
            if version in done:
                continue
            log.info("Applying migration %s (%s)", version, path.name)
            with conn.cursor() as cur:
                cur.execute(path.read_text())
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                )
            conn.commit()
            applied.append(version)
    return applied
