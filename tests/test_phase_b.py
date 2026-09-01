"""End-to-end exercise of Phase B: the census, the fingerprint, and the audit.

Same shape as test_phase_a: a real Postgres, real HTTP, and a fake instance —
here two of them, because phase B is the first code that talks to something
other than our own identity Odoo. The point of the fake staging instance is that
a test can make an instance dirty and watch the verdict flip, which is the only
way to be sure a check is actually checking.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import fake_odoo
import fake_staging

IDENTITY_PORT = 8898
STAGING_PORT = 8901
fake_odoo.serve(IDENTITY_PORT)
fake_staging.serve(STAGING_PORT)

tmp = tempfile.mkdtemp(prefix="qa-gate-test-b-")
os.environ["QA_GATE_CONFIG"] = str(Path(tmp) / "config.yaml")
os.environ["QA_GATE_STATE_DIR"] = str(Path(tmp) / "state")

from dataclasses import replace  # noqa: E402

from qa_gate import config as config_mod, db  # noqa: E402

TEST_DB = os.environ.get("QA_GATE_TEST_DB", "postgresql://odoo@/odoo_qa_gate_test")
cfg = config_mod.load()
config_mod.save(replace(cfg, database_url=TEST_DB))

from fastapi.testclient import TestClient  # noqa: E402

from qa_gate import audit as audit_mod  # noqa: E402
from qa_gate import census as census_mod  # noqa: E402
from qa_gate import clients as clients_mod  # noqa: E402
from qa_gate import fingerprint as fp_mod, instance  # noqa: E402
from qa_gate.web.app import create_app  # noqa: E402

PASS, FAIL = [], []
SLUG = "acme-phase-b"
STAGING_URL = f"http://127.0.0.1:{STAGING_PORT}"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def status_of(result, check_id):
    return next(c.status for c in result.checks if c.id == check_id)


# ---- pure functions, no server needed --------------------------------------

print("\n== allowlist pattern ==")
check("%_staging matches acme_staging", audit_mod.matches_pattern("acme_staging", "%_staging"))
check("%_staging rejects acme_prod", not audit_mod.matches_pattern("acme_prod", "%_staging"))
check("underscore is literal, not a wildcard",
      not audit_mod.matches_pattern("acmestaging", "%_staging"))
check("staging_% matches staging_acme", audit_mod.matches_pattern("staging_acme", "staging_%"))
check("a pattern without % is an exact match",
      audit_mod.matches_pattern("acme_staging", "acme_staging")
      and not audit_mod.matches_pattern("acme_staging2", "acme_staging"))


with TestClient(create_app(), follow_redirects=False) as c:
    fake_staging.STATE.reset()

    print("\n== sign in ==")
    r = c.get("/audit")
    check("anonymous /audit redirects to login", r.headers.get("location") == "/login?next=/audit")

    if not config_mod.load().odoo.configured:
        c.post("/setup", data={"url": f"http://127.0.0.1:{IDENTITY_PORT}", "db": fake_odoo.DB})
    r = c.post("/login", data={"login": "hamza", "password": "hunter2", "next": "/dashboard"})
    check("staff login works", r.status_code == 303)

    print("\n== a client pointing at the fake staging instance ==")
    existing = clients_mod.get_by_slug(SLUG)
    if existing:
        db.execute("DELETE FROM clients WHERE id = %s", (existing.id,))
    token = csrf_of(c.get("/clients/new").text)
    r = c.post("/clients/new", data={
        "csrf_token": token, "slug": SLUG, "name": "Acme Phase B",
        "odoo_version": "17.0", "hosting_platform": "odoo_sh",
        "staging_url": STAGING_URL, "staging_db": fake_staging.DB,
        "db_name_pattern": "%_staging", "branch_mode": "per_task", "base_branch": "main",
    })
    check("client created", r.status_code == 303, r.text[:200])
    client_id = int(r.headers["location"].rsplit("/", 1)[1])

    token = csrf_of(c.get(f"/clients/{client_id}").text)
    r = c.post(f"/clients/{client_id}/credentials", data={
        "csrf_token": token, "rpc_login": fake_staging.LOGIN,
        "rpc_api_key": fake_staging.API_KEY,
    })
    check("staging credentials verified and stored", r.status_code == 303, r.text[:300])

    client = clients_mod.get(client_id)
    secret = config_mod.load().secret_key

    print("\n== census ==")
    conn = instance.connect(client, secret)
    census = census_mod.take(conn)
    check("server version read", census.server_version.startswith("17."))
    check("modules read", len(census.modules) == 3)
    check("core modules are separated from custom ones",
          {m.name for m in census.custom_modules} == {"hst_kill_sheet", "muk_web_theme"},
          str([m.name for m in census.custom_modules]))
    check("manual/Studio fields found, base fields excluded",
          [f["name"] for f in census.manual_fields] == ["x_pen_number"])
    check("view count read", census.view_count == 3)
    check("view patches attributed to their owning module",
          {p.module for p in census.view_patches} == {"hst_kill_sheet", "muk_web_theme"})
    conflicts = census.conflicts()
    check("two modules patching one view is reported as a conflict",
          len(conflicts) == 1 and conflicts[0]["modules"] == ["hst_kill_sheet", "muk_web_theme"],
          str(conflicts))
    check("census records no gaps on a healthy instance", census.gaps == {}, str(census.gaps))

    print("\n== audit refuses an instance that has not opted in ==")
    result = audit_mod.run(client, secret)
    check("verdict is refuse", result.verdict == audit_mod.VERDICT_REFUSE, result.verdict)
    check("the missing sentinel is the reason", status_of(result, "sentinel") == audit_mod.FAIL)
    check("all eight checks are recorded", len(result.checks) == 8, str(len(result.checks)))
    check("passing checks are recorded too, not just failures",
          any(x.status == audit_mod.PASS for x in result.checks))
    check("the stub check is skipped and says why",
          status_of(result, "integration_stubs") == audit_mod.SKIPPED
          and "phase E" in next(x.detail for x in result.checks if x.id == "integration_stubs"))

    print("\n== a sentinel for the wrong client is still a refusal ==")
    fake_staging.STATE.set_param(fp_mod.SENTINEL_KEY,
                                 f"staging:{client_id + 999}:2026-09-01T10:00:00:hamza")
    result = audit_mod.run(client, secret)
    check("a sentinel naming another client refuses",
          status_of(result, "sentinel") == audit_mod.FAIL)
    check("the refusal names both client ids",
          str(client_id) in next(x.detail for x in result.checks if x.id == "sentinel"))

    print("\n== a clean, opted-in instance passes ==")
    fake_staging.STATE.set_param(fp_mod.SENTINEL_KEY,
                                 f"staging:{client_id}:2026-09-01T10:00:00:hamza")
    result = audit_mod.run(client, secret)
    check("verdict is pass", result.verdict == audit_mod.VERDICT_PASS,
          str([(x.id, x.status, x.detail) for x in result.checks if x.status != audit_mod.PASS]))
    check("payment providers in test mode do not block",
          status_of(result, "payment") == audit_mod.PASS)
    check("web.base.url matching the staging URL passes",
          status_of(result, "base_url") == audit_mod.PASS)

    print("\n== each dirty condition is caught ==")
    fake_staging.STATE.add("ir.cron", cron_name="Nightly invoicing", active=True)
    result = audit_mod.run(client, secret)
    check("an active cron refuses the run", status_of(result, "crons") == audit_mod.FAIL)
    check("the cron is named in the evidence",
          "Nightly invoicing" in next(x.evidence for x in result.checks if x.id == "crons"))

    fake_staging.STATE.add("ir.mail_server", name="Live SMTP", active=True)
    result = audit_mod.run(client, secret)
    check("an active mail server refuses the run", status_of(result, "mail") == audit_mod.FAIL)

    fake_staging.STATE.add("payment.provider", name="Stripe live", state="enabled",
                           code="stripe")
    result = audit_mod.run(client, secret)
    check("a payment provider outside test mode refuses the run",
          status_of(result, "payment") == audit_mod.FAIL)

    fake_staging.STATE.set_param("web.base.url", "https://erp.acme.com")
    fake_staging.STATE.set_param("stripe_secret_key", "sk_live_abcdef")
    result = audit_mod.run(client, secret)
    check("a production web.base.url refuses the run",
          status_of(result, "base_url") == audit_mod.FAIL)
    check("live-looking integration parameters warn rather than block",
          status_of(result, "integration_params") == audit_mod.WARN)
    check("four failures are reported together, not one at a time",
          len(result.failures) == 4, str([x.id for x in result.failures]))
    check("verdict is refuse", result.verdict == audit_mod.VERDICT_REFUSE)

    print("\n== unreachable and unauthorised instances are 'unknown', not 'unsafe' ==")
    broken = clients_mod.update(client_id, name="Acme Phase B",
                                staging_url="http://127.0.0.1:9",
                                staging_db=fake_staging.DB, db_name_pattern="%_staging",
                                odoo_version="17.0", hosting_platform="odoo_sh",
                                branch_mode="per_task", base_branch="main")
    result = audit_mod.run(broken, secret)
    check("an unreachable instance is an error verdict",
          result.verdict == audit_mod.VERDICT_ERROR, result.verdict)
    check("the error explains what could not be reached", "127.0.0.1:9" in result.error)
    clients_mod.update(client_id, name="Acme Phase B", staging_url=STAGING_URL,
                       staging_db=fake_staging.DB, db_name_pattern="%_staging",
                       odoo_version="17.0", hosting_platform="odoo_sh",
                       branch_mode="per_task", base_branch="main")
    client = clients_mod.get(client_id)

    print("\n== the database allowlist is enforced independently of the sentinel ==")
    clients_mod.update(client_id, name="Acme Phase B", staging_url=STAGING_URL,
                       staging_db=fake_staging.DB, db_name_pattern="prod_%",
                       odoo_version="17.0", hosting_platform="odoo_sh",
                       branch_mode="per_task", base_branch="main")
    result = audit_mod.run(clients_mod.get(client_id), secret)
    check("a database outside the allowlist refuses",
          status_of(result, "db_name") == audit_mod.FAIL)
    clients_mod.update(client_id, name="Acme Phase B", staging_url=STAGING_URL,
                       staging_db=fake_staging.DB, db_name_pattern="%_staging",
                       odoo_version="17.0", hosting_platform="odoo_sh",
                       branch_mode="per_task", base_branch="main")
    client = clients_mod.get(client_id)

    print("\n== fingerprint and drift ==")
    stored = fp_mod.latest(client_id)
    check("an audit records a fingerprint", stored is not None)
    check("the fingerprint payload can explain itself",
          "hst_kill_sheet" in (stored["payload"] or {}).get("modules", {}))
    fake_staging.STATE.add("ir.module.module", name="hr_attendance_geolocation",
                           latest_version="17.0.1.0.2", state="installed",
                           author="Someone Else", shortdesc="Geo")
    conn = instance.connect(client, secret)
    drift = fp_mod.diff(stored, fp_mod.compute(census_mod.take(conn)))
    kinds = {(d.kind, d.subject) for d in drift}
    check("a module installed behind our back shows up as drift",
          ("module_added", "hr_attendance_geolocation") in kinds, str(kinds))
    check("a first fingerprint reports no drift", fp_mod.diff(None, fp_mod.compute(census)) == [])

    print("\n== the pages ==")
    r = c.get("/audit")
    check("the fleet report lists the client", "Acme Phase B" in r.text)
    check("the fleet report shows the refusal", "refuses" in r.text)

    r = c.get(f"/clients/{client_id}/audit")
    check("the client audit page renders every check",
          all(x in r.text for x in ["Opt-in sentinel", "No active scheduled actions",
                                    "No active outgoing or incoming mail servers"]))
    check("the audit page states the verdict in words",
          "refuse to run against this instance" in r.text)

    r = c.get(f"/clients/{client_id}/census")
    check("the census page renders", "hst_kill_sheet" in r.text and "x_pen_number" in r.text)
    check("the census page shows the view conflict", "muk_web_theme" in r.text)

    r = c.get(f"/clients/{client_id}/team")
    check("the team page lists the person who created the client", "Hamza Q" in r.text)
    token = csrf_of(r.text)
    r = c.post(f"/clients/{client_id}/team",
               data={"csrf_token": token, "action": "remove",
                     "user_id": str(clients_mod.team_of(client_id)[0]["id"])})
    check("someone can be detached", clients_mod.team_of(client_id) == [])
    r = c.post(f"/clients/{client_id}/team", data={"csrf_token": "wrong", "action": "remove",
                                                   "user_id": "1"})
    check("the team form is CSRF protected", r.status_code == 400)

    print("\n== running the audit from the web ==")
    token = csrf_of(c.get(f"/clients/{client_id}/audit").text)
    before = len(audit_mod.history_for(client_id))
    r = c.post(f"/clients/{client_id}/audit", data={"csrf_token": token})
    check("the audit button runs an audit",
          r.status_code == 303 and len(audit_mod.history_for(client_id)) == before + 1)

    # Leave the test database as we found it.
    db.execute("DELETE FROM clients WHERE id = %s", (client_id,))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("Failed: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
