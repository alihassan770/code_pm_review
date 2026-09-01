"""The pre-flight instance audit — plan §3, shipped standalone as UC-16.

Revision 1 got its safety from infrastructure we owned. Revision 3 has none of
that: there is one staging database, it belongs to the client, it is shared with
humans, and there is no second database to retreat to. What replaces the
isolated network is this — a set of checks that refuse by default.

Eight checks, every one of which refuses the run. They are re-run at the start
of every run and **never cached**, because a hosting provider can re-enable crons
or mail during a platform update without telling anyone, and a cached pass is
then a pass for a state that no longer exists.

Shipped first, and on its own, for the reason UC-16 gives: right now nobody knows
which client staging instances could email real customers. This module needs only
RPC, writes nothing anywhere, and has value even if the rest of the gate is never
built.

## Statuses, and why there are five

    pass     the check ran and the instance is clean
    fail     the check ran and the gate must not run here
    warn     something worth a human's attention that cannot honestly be called
             a refusal on the evidence available
    skipped  the check does not apply, or its inputs arrive in a later phase.
             Named and explained rather than quietly omitted
    error    the check could not be performed. Deliberately not `fail`: "unsafe"
             and "unknown" are different answers, and collapsing them turns an
             outage into a false accusation

The verdict is `refuse` if any check failed, `error` if the instance could not be
reached at all, otherwise `pass`. Warnings never block — a check that blocks on a
heuristic is a check people learn to bypass.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import census as census_mod
from . import db, fingerprint as fp_mod
from .census import Census
from .clients import Client
from .fingerprint import SENTINEL_KEY
from .instance import Connection, MissingCredentials, connect
from .odoo_client import OdooAuthError, OdooError

log = logging.getLogger(__name__)

PASS, FAIL, WARN, SKIPPED, ERROR = "pass", "fail", "warn", "skipped", "error"

VERDICT_PASS, VERDICT_REFUSE, VERDICT_ERROR = "pass", "refuse", "error"

# Config-parameter keys that tend to hold live third-party credentials. A
# heuristic standing in for §3's "per-client list of the endpoints that module
# set touches" — that list arrives with `qa/knowledge.yml` in phase C, at which
# point this check can become a refusal instead of a warning.
INTEGRATION_KEY_PATTERNS = [
    r"api[_.]?key", r"secret", r"token", r"password", r"passwd",
    r"client[_.]?id", r"webhook", r"credential",
    r"stripe", r"paypal", r"adyen", r"quickbooks", r"xero", r"twilio",
    r"sendgrid", r"mailgun", r"mailchimp", r"shopify", r"amazon", r"aws[_.]",
]
_INTEGRATION_RE = re.compile("|".join(INTEGRATION_KEY_PATTERNS), re.I)

# Values that say "this is not pointed at anything live". Matched on the whole
# value, not a substring, so a real key containing the letters "test" is not
# waved through.
_HARMLESS_VALUE_RE = re.compile(
    r"^\s*(|0|false|none|null|test|sandbox|dummy|changeme|xxx+|todo)\s*$", re.I)


@dataclass
class Check:
    """One audit check and its outcome.

    `evidence` carries the records that produced the outcome — the cron names,
    the mail server hosts. §3 requires the bundle to record every check and its
    outcome so that a refusal is self-explanatory; "there are live crons" is a
    verdict, "there are live crons and here are their names" is actionable.
    """
    id: str
    title: str
    status: str
    detail: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return self.status == FAIL


@dataclass
class AuditResult:
    client_id: int
    client_slug: str
    verdict: str
    checks: list[Check] = field(default_factory=list)
    server_version: str = ""
    staging_url: str = ""
    staging_db: str = ""
    error: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int = 0
    id: int | None = None
    # The census is returned but never persisted (§9). Callers that want the
    # coverage map or the drift feed use it and drop it.
    census: Census | None = None
    drift: list = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        return self.verdict == VERDICT_PASS

    def counts(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0, WARN: 0, SKIPPED: 0, ERROR: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out


# ---- running ---------------------------------------------------------------

def run(client: Client, secret_key: str, *, run_by: int | None = None,
        store: bool = True) -> AuditResult:
    """Audit one client staging instance. Read-only, always.

    Connection failure is a result with verdict `error`, not an exception: the
    fleet report's whole job is to be a complete table, and one unreachable
    instance must not take the other thirty-nine down with it.
    """
    started = time.monotonic()
    result = AuditResult(
        client_id=client.id, client_slug=client.slug, verdict=VERDICT_ERROR,
        staging_url=client.staging_url, staging_db=client.staging_db,
    )
    try:
        conn = connect(client, secret_key)
    except MissingCredentials as exc:
        result.error = str(exc)
    except OdooAuthError:
        result.error = (
            "The staging instance rejected the stored credentials. The API key "
            "was probably revoked in Odoo; re-enter it on the client page."
        )
    except OdooError as exc:
        result.error = str(exc)
    else:
        result.server_version = conn.server_version
        census = census_mod.take(conn)
        result.census = census
        result.checks = _all_checks(client, conn, census)
        result.verdict = VERDICT_REFUSE if result.failures else VERDICT_PASS

    result.duration_ms = int((time.monotonic() - started) * 1000)
    if store:
        _persist(result, run_by=run_by)
    return result


def run_fleet(clients: list[Client], secret_key: str, *,
              run_by: int | None = None) -> list[AuditResult]:
    """UC-16: the audit across every instance.

    Sequential on purpose. Auditing forty instances in parallel means forty
    simultaneous RPC sessions against hosts that rate-limit, and the whole sweep
    is seconds per instance — there is no wall-clock problem worth the risk of
    being throttled halfway through and reporting the remainder as errors.
    """
    return [run(c, secret_key, run_by=run_by) for c in clients]


def _all_checks(client: Client, conn: Connection, census: Census) -> list[Check]:
    return [
        _check_sentinel(client, census),
        _check_db_name(client),
        _check_crons(conn),
        _check_mail(conn),
        _check_payment_providers(conn),
        _check_base_url(client, census),
        _check_integration_params(census),
        _check_integration_stubs(census),
    ]


# ---- the eight checks ------------------------------------------------------

def _check_sentinel(client: Client, census: Census) -> Check:
    """Opt-in by sentinel: `staging:<client_id>:<iso8601>:<who>`.

    The point is the direction of the default. An instance has to be walked up
    to and marked by hand, so no configuration mistake on our side can make a
    production instance eligible — a wrong URL produces a refusal, not an
    incident.
    """
    c = Check("sentinel", "Opt-in sentinel present and matches this client", PASS)
    raw = census.config_params.get(SENTINEL_KEY, "")
    if not raw:
        c.status = FAIL
        c.detail = (
            f"No {SENTINEL_KEY} system parameter. Set it on the instance to "
            f"'staging:{client.id}:<iso8601>:<who>' to opt in. Until then the "
            "gate will not run here, which is the intended default."
        )
        return c

    parts = raw.split(":")
    if len(parts) < 2 or parts[0] != "staging":
        c.status = FAIL
        c.detail = (f"Malformed sentinel {raw!r}. Expected "
                    "'staging:<client_id>:<iso8601>:<who>'.")
        return c
    if parts[1] != str(client.id):
        c.status = FAIL
        c.detail = (
            f"The sentinel names client id {parts[1]}, but this is client "
            f"{client.id} ({client.slug}). Either the URL points at the wrong "
            "instance or the sentinel was copied from another one."
        )
        return c
    c.detail = raw
    c.evidence = [raw]
    return c


def _check_db_name(client: Client) -> Check:
    """Belt to the sentinel's braces: the database name must match the pattern."""
    c = Check("db_name", "Database name matches the client's allowlist pattern", PASS)
    pattern = client.db_name_pattern or ""
    if not pattern:
        c.status = WARN
        c.detail = "No allowlist pattern configured, so this check proves nothing."
        return c
    if not client.staging_db:
        c.status = FAIL
        c.detail = "No staging database name is configured for this client."
        return c
    if not matches_pattern(client.staging_db, pattern):
        c.status = FAIL
        c.detail = (f"Database {client.staging_db!r} does not match the allowlist "
                    f"pattern {pattern!r}.")
        return c
    c.detail = f"{client.staging_db} matches {pattern}"
    return c


