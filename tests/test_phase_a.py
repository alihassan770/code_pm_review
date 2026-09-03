"""End-to-end exercise of Phase A against a real Postgres and a fake Odoo."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import fake_odoo

PORT = 8899
fake_odoo.serve(PORT)

# Isolated config + isolated database, so nothing here touches the dev instance.
tmp = tempfile.mkdtemp(prefix="qa-gate-test-")
os.environ["QA_GATE_CONFIG"] = str(Path(tmp) / "config.yaml")
os.environ["QA_GATE_STATE_DIR"] = str(Path(tmp) / "state")

from dataclasses import replace  # noqa: E402
from qa_gate import config as config_mod, db  # noqa: E402

TEST_DB = "postgresql://odoo@/odoo_qa_gate_test"
cfg = config_mod.load()
config_mod.save(replace(cfg, database_url=TEST_DB))

from fastapi.testclient import TestClient  # noqa: E402
from qa_gate import clients as clients_mod_probe  # noqa: E402
from qa_gate.web.app import create_app  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


with TestClient(create_app(), follow_redirects=False) as c:
    print("\n== setup ==")
    r = c.get("/")
    check("/ redirects to /setup when unconfigured", r.headers.get("location") == "/setup")

    r = c.post("/setup", data={"url": "http://127.0.0.1:9", "db": "nope"})
    check("setup rejects an unreachable Odoo", "Could not reach" in r.text or "did not return JSON" in r.text)

    r = c.post("/setup", data={"url": "ftp://x", "db": "d"})
    check("setup rejects a non-http URL", "http://" in r.text and "must start with" in r.text)

    r = c.post("/setup", data={"url": f"http://127.0.0.1:{PORT}", "db": fake_odoo.DB})
    check("setup accepts a reachable Odoo", r.status_code == 303 and r.headers["location"] == "/login")

    print("\n== fixing a wrong connection ==")
    # Nobody has an account yet, so there is nothing to take over and /setup stays
    # reachable. This is exactly the case where a wrong database was saved and the
    # operator cannot log in to correct it.
    r = c.get("/setup")
    check("setup stays open while no user exists", r.status_code == 200)
    check("setup prefills the current values", fake_odoo.DB in r.text)
    check("setup reads as a fix, not a first run", "Fix the Odoo connection" in r.text)

    r = c.post("/setup", data={"url": f"http://127.0.0.1:{PORT}", "db": "no-such-db"})
    check("a wrong database is refused, not saved", "no database named" in r.text)

    r = c.get("/login")
    check("login names the database in use", fake_odoo.DB in r.text)
    check("login offers a way to change the connection", "Change it" in r.text)

    print("\n== login ==")
    r = c.get("/dashboard")
    check("anonymous /dashboard redirects with next", r.headers.get("location") == "/login?next=/dashboard")

    r = c.post("/login", data={"login": "hamza", "password": "wrong", "next": "/dashboard"})
    check("bad password is rejected", "Odoo rejected those credentials" in r.text)
    check("rejection mentions the 2FA/API-key rule", "two-factor" in r.text)
    check("no cookie set on failure", "qa_gate_session" not in r.cookies)

    r = c.post("/login", data={"login": "twofa", "password": "anything", "next": "/dashboard"})
    check("2FA user cannot log in with a password", "Odoo rejected" in r.text)

    r = c.post("/login", data={"login": "twofa", "password": "key-twofa", "next": "/dashboard"})
    check("2FA user CAN log in with an API key", r.status_code == 303)
    c.cookies.clear()

    r = c.post("/login", data={"login": "hamza", "password": "hunter2", "next": "/clients"})
    check("valid login redirects to next", r.status_code == 303 and r.headers["location"] == "/clients")
    cookie = c.cookies.get("qa_gate_session")
    check("session cookie issued", bool(cookie))

    print("\n== identity persisted ==")
    db.init_pool(TEST_DB)
    row = db.query_one("SELECT * FROM users WHERE login = 'hamza'")
    check("user row created from Odoo", row is not None and row["odoo_uid"] == 7)
    check("admin flag read via has_group", bool(row and row["is_admin"]))
    srow = db.query_one("SELECT * FROM sessions LIMIT 1")
    check("session stored hashed, not raw", srow is not None and cookie.encode() not in bytes(srow["token_hash"]))

    print("\n== setup locks down once an account exists ==")
    saved = dict(c.cookies)
    c.cookies.clear()
    check("setup closed to anonymous visitors", c.get("/setup").status_code == 303)
    check("login hides the change link from anonymous", "Change it" not in c.get("/login").text)
    c.cookies.update(saved)
    check("setup still open to an admin", c.get("/setup").status_code == 200)

    print("\n== authenticated pages ==")
    r = c.get("/dashboard")
    check("dashboard renders", r.status_code == 200 and "Dashboard" in r.text)
    check("dashboard shows empty state", "No clients yet" in r.text)

    print("\n== clients ==")
    r = c.get("/clients/new")
    token = csrf_of(r.text)

    r = c.post("/clients/new", data={
        "csrf_token": "forged", "slug": "lmm", "name": "Legacy Maker Meats"})
    check("CSRF rejected", r.status_code == 400)

    r = c.post("/clients/new", data={
        "csrf_token": token, "slug": "Bad Slug!", "name": "X"})
    check("bad slug rejected", "Slug must be" in r.text)

    # GitHub moved onto per-repo rows, so a bad value is rejected there now.
    r = c.post("/clients/new", data={
        "csrf_token": token, "slug": "lmm", "name": "Legacy Maker Meats",
        "repo_github": "not-a-repo", "action": "save"})
    check("bad github rejected", "owner/name" in r.text)
    check("a rejected repo does not leave a half-made client",
          clients_mod_probe.get_by_slug("lmm") is None
          or "Client created, but" in r.text)

    r = c.post("/clients/new", data={
        "csrf_token": token, "slug": "lmm", "name": "Legacy Maker Meats",
        "repo_github": "hsxtech/legacymakermeats", "odoo_version": "18.0",
        "hosting_platform": "cloudpepper",
        "staging_url": f"http://127.0.0.1:{PORT}", "staging_db": fake_odoo.DB,
        "db_name_pattern": "%_staging", "branch_mode": "per_task", "base_branch": "main"})
    check("client created", r.status_code == 303, r.text[:200])
    cid = int(r.headers["location"].rsplit("/", 1)[1])

    r = c.post("/clients/new", data={"csrf_token": token, "slug": "lmm", "name": "Dup"})
    check("duplicate slug rejected", "already exists" in r.text)

    r = c.get("/dashboard")
    check("client appears on dashboard", "Legacy Maker Meats" in r.text)
    # The wording changed with migration 008: a client can be reachable by a
    # browser sign-in OR an API key, so "credentials" is no longer one thing.
    check("dashboard warns about missing credentials",
          "until it can reach its staging instance" in r.text)

    print("\n== credentials ==")
    r = c.get(f"/clients/{cid}")
    token = csrf_of(r.text)
    r = c.post(f"/clients/{cid}/credentials", data={
        "csrf_token": token, "rpc_login": "hamza", "rpc_api_key": "wrong-key"})
    check("wrong staging key rejected", "rejected those credentials" in r.text)

    r = c.post(f"/clients/{cid}/credentials", data={
        "csrf_token": token, "rpc_login": "hamza", "rpc_api_key": "key-hamza"})
    check("valid staging key stored", r.status_code == 303)

    enc = db.query_one("SELECT rpc_api_key_enc FROM instance_secrets WHERE client_id=%s", (cid,))
    check("api key encrypted at rest", "key-hamza" not in (enc["rpc_api_key_enc"] or ""))

    from qa_gate import clients as clients_mod
    cfg2 = config_mod.load()
    login, key = clients_mod.get_rpc_credentials(cid, cfg2.secret_key)
    check("api key decrypts back", (login, key) == ("hamza", "key-hamza"))

    r = c.get(f"/clients/{cid}")
    check("stored key never rendered back", "key-hamza" not in r.text)
    check("detail shows credentials stored", "credentials stored" in r.text)


    print("\n== logout ==")
    r = c.get("/dashboard")
    token = csrf_of(r.text)
    r = c.post("/logout", data={"csrf_token": token})
    check("logout redirects to login", r.status_code == 303)
    r = c.get("/dashboard")
    check("session no longer valid", r.headers.get("location", "").startswith("/login"))

print(f"\n{'='*60}\n  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("  failed:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
