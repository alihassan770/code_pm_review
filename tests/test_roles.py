"""Two roles, and the provider an administrator picks for everybody.

The guards are the point of this file. Demoting the last administrator, or
yourself, leaves a system nobody can configure from inside, and the only way
back is a shell on the server. That is not a bug you find in review, so it is
one worth a test.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

tmp = tempfile.mkdtemp(prefix="qa-gate-test-roles-")
os.environ["QA_GATE_CONFIG"] = str(Path(tmp) / "config.yaml")
os.environ["QA_GATE_STATE_DIR"] = str(Path(tmp) / "state")

from dataclasses import replace  # noqa: E402

from qa_gate import config as config_mod, db  # noqa: E402

TEST_DB = os.environ.get("QA_GATE_TEST_DB", "postgresql://odoo@/odoo_qa_gate_test")
cfg = replace(config_mod.load(), database_url=TEST_DB)
config_mod.save(cfg)

from qa_gate import ai, app_secrets, app_settings, users  # noqa: E402
from qa_gate.odoo_client import OdooUser  # noqa: E402

db.init_pool(TEST_DB)
db.migrate()

PASS, FAIL = [], []


def check(label, got, want):
    (PASS if got == want else FAIL).append(label)
    if got != want:
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def make(login, admin=False):
    return users.upsert_from_odoo(
        OdooUser(uid=abs(hash(login)) % 100000, login=login, name=login.title(),
                 email=f"{login}@x.test", is_admin=admin))


# ---------------------------------------------------------------- roles -----
db.execute("DELETE FROM users")

first = make("ana")          # first user in is an admin whatever Odoo says
check("first user is an admin", first.is_admin, True)

second = make("ben")
check("second user is not", second.is_admin, False)
check("one administrator so far", users.admin_count(), 1)

users.set_admin(second.id, True)
check("promoting works", users.get(second.id).is_admin, True)
check("now two administrators", users.admin_count(), 2)

users.set_admin(second.id, False)
check("demoting works", users.get(second.id).is_admin, False)
check("back to one", users.admin_count(), 1)

# An Odoo sysadmin signing in is an admin again, and an ordinary login never
# silently demotes somebody: `is_admin = is_admin OR EXCLUDED.is_admin`.
users.set_admin(second.id, True)
make("ben")
check("a plain sign in does not demote", users.get(second.id).is_admin, True)
users.set_admin(second.id, False)
make("ben", admin=True)
check("holding group_system re-grants admin", users.get(second.id).is_admin, True)

# `admin_count` is what the last-administrator guard reads, so it must not
# count somebody who has been deactivated.
users.set_admin(second.id, False)
db.execute("UPDATE users SET active = false WHERE id = %s", (first.id,))
check("an inactive admin is not counted", users.admin_count(), 0)
db.execute("UPDATE users SET active = true WHERE id = %s", (first.id,))
check("and is counted again once active", users.admin_count(), 1)

# ------------------------------------------------------------ providers -----
check("three providers offered", sorted(ai.PROVIDERS), ["anthropic", "deepseek", "openai"])
check("deepseek is the default", ai.DEFAULT_PROVIDER, "deepseek")
check("anthropic speaks its own dialect", ai.PROVIDERS["anthropic"].dialect, "anthropic")
check("openai speaks the openai dialect", ai.PROVIDERS["openai"].dialect, "openai")
check("keys are stored per provider",
      [ai.secret_key_name(p) for p in ("deepseek", "anthropic", "openai")],
      ["ai_key_deepseek", "ai_key_anthropic", "ai_key_openai"])

db.execute("DELETE FROM app_settings")
db.execute("DELETE FROM app_secrets")
check("with nothing stored, the default is selected", ai.selected().key, "deepseek")
check("and nothing is configured", ai.is_configured(), False)

app_settings.set_(app_settings.AI_PROVIDER, "anthropic")
check("the selection is honoured", ai.selected().key, "anthropic")
check("an unknown selection falls back rather than raising",
      (app_settings.set_(app_settings.AI_PROVIDER, "gemini-9"), ai.selected().key)[1],
      "deepseek")

# Each provider's key is its own row, so switching provider and back does not
# lose the first key.
app_settings.set_(app_settings.AI_PROVIDER, "deepseek")
app_secrets.set_("ai_key_deepseek", login="aaaa", secret="sk-deep", secret_key=cfg.secret_key)
app_secrets.set_("ai_key_anthropic", login="bbbb", secret="sk-ant-x", secret_key=cfg.secret_key)
check("deepseek key is live", ai.is_configured(), True)
check("and the right client class", type(ai.client(cfg.secret_key)).__name__,
      "OpenAICompatible")

app_settings.set_(app_settings.AI_PROVIDER, "anthropic")
check("anthropic key survived the switch", ai.is_configured(), True)
check("and gets the anthropic client", type(ai.client(cfg.secret_key)).__name__, "Anthropic")
app_settings.set_(app_settings.AI_PROVIDER, "deepseek")
check("switching back finds the first key intact",
      app_secrets.get("ai_key_deepseek", cfg.secret_key).secret, "sk-deep")

# A provider selected with no key must say so as NotConfigured, which sends
# somebody to the settings page instead of to a retry.
app_settings.set_(app_settings.AI_PROVIDER, "openai")
try:
    ai.client(cfg.secret_key)
    check("no key raises NotConfigured", "no error", "NotConfigured")
except ai.NotConfigured as exc:
    check("no key raises NotConfigured", "NotConfigured", "NotConfigured")
    check("and the message points at settings", "Settings" in str(exc), True)

# An install that predates per-provider storage keeps working: the key under the
# old single-provider name is still read.
db.execute("DELETE FROM app_secrets")
app_settings.set_(app_settings.AI_PROVIDER, "deepseek")
app_secrets.set_(app_secrets.DEEPSEEK_KEY, login="cccc", secret="sk-legacy",
                 secret_key=cfg.secret_key)
check("a pre-migration key is still found", ai.is_configured(), True)
check("and still builds a client", ai.client(cfg.secret_key)._key, "sk-legacy")

# --------------------------------------------------------- anthropic wire ----
# Claude returns typed blocks, and the thinking one must not land in the text.
answer = ai.Anthropic._read({
    "model": "claude-opus-5",
    "content": [{"type": "thinking", "thinking": "let me see"},
                {"type": "text", "text": "the answer"}],
    "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 7},
}, "fallback")
check("text block becomes text", answer.text, "the answer")
check("thinking block becomes reasoning", answer.reasoning, "let me see")
check("usage is mapped from anthropic's names", answer.usage.prompt_tokens, 10)
check("cache reads are mapped too", answer.usage.cache_hit_tokens, 7)
check("an empty reply does not raise",
      ai.Anthropic._read({"content": []}, "fallback").text, "")
check("model falls back when absent", ai.Anthropic._read({}, "fallback").model, "fallback")


# --------------------------------------------------- a verdict is sticky -----
# The bug: a task reviewed to `partial`, then retried, where the retry failed.
# Keying the badge off the latest run made the failed retry erase the verdict
# and the task dropped back to "never reviewed".
from qa_gate import review  # noqa: E402

db.execute("DELETE FROM review_runs")
db.execute("DELETE FROM clients")
db.execute(
    "INSERT INTO clients (id, name, slug, active) VALUES (7, 'T', 't', true)")


def run_row(rid, task, state, verdict, minute):
    db.execute(
        """INSERT INTO review_runs (id, client_id, task_id, state, verdict, phase,
                                    started_at)
           VALUES (%s, 7, %s, %s, %s, 'interpret',
                   timestamptz '2026-01-01 00:00:00' + (%s || ' minutes')::interval)""",
        (rid, task, state, verdict, minute))


run_row(1, 900, "done", "partial", 1)
run_row(2, 900, "failed", "", 2)          # the retry that died

check("latest run is the failed retry", review.latest_by_task(7, [900])[900].id, 2)
check("but the verdict is still the finished one",
      review.verdicts_by_task(7, [900])[900].id, 1)
check("and it still reads partial",
      review.verdicts_by_task(7, [900])[900].verdict, "partial")

# A newer verdict does replace an older one.
run_row(3, 900, "done", "pass", 3)
check("a newer verdict wins", review.verdicts_by_task(7, [900])[900].verdict, "pass")

# A task with only inconclusive runs has no verdict, so it stays in the queue.
run_row(4, 901, "cancelled", "", 1)
run_row(5, 901, "failed", "", 2)
check("no verdict means not reviewed", 901 in review.verdicts_by_task(7, [901]), False)
check("but the attempt is still findable", review.latest_by_task(7, [901])[901].id, 5)
check("no tasks, no query", review.verdicts_by_task(7, []), {})

# ------------------------------------------- what message_post gives back ----
# Odoo serialises a returned recordset to `.ids`, so this is a list on every
# version we support and `int()` on it raised a TypeError *after* the note had
# already been posted.
from qa_gate.projects import Identity  # noqa: E402

check("a one-element list is the id", Identity._message_id([1234]), 1234)
check("a tuple works too", Identity._message_id((77,)), 77)
check("a bare id still works", Identity._message_id(42), 42)
check("an empty list does not raise", Identity._message_id([]), 0)
check("None does not raise", Identity._message_id(None), 0)
check("nonsense does not raise", Identity._message_id([[5]]), 0)


# ------------------------------------- the verdict must reach the run row ----
# `summarise` computes the verdict and the write to `review_runs` was left
# behind when the separate `verdict` phase was removed. Runs then completed all
# seven phases with an empty verdict: the run page showed only the state
# ("done") while the task list kept showing an older run's verdict, which is how
# it was noticed.
import inspect  # noqa: E402

src = inspect.getsource(review.advance)
check("summarise persists the verdict", "verdict=value" in src, True)
check("and keeps it in the step detail", '"verdict": value' in src, True)

# A run where nothing could be checked has no verdict, and that is correct: it
# must not be recorded as a pass.
run_row(6, 902, "done", "", 1)
check("a done run with no verdict is not reviewed",
      902 in review.verdicts_by_task(7, [902]), False)

print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