def matches_pattern(name: str, pattern: str) -> bool:
    """SQL-LIKE-shaped matching, with `_` treated literally.

    In SQL LIKE, `_` matches any single character. Applying that here would make
    the documented example pattern `%_staging` match `clientstaging` — and worse,
    silently widen every allowlist, since Odoo database names contain underscores
    constantly. `%` is the only wildcard, which is what everyone writing one of
    these patterns actually means.
    """
    parts = [re.escape(p) for p in pattern.split("%")]
    return re.fullmatch(".*".join(parts), name or "", re.I) is not None


def _check_crons(conn: Connection) -> Check:
    """A staging instance should not be running scheduled jobs.

    Crons firing mid-run corrupt results, and worse, they can act on records the
    savepoint is about to roll back — the cron's own transaction commits what
    ours was going to undo.
    """
    c = Check("crons", "No active scheduled actions (ir.cron)", PASS)
    try:
        rows = conn.search_read("ir.cron", [("active", "=", True)],
                                ["cron_name", "model_id"], limit=50)
    except (OdooError, OdooAuthError) as exc:
        return _errored(c, exc)
    if rows:
        c.status = FAIL
        c.evidence = [_label(r, "cron_name") for r in rows]
        c.detail = f"{len(rows)} active cron(s). Deactivate them on staging."
    return c


