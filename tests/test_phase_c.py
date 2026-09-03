"""End-to-end exercise of Phase C: the client repo, the overlay, the coverage map.

Three fakes now — identity Odoo, a client staging instance, and the GitHub API —
because the coverage map is the first thing in the app that is a join between
the instance and the repo, and it is only worth testing where both halves are
real enough to disagree with each other.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import fake_github
import fake_odoo
import fake_staging

IDENTITY_PORT = 8897
STAGING_PORT = 8903
GITHUB_PORT = 8902
fake_odoo.serve(IDENTITY_PORT)
fake_staging.serve(STAGING_PORT)
fake_github.serve(GITHUB_PORT)
API_ROOT = f"http://127.0.0.1:{GITHUB_PORT}"

tmp = tempfile.mkdtemp(prefix="qa-gate-test-c-")
os.environ["QA_GATE_CONFIG"] = str(Path(tmp) / "config.yaml")
os.environ["QA_GATE_STATE_DIR"] = str(Path(tmp) / "state")

from dataclasses import replace  # noqa: E402

from qa_gate import config as config_mod, db  # noqa: E402

TEST_DB = os.environ.get("QA_GATE_TEST_DB", "postgresql://odoo@/odoo_qa_gate_test")
cfg = config_mod.load()
config_mod.save(replace(cfg, database_url=TEST_DB))

from fastapi.testclient import TestClient  # noqa: E402

from qa_gate import census as census_mod  # noqa: E402
from qa_gate import clients as clients_mod  # noqa: E402
from qa_gate import coverage as coverage_mod  # noqa: E402
from qa_gate import github as github_mod  # noqa: E402
from qa_gate import instance  # noqa: E402
from qa_gate import knowledge as knowledge_mod  # noqa: E402
from qa_gate import repo_sync, scenarios as scenarios_mod  # noqa: E402
from qa_gate.web.app import create_app  # noqa: E402

PASS, FAIL = [], []
SLUG = "acme-phase-c"
STAGING_URL = f"http://127.0.0.1:{STAGING_PORT}"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


# ---- parsing, no server needed ---------------------------------------------

print("\n== qa/knowledge.yml ==")
k = knowledge_mod.parse(fake_github.KNOWLEDGE)
check("no parse errors on a good file", k.errors == [], str(k.errors))
check("invariants and danger zones are separate",
      len(k.invariants) == 2 and len(k.danger_zones) == 1)
check("scope survives as a mapping",
      k.invariants[0].scope == {"models": ["mrp.production"],
                                "modules": ["hst_kill_sheet"]})
check("an unquoted YAML date parses", k.invariants[0].last_confirmed == date(2026, 8, 12))
check("expected_values are read", k.expected_values["sales_tax_default"] == 8.25)
check("unused_apps are read", k.unused_apps == ["website_sale"])
check("only the entry past review_after is stale",
      [e.id for e in k.stale(date(2026, 9, 1))] == ["INV-02"],
      str([e.id for e in k.stale(date(2026, 9, 1))]))
check("a stale entry still applies rather than disappearing",
      any(e.id == "INV-02" for e in k.entries))

bad = knowledge_mod.parse("invariants:\n  - id: A\n   text: broken indent\n")
check("malformed YAML is an error, not a crash", bad.errors and "not valid YAML" in bad.errors[0])

partial = knowledge_mod.parse("""
invariants:
  - id: OK-1
    text: This one is fine.
  - text: This one has no id.
  - id: NO-TEXT
danger_zones:
  - id: OK-1
    text: A duplicate id, on purpose.
