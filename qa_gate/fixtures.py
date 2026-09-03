"""Creating and removing the records a review needs to test against.

This is the only module in the gate that writes to a client's database, and
everything about it is shaped by that.

## The gate before the gate

`assert_writable` refuses unless Odoo itself says the database is neutralized.
`database.is_neutralized` is set by Odoo on staging and duplicate databases; it
is what disables outgoing mail, payment provider calls and external crons. A
production database does not have it.

That check is deliberately not a URL pattern or a name convention. "staging" in
a hostname is a naming habit somebody can get wrong once; `is_neutralized` is
the database's own statement about what it is. Verified against a live Odoo 18
staging instance, where it reads `true`.

## Everything created is recorded before it is used

Every write goes through `create`, which writes the ledger row in the same call.
If this process dies immediately afterwards, the row is what lets a later run
find the orphan. A list held in memory would leave records in somebody's
database with nothing pointing at them.

## Removal is expected to fail sometimes

Odoo will not unlink a posted journal entry, and it is right not to. So
`rollback` records the refusal against the ledger row rather than raising: a
record that could not be removed is something a human must be told about, and
losing that fact to an exception would be the worst outcome.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import db

log = logging.getLogger(__name__)


class NotWritable(Exception):
    """The instance must not be written to. Message is safe to show."""


class FixtureError(Exception):
    """A fixture could not be created. Message is safe to show."""


#: Models a review may never create, whatever a plan asks for. Users and access
#: groups because a fixture that grants itself rights is not a test; company and
#: currency because they are global settings a client's whole database hangs
#: off, and an accidental one is not cleanable in any meaningful sense.
FORBIDDEN_MODELS = frozenset({
    "res.users", "res.groups", "res.company", "res.currency",
    "ir.model", "ir.model.fields", "ir.module.module", "ir.rule",
    "ir.config_parameter", "ir.cron",
    # Configuration, not test data. Every Odoo already has a bank journal and a
    # payment method; a review that invents its own is adding setup to a client's
    # chart of accounts. Worse, Odoo refuses to unlink a journal once anything
    # posts to it, so cleanup fails and the litter is permanent. Plans are told
    # to FIND these with `find` instead — see `resolve`.
    "account.journal", "account.payment.method", "account.payment.method.line",
    "account.account", "account.tax", "account.fiscal.position",
    "product.category", "uom.uom", "res.currency.rate",
})


def assert_writable(conn) -> None:
    """Refuse unless Odoo says this database is neutralized.

    Called once per run before anything is created, and again is cheap enough
    that callers need not cache it.
    """
    try:
        value = conn.call("ir.config_parameter", "get_param",
                          ["database.is_neutralized"])
    except Exception as exc:  # noqa: BLE001 - unreadable means unproven
        raise NotWritable(
            "Could not ask this instance whether it is a neutralized staging "
            f"database ({exc}), so nothing will be created on it.") from exc

    if str(value).strip().lower() not in ("true", "1"):
        raise NotWritable(
            "This database does not report itself as neutralized "
            "(`database.is_neutralized` is not true), so it may be production. "
            "The gate will not create records on it. Odoo sets this flag on "
            "staging and duplicate databases.")


@dataclass
class Ledger:
    """What one run has created, and what became of it."""
    run_id: int
    by_ref: dict[str, tuple[str, int]] = field(default_factory=dict)

    def id_of(self, ref: str) -> int | None:
        found = self.by_ref.get(ref)
        return found[1] if found else None

    def model_of(self, ref: str) -> str:
        found = self.by_ref.get(ref)
        return found[0] if found else ""


def create(conn, ledger: Ledger, *, ref: str, model: str, values: dict) -> int:
    """Create one record, recording it before returning."""
    model = (model or "").strip()
    if not model:
        raise FixtureError("A fixture with no model cannot be created.")
    if model in FORBIDDEN_MODELS:
        raise FixtureError(
            f"{model} is on the list of models a review may never create. "
            "Testing a change to it needs a person, not an automated run.")
    if not isinstance(values, dict):
        raise FixtureError(f"Fixture {ref!r} has no field values.")

    try:
        res_id = conn.call(model, "create", [values])
    except Exception as exc:  # noqa: BLE001 - the message is what the reader needs
        raise FixtureError(f"Could not create {model} for {ref!r}: {exc}") from exc

    if isinstance(res_id, list):
        res_id = res_id[0] if res_id else 0
    res_id = int(res_id or 0)
    if not res_id:
        raise FixtureError(f"Odoo created no {model} for {ref!r}.")

    db.execute(
        "INSERT INTO review_fixtures (run_id, ref, model, res_id) "
        "VALUES (%s, %s, %s, %s)",
        (ledger.run_id, ref[:120], model, res_id))
    ledger.by_ref[ref] = (model, res_id)
    log.info("run %s created %s#%s as %r", ledger.run_id, model, res_id, ref)
    return res_id


def resolve(conn, ledger: Ledger, *, ref: str, model: str, domain: list) -> int:
    """Point a ref at a record that already exists. Creates nothing.

    The counterpart to `create`, and the reason configuration is on the
    forbidden list. A scenario needs *a* bank journal; it does not need a new
    one. Resolving the client's own means the test runs against their real
    setup, and there is nothing to clean up afterwards — so no ledger row is
    written, because nothing was made.
    """
    try:
        rows = conn.call(model, "search_read", [domain],
                         {"fields": ["id"], "limit": 2, "order": "id"})
    except Exception as exc:  # noqa: BLE001
        raise FixtureError(f"Could not look up {model} for {ref!r}: {exc}") from exc
    if not rows:
        raise FixtureError(
            f"No {model} matches {domain!r} on this instance, so {ref!r} cannot "
            "be resolved. The scenario needs configuration this client does not have.")
    ledger.by_ref[ref] = (model, int(rows[0]["id"]))
    log.info("run %s resolved %r to existing %s#%s",
             ledger.run_id, ref, model, rows[0]["id"])
    return int(rows[0]["id"])


def live_rows(run_id: int) -> list[dict]:
    return db.query(
        "SELECT id, ref, model, res_id FROM review_fixtures "
        "WHERE run_id = %s AND removed_at IS NULL ORDER BY id DESC", (run_id,))


def rollback(conn, run_id: int) -> dict:
    """Remove everything this run created, newest first.

    Newest first because Odoo's own constraints run that way: a line cannot
    outlive the move it belongs to, and removing the parent first turns a clean
    delete into a cascade nobody asked for.

    Never raises. A refusal is recorded and reported, because leaving a record
    behind silently is the failure this whole module exists to avoid.
    """
    removed, archived, refused = 0, 0, []
    for row in live_rows(run_id):
        try:
            conn.call(row["model"], "unlink", [[row["res_id"]]])
        except Exception as exc:  # noqa: BLE001
            message = str(exc)[:400]

            # Odoo will not delete a user who has touched anything, and it is
            # right not to — the audit trail on those records points at them.
            # Archiving is the honest second best: the account can no longer log
            # in, which is the property that actually matters, and the history
            # stays intact. Recorded as archived rather than removed, because
            # the two are not the same fact.
            if row["model"] == "res.users":
                try:
                    conn.call("res.users", "write",
                              [[row["res_id"]], {"active": False}])
                except Exception as exc2:  # noqa: BLE001
                    message = f"{message} — and could not be archived either: {exc2}"
                else:
                    db.execute(
                        "UPDATE review_fixtures SET removed_at = now(), "
                        "remove_error = %s WHERE id = %s",
                        ("Could not be deleted (it owns records), so it was "
                         "archived instead and can no longer sign in.", row["id"]))
                    archived += 1
                    log.info("run %s archived test user #%s (delete refused)",
                             run_id, row["res_id"])
                    continue

            db.execute("UPDATE review_fixtures SET remove_error = %s WHERE id = %s",
                       (message, row["id"]))
            refused.append({"model": row["model"], "id": row["res_id"],
                            "why": message})
            log.warning("run %s could not remove %s#%s: %s",
                        run_id, row["model"], row["res_id"], message)
            continue
        db.execute("UPDATE review_fixtures SET removed_at = now(), remove_error = '' "
                   "WHERE id = %s", (row["id"],))
        removed += 1
    return {"removed": removed, "archived": archived, "refused": refused}


def orphans() -> list[dict]:
    """Records left behind by runs that are no longer active.

    The reason the ledger is in Postgres rather than in memory. Nothing calls
    this automatically — deleting from a client's database on a timer is exactly
    the kind of unattended write this module is careful about — but it is what
    makes "did we leave anything?" an answerable question.
    """
    return db.query(
        """
        SELECT f.id, f.run_id, f.ref, f.model, f.res_id, f.remove_error,
               c.slug, r.state
          FROM review_fixtures f
          JOIN review_runs r ON r.id = f.run_id
          JOIN clients c ON c.id = r.client_id
         WHERE f.removed_at IS NULL
           AND r.state NOT IN ('queued', 'running', 'paused')
         ORDER BY f.created_at DESC
        """)


# ---- test users ------------------------------------------------------------
#
# `res.users` stays in FORBIDDEN_MODELS above, so a plan can never ask for one
# through the generic `create`. This is the only door, and it is narrow on
# purpose.
#
# It exists because of §2 decision 7: access-rights regressions are among the
# most common failures in Odoo work and are *completely invisible to an admin
# session*. A gate that can only sign in as an administrator cannot see them —
# it would report a screen the client's actual salesperson will never be shown.
# When admin is the only credential we are given, making a scoped user and
# looking through their eyes is the only honest way to test.

#: Never granted, whatever a scenario asks for. These two are the difference
#: between "a user who can do a job" and "a user who can do anything", and a
#: review that quietly minted an administrator on a client's instance would be
#: a far worse outcome than a test that could not run.
FORBIDDEN_GROUPS = frozenset({
    "base.group_system",         # Settings — full technical access
    "base.group_erp_manager",    # Administration / access rights
})

#: `.invalid` is reserved by RFC 2606 and can never resolve, so a login built
#: from it cannot receive mail even if a database's neutralisation were undone.
TEST_LOGIN_DOMAIN = "qa-gate.invalid"


@dataclass(frozen=True)
class TestUser:
    uid: int
    login: str
    password: str
    groups: list[str]


def create_test_user(conn, ledger: Ledger, *, ref: str, name: str,
                     groups: list[str]) -> TestUser:
    """Make a scoped user to test access rights as, and record it for removal.

    `groups` are xml ids — `sales_team.group_sale_salesman`, not database ids,
    because ids differ per instance and a number in a plan would be a different
    group on the next client.

    The password is generated here and never stored anywhere but the ledger's
    process memory and the run's own use of it: the point is to open a browser
    session as this user, not to hand anyone a durable account.
    """
    import secrets

    if not name.strip():
        raise FixtureError("A test user needs a name.")

    wanted = [g for g in (groups or []) if isinstance(g, str) and g.strip()]
    refused = [g for g in wanted if g in FORBIDDEN_GROUPS]
    if refused:
        raise FixtureError(
            f"A review may not create a user in {', '.join(refused)}. Those grant "
            "administrative access, and a test that grants itself admin is not a "
            "test of what a real user can see.")
    if not wanted:
        # An internal user with no groups can log in and see almost nothing,
        # which makes every access assertion pass for the wrong reason.
        raise FixtureError(
            f"Test user {ref!r} was given no groups. Name the ones whose access "
            "is being tested, e.g. sales_team.group_sale_salesman.")

    group_ids = []
    for xml_id in wanted:
        module, _, ident = xml_id.partition(".")
        rows = conn.call("ir.model.data", "search_read",
                         [[["module", "=", module], ["name", "=", ident],
                           ["model", "=", "res.groups"]]],
                         {"fields": ["res_id"], "limit": 1})
        if not rows:
            raise FixtureError(
                f"{xml_id} is not a group on this instance, so {ref!r} cannot be "
                "created. The app that defines it may not be installed.")
        group_ids.append(int(rows[0]["res_id"]))

    login = f"qa-gate-r{ledger.run_id}-{ref}@{TEST_LOGIN_DOMAIN}".lower()
    password = secrets.token_urlsafe(18)
    values = {
        "name": f"[QA Gate] {name}",
        "login": login,
        "password": password,
        # Explicit rather than inherited from a template: whatever the client's
        # default new-user groups are, this user gets exactly what was asked for.
        "groups_id": [(6, 0, group_ids)],
        "notification_type": "email",
    }
    try:
        uid = conn.call("res.users", "create", [values])
    except Exception as exc:  # noqa: BLE001
        raise FixtureError(f"Could not create test user {ref!r}: {exc}") from exc
    if isinstance(uid, list):
        uid = uid[0] if uid else 0
    uid = int(uid or 0)
    if not uid:
        raise FixtureError(f"Odoo created no user for {ref!r}.")

    db.execute(
        "INSERT INTO review_fixtures (run_id, ref, model, res_id) "
        "VALUES (%s, %s, 'res.users', %s)",
        (ledger.run_id, ref[:120], uid))
    ledger.by_ref[ref] = ("res.users", uid)
    log.info("run %s created test user %s (uid %s) in %s",
             ledger.run_id, login, uid, ", ".join(wanted))
    return TestUser(uid=uid, login=login, password=password, groups=wanted)