def _check_mail(conn: Connection) -> Check:
    """The check that matters most: this is the one that emails four thousand
    customers if you get it wrong."""
    c = Check("mail", "No active outgoing or incoming mail servers", PASS)
    findings: list[str] = []
    try:
        for model, label in (("ir.mail_server", "outgoing"),
                             ("fetchmail.server", "incoming")):
            if not conn.model_exists(model):
                continue
            rows = conn.search_read(model, [], ["name", "active"], limit=50)
            for r in rows:
                # A model without an `active` field cannot be archived, so a row
                # existing at all means it is live.
                if r.get("active", True):
                    findings.append(f"{label}: {r.get('name') or r.get('id')}")
    except (OdooError, OdooAuthError) as exc:
        return _errored(c, exc)
    if findings:
        c.status = FAIL
        c.evidence = findings
        c.detail = (f"{len(findings)} active mail server(s). This instance can "
                    "send or fetch real mail.")
    return c


def _check_payment_providers(conn: Connection) -> Check:
    c = Check("payment", "No payment provider outside test mode", PASS)
    if not conn.model_exists("payment.provider"):
        c.status = SKIPPED
        c.detail = "No payment module installed on this instance."
        return c
    try:
        rows = conn.search_read("payment.provider", [("state", "!=", "disabled")],
                                ["name", "state", "code"], limit=50)
    except (OdooError, OdooAuthError) as exc:
        return _errored(c, exc)
    live = [r for r in rows if (r.get("state") or "") not in ("test", "disabled")]
    if live:
        c.status = FAIL
        c.evidence = [f"{r.get('name')} ({r.get('state')})" for r in live]
        c.detail = f"{len(live)} provider(s) enabled outside test mode."
    elif rows:
        c.detail = f"{len(rows)} provider(s), all in test mode."
    return c


def _check_base_url(client: Client, census: Census) -> Check:
    """`web.base.url` must agree with the staging URL we were given.

    §3 words this as "resolves to a production hostname, checked against the
    client's known production domain". We check against the configured staging
    host instead, which needs no extra per-client field and catches strictly
    more: an instance whose base URL is anything other than where we think we
    are talking to is either a production restore or a misconfiguration, and
    both are reasons to stop. Mail links, portal invitations and report URLs are
    all generated from this value.
    """
    c = Check("base_url", "web.base.url points at this staging instance", PASS)
    base = census.config_params.get("web.base.url", "")
    if not base:
        c.status = WARN
        c.detail = "web.base.url is not set on this instance."
        return c
    c.evidence = [base]
    configured = _host(client.staging_url)
    actual = _host(base)
    if not configured:
        c.status = WARN
        c.detail = f"web.base.url is {base}, but no staging URL is configured to compare it to."
        return c
    if actual != configured:
        c.status = FAIL
        c.detail = (
            f"web.base.url is {base}, but the gate reaches this instance at "
            f"{client.staging_url}. A database restored from production keeps "
            "the production URL, and every link it generates would point there."
        )
        return c
    c.detail = base
    return c