""")
check("a bad entry does not discard the good ones",
      [e.id for e in partial.invariants] == ["OK-1"], str(partial.errors))
check("a missing id is reported", any("no id" in e for e in partial.errors))
check("an entry with no text is reported", any("says nothing" in e for e in partial.errors))
check("a duplicate id is reported", any("Duplicate" in e for e in partial.errors))

print("\n== qa/scenarios ==")
s = scenarios_mod.parse("qa/scenarios/mrp/kill_sheet_totals.yml", fake_github.SCENARIO_OK)
check("a good scenario has no errors", s.valid, str(s.errors))
check("tier is read", s.tier == 2)
check("models are found wherever they appear", s.models == ["mrp.production"], str(s.models))
check("the ratified tag is recognised", s.ratified)

no_tier = scenarios_mod.parse("x.yml", fake_github.SCENARIO_NO_TIER)
check("a scenario with no tier is invalid", not no_tier.valid)
check("the tier error explains why there is no default",
      any("no default" in e for e in no_tier.errors), str(no_tier.errors))

tier4 = scenarios_mod.parse("x.yml", fake_github.SCENARIO_TIER_4)
check("tier 4 is rejected", not tier4.valid)
check("the tier 4 error explains that revision 3 removed it",
      any("Revision 3" in e or "revision 3" in e for e in tier4.errors), str(tier4.errors))

print("\n== reading a tree ==")
entries = [github_mod.TreeEntry(p, "sha", len(c)) for p, c in fake_github.FILES.items()]
mods = github_mod.module_dirs(entries)
check("a directory with __manifest__.py is a module",
      set(mods) == {"hst_kill_sheet", "hst_lot_weight"}, str(mods))
know_entry, scen_entries = github_mod.qa_files(entries)
check("qa/knowledge.yml is found", know_entry is not None)
check("only .yml files under qa/scenarios/ are scenarios",
      len(scen_entries) == 3 and all(e.path.endswith(".yml") for e in scen_entries),
      str([e.path for e in scen_entries]))


with TestClient(create_app(), follow_redirects=False) as c:
    fake_staging.STATE.reset()
    fake_github.STATE.reset()

    print("\n== sign in ==")
    # Before the cookie exists, so the jar cannot make this pass by accident.
    r = c.get("/clients/1/coverage")
    check("the coverage page needs a session",
          r.headers.get("location") == "/login?next=/clients/1/coverage",
          str(r.headers.get("location")))
    r = c.get("/clients/1/knowledge")
    check("the knowledge page needs a session",
          r.headers.get("location") == "/login?next=/clients/1/knowledge")

    if not config_mod.load().odoo.configured:
        c.post("/setup", data={"url": f"http://127.0.0.1:{IDENTITY_PORT}", "db": fake_odoo.DB})
    r = c.post("/login", data={"login": "hamza", "password": "hunter2", "next": "/dashboard"})
    check("staff login works", r.status_code == 303)

    existing = clients_mod.get_by_slug(SLUG)
    if existing:
        db.execute("DELETE FROM clients WHERE id = %s", (existing.id,))
    token = csrf_of(c.get("/clients/new").text)
    r = c.post("/clients/new", data={
        "csrf_token": token, "slug": SLUG, "name": "Acme Phase C",
        "odoo_version": "17.0", "hosting_platform": "odoo_sh",
        "repo_github": fake_github.REPO, "repo_base_branch": "main",
        "staging_url": STAGING_URL, "staging_db": fake_staging.DB,
        "db_name_pattern": "%_staging", "action": "save",
    })
    client_id = int(r.headers["location"].rsplit("/", 1)[1])
    token = csrf_of(c.get(f"/clients/{client_id}").text)
    c.post(f"/clients/{client_id}/credentials", data={
        "csrf_token": token, "rpc_login": fake_staging.LOGIN,
        "rpc_api_key": fake_staging.API_KEY})
    client = clients_mod.get(client_id)
    check("client created with a GitHub repo", client.github == fake_github.REPO)

    print("\n== syncing the repo ==")
    snap = repo_sync.fetch(client, api_root=API_ROOT)
    check("the head sha is resolved", snap.commit_sha == fake_github.HEAD)
    check("everything is read at one commit, not at a branch name",
          snap.ok and snap.error == "")
    check("modules are discovered", set(snap.modules) == {"hst_kill_sheet", "hst_lot_weight"})
    check("knowledge is parsed", len(snap.knowledge.invariants) == 2)
    check("scenarios are indexed", len(snap.scenarios) == 3)
    check("the invalid scenarios are reported, not dropped",
          len(snap.scenario_errors) == 2, str([s.path for s in snap.scenario_errors]))
    check("last-changed is looked up per module",
          snap.last_changed["hst_lot_weight"][0].year == 2026)

    print("\n== the cache is a cache, not a second source of truth ==")
    repo_sync.save(client_id, snap)
    loaded = repo_sync.load(client_id)
    check("the sha it came from is stored", loaded.commit_sha == snap.commit_sha)
    check("dates survive the round trip",
          loaded.knowledge.invariants[0].last_confirmed == date(2026, 8, 12))
    check("scenarios survive the round trip",
          {s.id for s in loaded.scenarios} == {s.id for s in snap.scenarios})
    check("stale detection is quiet when the branch has not moved",
          repo_sync.stale(client, loaded, api_root=API_ROOT) is None)
    fake_github.STATE.head = "b" * 40
    note = repo_sync.stale(client, loaded, api_root=API_ROOT)
    check("a moved branch is reported rather than silently followed",
          note is not None and "has moved" in note, str(note))
    fake_github.STATE.head = fake_github.HEAD

    print("\n== a repo that is not there ==")
    missing = clients_mod.update(client_id, github="hsxtech/nope")
    bad_snap = repo_sync.fetch(missing, api_root=API_ROOT)
    check("a 404 is an error on the snapshot, not an exception", bad_snap.error != "")
    check("the error says what was looked for",
          "hsxtech/nope" in bad_snap.error and "main" in bad_snap.error, bad_snap.error)
    clients_mod.update(client_id, github=fake_github.REPO)
    client = clients_mod.get(client_id)

    print("\n== contradiction detection ==")
    conn = instance.connect(client, config_mod.load().secret_key)
    census = census_mod.take(conn)
    check("the census attributes fields to their owning module",
          census.module_models.get("hst_kill_sheet") == ["mrp.production"],
          str(census.module_models))
    found = knowledge_mod.contradictions(snap.knowledge, census)
    ids = {(c_.entry_id, c_.name) for c_ in found}
    check("an entry scoped to an uninstalled module is flagged",
          ("DZ-01", "pragmatic_quickbooks_connector") in ids, str(ids))
    check("an unused_apps entry for something not installed is flagged",
          ("unused_apps", "website_sale") in ids, str(ids))
    check("an entry scoped to an installed module is left alone",
          not any(c_.entry_id == "INV-01" for c_ in found))

    print("\n== the coverage map ==")
    # A second module of ours, touching a model no scenario mentions. This is the
    # row the page exists for.
    fake_staging.STATE.add("ir.module.module", name="hst_lot_weight",
                           latest_version="17.0.1.0.0", state="installed",
                           author="HSxTech", shortdesc="Lot weight")
    fake_staging.STATE.add("ir.model.fields", name="x_net_weight", model="stock.lot",
                           ttype="float", store=True, state="base", relation=False)
    lot_field = fake_staging.STATE.records["ir.model.fields"][-1]["id"]
    fake_staging.STATE.add("ir.model.data", model="ir.model.fields",
                           res_id=lot_field, module="hst_lot_weight")
    census = census_mod.take(instance.connect(client, config_mod.load().secret_key))

    cov = coverage_mod.build(census, repo_modules=snap.modules,
                             scenario_list=snap.scenarios, knowledge=snap.knowledge,
                             last_changed=snap.last_changed)
    rows = {r.name: r for r in cov.rows}
    check("a module we wrote with no scenario is exposed",
          rows["hst_lot_weight"].exposure == coverage_mod.EXPOSED,
          rows["hst_lot_weight"].exposure)
    check("exposure is the sort order, worst first",
          cov.rows[0].name == "hst_lot_weight", cov.rows[0].name)
    check("a scenario is attributed by the models it touches",
          rows["hst_kill_sheet"].scenario_count == 1)
    check("that module reads as covered",
          rows["hst_kill_sheet"].exposure == coverage_mod.COVERED)
    check("a view-only vendor module is view risk",
          rows["muk_web_theme"].exposure == coverage_mod.VIEW_RISK,
          rows["muk_web_theme"].exposure)
    check("Studio gets a row even though it is not a module",
          rows["studio_customization"].exposure == coverage_mod.NO_SOURCE_RISK)
    check("core Odoo modules are counted, not listed",
          "base" not in rows and cov.core_module_count == 1, str(cov.core_module_count))
    check("last-changed comes through", rows["hst_kill_sheet"].last_changed is not None)
    check("the exposed module is named in the summary",
          [r.name for r in cov.exposed] == ["hst_lot_weight"])

    print("\n== knowledge shapes what is selected ==")
    with_unused = coverage_mod.build(
        census, repo_modules=snap.modules, scenario_list=snap.scenarios,
        knowledge=knowledge_mod.parse("unused_apps: [muk_web_theme]"))
    check("a module in unused_apps drops out of the exposure ranking",
          {r.name: r.exposure for r in with_unused.rows}["muk_web_theme"]
          == coverage_mod.UNUSED)

    print("\n== a client with no repo still gets a map ==")
    bare = coverage_mod.build(census)
    check("every module reads as third-party when no repo is known",
          all(r.source != coverage_mod.OURS for r in bare.rows))
    check("and nothing crashes", len(bare.rows) >= 3)

    print("\n== the pages ==")
    repo_sync.save(client_id, repo_sync.fetch(client, api_root=API_ROOT))
    r = c.get(f"/clients/{client_id}/knowledge")
    # The page is a list of addons, not a briefing on them. The overlay entries,
    # scenario table and generated explanations were removed deliberately: the
    # reviewer reading this page knows the codebase, and the agent that does not
    # is handed the source itself (source_bundle) rather than prose about it.
    check("the knowledge page lists the addons",
          "hst_lot_weight" in r.text and "hst_kill_sheet" in r.text)
    check("it shows the path column", "Path" in r.text and "Last changed" in r.text)
    check("it shows the commit it read", fake_github.HEAD[:8] in r.text)
    check("it no longer renders overlay entries",
          "INV-01" not in r.text and "DZ-01" not in r.text)
    check("and no longer renders the scenario table", "no tier" not in r.text)
    check("no success toast without a synced redirect",
          "successfully" not in r.text)

    r = c.get(f"/clients/{client_id}/knowledge?synced=created")
    check("the redirect flag raises the success toast",
          "created successfully" in r.text)
    r = c.get(f"/clients/{client_id}/knowledge?synced=nonsense")
    check("an unrecognised flag cannot forge one", "successfully" not in r.text)

    r = c.get(f"/clients/{client_id}/coverage")
    check("the coverage page renders", "hst_lot_weight" in r.text and "exposed" in r.text)
    check("core modules are summarised rather than listed",
          "core module(s) are installed and not listed" in r.text)

    token = csrf_of(c.get(f"/clients/{client_id}/knowledge").text)
    r = c.post(f"/clients/{client_id}/knowledge/refresh", data={"csrf_token": "wrong"})
    check("the refresh form is CSRF protected", r.status_code == 400)

    db.execute("DELETE FROM clients WHERE id = %s", (client_id,))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("Failed: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
