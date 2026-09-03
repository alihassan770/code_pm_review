"""The report phase: posting a run's summary back to its Odoo task.

Asserts on what reached the fake Odoo rather than on what the code says it
sent — the three properties that matter are all properties of the request:
it is a log note, it carries the heading, and it has no attachments.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import fake_odoo

IDENTITY_PORT = 8896
fake_odoo.serve(IDENTITY_PORT)

tmp = tempfile.mkdtemp(prefix="qa-gate-test-report-")
os.environ["QA_GATE_CONFIG"] = str(Path(tmp) / "config.yaml")
os.environ["QA_GATE_STATE_DIR"] = str(Path(tmp) / "state")

from dataclasses import replace  # noqa: E402

from qa_gate import config as config_mod, db  # noqa: E402

TEST_DB = os.environ.get("QA_GATE_TEST_DB", "postgresql://odoo@/odoo_qa_gate_test")
cfg = config_mod.load()
cfg = replace(cfg, database_url=TEST_DB,
              odoo=config_mod.OdooIdentity(url=f"http://127.0.0.1:{IDENTITY_PORT}",
                                           db=fake_odoo.DB))
config_mod.save(cfg)

from qa_gate import app_secrets, review  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail and not cond else ''}")


db.init_pool(TEST_DB)
db.migrate()
cfg = config_mod.load()
app_secrets.set_(app_secrets.IDENTITY_RPC, login="hamza", secret="key-hamza",
                 secret_key=cfg.secret_key)

# A client to hang the run off. The report phase never touches the instance, so
# it needs a row and nothing more.
client_row = db.query_one(
    "INSERT INTO clients (slug, name) VALUES ('report-test', 'Report Test') "
    "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id")
CLIENT_ID = client_row["id"]


def new_run(summary: str, *, task_id: int = 4471) -> int:
    # Only one run per client may be active at a time — that partial unique
    # index is the §7 "one run holds the instance" rule — so retire the last one
    # before starting another. Reporting happens after a run is finished anyway.
    db.execute("UPDATE review_runs SET state = 'done' WHERE client_id = %s "
               "AND state IN ('queued', 'running', 'paused')", (CLIENT_ID,))
    row = db.query_one(
        "INSERT INTO review_runs (client_id, task_id, task_name, state, verdict, "
        "summary) VALUES (%s, %s, 'Kill sheet totals', 'running', 'pass', %s) "
        "RETURNING id", (CLIENT_ID, task_id, summary))
    return int(row["id"])


print("\n== the note body ==")
body = review.note_body("The discount carried to the invoice.\n\nNothing else moved.")
check("it starts with the heading",
      body.startswith("<p><b>PM REVIEW SUMMARY</b></p>"), body[:60])
check("the summary text follows it", "The discount carried to the invoice." in body)
check("blank lines become separate paragraphs", body.count("<p>") == 3, body)
check("nothing else is added",
      "href" not in body and "<img" not in body and "<table" not in body)

nasty = review.note_body("A <script>alert(1)</script> title & an ampersand")
check("the summary is escaped, not rendered",
      "&lt;script&gt;" in nasty and "<script>" not in nasty, nasty)
check("an empty summary produces no body", review.note_body("   ") == "")

print("\n== posting ==")
fake_odoo.MESSAGES.clear()
run_id = new_run("The discount carried to the invoice.")
detail = review.write_back(review.get(run_id))
check("it posts", detail.get("posted") is True, str(detail))
check("one message reached Odoo", len(fake_odoo.MESSAGES) == 1)
posted = fake_odoo.MESSAGES[-1]
check("it went to the right task", posted["task_id"] == 4471)
check("it is a log note, not a message that notifies followers",
      posted["subtype_xmlid"] == "mail.mt_note", str(posted["subtype_xmlid"]))
check("the body carries the heading", "PM REVIEW SUMMARY" in posted["body"])
check("reported_at is stamped", review.get(run_id).reported_at is not None)

print("\n== it cannot post twice ==")
again = review.write_back(review.get(run_id))
check("a second call is a no-op", again.get("posted") is False)
check("and says why", again.get("skipped") == "already posted", str(again))
check("nothing more reached Odoo", len(fake_odoo.MESSAGES) == 1)

print("\n== a version without subtype_xmlid ==")
fake_odoo.MESSAGES.clear()
fake_odoo.SUPPORTS_SUBTYPE_XMLID = False
try:
    detail = review.write_back(review.get(new_run("Posted the hard way.")))
    check("it still posts", detail.get("posted") is True, str(detail))
    check("falling back to a notification is still not a follower message",
          fake_odoo.MESSAGES[-1]["message_type"] == "notification",
          str(fake_odoo.MESSAGES[-1]))
finally:
    fake_odoo.SUPPORTS_SUBTYPE_XMLID = True

print("\n== nothing to say ==")
fake_odoo.MESSAGES.clear()
detail = review.write_back(review.get(new_run("")))
check("a run with no summary posts nothing", detail.get("posted") is False)
check("and nothing reached Odoo", fake_odoo.MESSAGES == [])

print("\n== an Odoo that refuses ==")
app_secrets.set_(app_secrets.IDENTITY_RPC, login="hamza", secret="wrong-key",
                 secret_key=cfg.secret_key)
run_id = new_run("This one cannot be posted.")
detail = review.write_back(review.get(run_id))
check("a refusal is reported, not raised", detail.get("posted") is False)
check("the reason names the likely fix",
      "write access" in (detail.get("error") or "") or "rejected" in (detail.get("error") or ""),
      str(detail))
check("reported_at stays empty so a retry is possible",
      review.get(run_id).reported_at is None)

app_secrets.set_(app_secrets.IDENTITY_RPC, login="hamza", secret="key-hamza",
                 secret_key=cfg.secret_key)
retry = review.retry_report(run_id)
check("retrying after the fix posts it", retry.get("posted") is True, str(retry))
check("the retry is recorded as a step",
      any(s.phase == "report" and s.state == "done"
          for s in (review.get(run_id).steps or [])))

print("\n== the phase list ==")
check("report is the last phase", review.PHASES[-1] == "report")
check("it has a title", review.PHASE_TITLES.get("report"))

db.execute("DELETE FROM clients WHERE id = %s", (CLIENT_ID,))
db.execute("DELETE FROM app_secrets WHERE key = %s", (app_secrets.IDENTITY_RPC,))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("Failed: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