def _check_integration_params(census: Census) -> Check:
    """System parameters that look like live third-party credentials.

    A warning rather than a refusal, and deliberately so: without the per-client
    endpoint list from `qa/knowledge.yml` (phase C) this is pattern matching on
    key names, and a check that refuses on a heuristic is a check people learn
    to bypass. It becomes a refusal when it has that list to compare against.
    """
    c = Check("integration_params",
              "No system parameters holding what look like live credentials", PASS)
    suspicious = [
        k for k, v in sorted(census.config_params.items())
        if _INTEGRATION_RE.search(k)
        and not k.startswith(census_mod.OUR_PARAM_PREFIX)
        and not _HARMLESS_VALUE_RE.match(v or "")
    ]
    if suspicious:
        c.status = WARN
        c.evidence = suspicious[:25]
        c.detail = (f"{len(suspicious)} parameter(s) match integration key patterns "
                    "and hold a non-placeholder value. Confirm each points at a "
                    "sandbox before running anything that touches them.")
    return c


def _check_integration_stubs(census: Census) -> Check:
    """Modules declaring an outbound integration with no stub registered.

    Skipped, and named rather than dropped. The check needs two things that do
    not exist yet: the manifest convention from §3 (a module that talks to the
    outside world declares it and ships a stub descriptor) and the stub registry
    the probe consults, which arrives in phase E. What it can do today is say
    which installed modules look like connectors, so the list of manifests
    somebody has to go and annotate is already sitting there.
    """
    c = Check("integration_stubs",
              "Every module declaring an outbound integration has a stub", SKIPPED)
    likely = sorted(
        m.name for m in census.custom_modules
        if _INTEGRATION_RE.search(m.name) or "connector" in m.name.lower()
    )
    c.evidence = likely
    c.detail = (
        "Needs the manifest stub convention (§3) and the probe's stub registry, "
        "which arrive in phase E. "
        + (f"{len(likely)} installed module(s) look like connectors and will need "
           "a stub descriptor." if likely else
           "No installed module looks like a connector by name.")
    )
    return c


def _errored(c: Check, exc: Exception) -> Check:
    c.status = ERROR
    c.detail = f"Could not run this check: {exc}"
    return c


def _label(row: dict, *fields: str) -> str:
    for f in fields:
        if row.get(f):
            return str(row[f])
    return f"id {row.get('id')}"


def _host(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "//" in url else f"//{url}")
    return (parsed.hostname or "").lower()


# ---- persistence -----------------------------------------------------------
#
# Audits are stored, censuses are not. An audit is a decision we made about a
# live client instance at a point in time, and being able to answer "was this
# instance clean when we ran on the 4th" after the fact is the difference
# between an audit trail and a log line.

def _persist(result: AuditResult, *, run_by: int | None) -> None:
    row = db.query_one(
        """
        INSERT INTO instance_audits
            (client_id, verdict, checks, server_version, staging_url, staging_db,
             error, started_at, finished_at, duration_ms, run_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s)
        RETURNING id
        """,
        (result.client_id, result.verdict,
         json.dumps([asdict(c) for c in result.checks]),
         result.server_version, result.staging_url, result.staging_db,
         result.error, result.started_at, result.duration_ms, run_by),
    )
    result.id = int(row["id"])

    if result.census is not None:
        fp = fp_mod.compute(result.census)
        previous = fp_mod.latest(result.client_id)
        result.drift = fp_mod.diff(previous, fp)
        fp_mod.record(result.client_id, fp, audit_id=result.id)


def latest_for(client_id: int) -> dict | None:
    return db.query_one(
        "SELECT * FROM instance_audits WHERE client_id = %s "
        "ORDER BY started_at DESC LIMIT 1",
        (client_id,),
    )


def history_for(client_id: int, limit: int = 20) -> list[dict]:
    return db.query(
        "SELECT * FROM instance_audits WHERE client_id = %s "
        "ORDER BY started_at DESC LIMIT %s",
        (client_id, limit),
    )


def latest_by_client() -> dict[int, dict]:
    """The newest audit per client, for the fleet table.

    DISTINCT ON rather than a window function or a per-client query: it is the
    Postgres idiom for exactly this, and the fleet page would otherwise issue
    one query per client to render one table.
    """
    rows = db.query(
        "SELECT DISTINCT ON (client_id) * FROM instance_audits "
        "ORDER BY client_id, started_at DESC"
    )
    return {int(r["client_id"]): r for r in rows}


def checks_of(row: dict) -> list[Check]:
    """Rehydrate stored checks. jsonb comes back as a list of dicts."""
    return [Check(**c) for c in (row.get("checks") or [])]
