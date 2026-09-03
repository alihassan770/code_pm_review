"""One screen, one screenshot.

The bug these lock down: a plan wrote seven scenarios about one wizard, one per
field, and the review produced seven identical pictures of that wizard. The
prompt now asks for one scenario per screen, but a prompt is a request and not a
guarantee, so the merge has to hold whatever the planner returns.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from qa_gate import review

passed = failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


class FakeLedger:
    def __init__(self, ids): self.ids = ids
    def id_of(self, ref): return self.ids.get(ref)
    def model_of(self, ref): return "account.payment.register"


def shots_for(scenarios, ledger, checks_by_id=None):
    """Run the capture decision the way `execute` does, and count pictures."""
    groups = review.merge_screens(scenarios)
    seen, taken = {}, []
    for sc in scenarios:
        plan = review._screen_plan(
            sc, (checks_by_id or {}).get(sc["id"], []), ledger, groups)
        if not plan:
            continue
        if plan["key"] in seen:
            continue
        seen[plan["key"]] = sc["id"]
        taken.append(plan)
    return taken


WIZ = "account.payment.register"
led = FakeLedger({"w1": 42, "inv": 7})

# --- the reported bug --------------------------------------------------------
seven = [{"id": f"S{i}", "title": f"field {f}",
          "screen": {"kind": "wizard", "model": WIZ, "record": "w1",
                     "highlight": [f]}}
         for i, f in enumerate("abcdefg", 1)]
took = shots_for(seven, led)
check("seven one-field scenarios take one shot", len(took), 1)
check("and it rings all seven fields", took[0]["highlight"], list("abcdefg"))
check("and it names every scenario it covers",
      took[0]["covers"], [f"S{i}" for i in range(1, 8)])
check("and it photographs the record, not the list", took[0]["res_id"], 42)

# --- the shape the prompt actually asks for ---------------------------------
one = [{"id": "S1", "title": "payment wizard",
        "screen": {"kind": "wizard", "model": WIZ, "record": "w1",
                   "highlight": list("abcdefg")}}]
took = shots_for(one, led)
check("one scenario, one shot", len(took), 1)
check("same rings either way", took[0]["highlight"], list("abcdefg"))

# --- genuinely different screens still get their own picture ----------------
two = [{"id": "S1", "screen": {"kind": "wizard", "model": WIZ, "record": "w1"}},
       {"id": "S2", "screen": {"kind": "form", "model": "account.move",
                               "record": "inv"}}]
check("different models, two shots", len(shots_for(two, led)), 2)

# Same model, different records: two different records, two pictures.
recs = [{"id": "S1", "screen": {"kind": "form", "model": "account.move", "record": "inv"}},
        {"id": "S2", "screen": {"kind": "form", "model": "account.move", "record": "gone"}}]
check("same model, different records, two shots", len(shots_for(recs, led)), 2)

# --- configuration is photographed as configuration -------------------------
for kind, model in [("groups", "res.groups"), ("access", "ir.model.access")]:
    got = review._screen_plan(
        {"id": "S9", "screen": {"kind": kind, "model": "account.move",
                                "record": "inv"}}, [], led, {})
    check(f"{kind} screen photographs {model}", got["model"], model)
    check(f"{kind} screen drops the unrelated record", got["res_id"], None)

# `settings` keeps the model it was given, since the page is model-specific.
got = review._screen_plan(
    {"id": "S9", "screen": {"kind": "settings", "model": "res.config.settings"}},
    [], led, {})
check("settings keeps its model", got["model"], "res.config.settings")

# --- a plan written before `screen` existed still photographs something ------
legacy = review._screen_plan({"id": "S0"}, [{"model": "sale.order", "res_id": 7}], led, {})
check("legacy plan falls back to the asserted model", legacy["model"], "sale.order")
check("legacy plan uses the asserted record", legacy["res_id"], 7)
check("legacy plan with nothing to go on yields no screen",
      review._screen_plan({"id": "S0"}, [], led, {}), {})

# --- junk in the plan must not raise ----------------------------------------
check("screen that is not a dict", review._screen_plan({"id": "S", "screen": "x"}, [], led, {}), {})
check("merge ignores scenarios with no model", review.merge_screens(
    [{"id": "S1", "screen": {"kind": "form"}}]), {})
check("merge ignores non-string highlights", review.merge_screens(
    [{"id": "S1", "screen": {"model": "a.b", "highlight": [1, None, "ok", "  "]}}]
)[("a.b", "", "")]["highlight"], ["ok"])
check("merge dedupes a field named twice", review.merge_screens(
    [{"id": "S1", "screen": {"model": "a.b", "highlight": ["x"]}},
     {"id": "S2", "screen": {"model": "a.b", "highlight": ["x", "y"]}}]
)[("a.b", "", "")]["highlight"], ["x", "y"])

# --- the prompt must keep carrying the rule ---------------------------------
check("prompt forbids one scenario per field",
      "NEVER ONE PER FIELD" in review.PLAN_SYSTEM, True)
check("prompt requires a screen block", '"screen"' in review.PLAN_SYSTEM, True)
check("prompt asks for configuration to be photographed",
      "GROUPS OR ACCESS RIGHTS" in review.PLAN_SYSTEM, True)
check("prompt still bans em dashes", "em dash" in review.PLAN_SYSTEM, True)



# ---------------------------------------------- photograph ONE record --------
# A scenario about one scheduled action was evidenced by the Scheduled Actions
# list, forty rows deep, with the relevant one somewhere in the middle. The
# screen now resolves a domain to a single record and opens its form.

class FakeConn:
    """Records what was asked, and answers the way Odoo would."""

    def __init__(self, rows):
        self.rows = rows          # {(model, tuple(domain)): [ids]}
        self.calls = []

    def call(self, model, method, args=None, kwargs=None):
        self.calls.append((model, method, args, kwargs))
        domain = (args or [[]])[0]
        key = (model, repr(domain))
        ids = self.rows.get(key, [])
        limit = (kwargs or {}).get("limit")
        return ids[:limit] if limit else ids


CRON = [["name", "=", "Auto reconcile"]]
conn = FakeConn({("ir.cron", repr(CRON)): [58]})

got = review._screen_plan(
    {"id": "S5", "screen": {"kind": "form", "model": "ir.cron",
                            "domain": CRON, "highlight": ["active"]}},
    [], led, {}, conn)
check("a domain resolves to the record", got["res_id"], 58)
check("and the screen becomes a form", got["kind"], "form")

# Archived records must be findable. Odoo hides them by default and the gate
# read 1 cron instead of 41, so every check about an inactive record came back
# "no record matches" and looked like a badly written plan.
ctx = (conn.calls[-1][3] or {}).get("context") or {}
check("_read disables active_test", ctx.get("active_test"), False)

# A domain matching several records names none: a picture of the wrong one of
# six is worse than a picture of the list, because it looks precise.
many = FakeConn({("ir.cron", repr(CRON)): [58, 59]})
check("an ambiguous domain resolves to nothing",
      review._resolve_screen_record(many, "ir.cron", CRON), None)
check("an empty domain resolves to nothing",
      review._resolve_screen_record(many, "ir.cron", []), None)
check("a domain matching nothing resolves to nothing",
      review._resolve_screen_record(FakeConn({}), "ir.cron", CRON), None)


class BoomConn:
    def call(self, *a, **k):
        raise RuntimeError("instance is down")


check("an unreadable instance costs a screenshot, not the run",
      review._resolve_screen_record(BoomConn(), "ir.cron", CRON), None)

# An assertion's own domain is the last resort, since an assertion has already
# been required to identify exactly one record.
got = review._screen_plan(
    {"id": "S6", "screen": {"kind": "form", "model": "ir.cron"}},
    [{"model": "ir.cron", "domain": CRON, "res_id": None}], led, {},
    FakeConn({("ir.cron", repr(CRON)): [58]}))
check("falls back to an assertion's domain", got["res_id"], 58)

# With nothing at all to go on it stays a list, and says so rather than
# pretending to be a form.
got = review._screen_plan(
    {"id": "S7", "screen": {"kind": "list", "model": "ir.cron"}}, [], led, {},
    FakeConn({}))
check("no record means it stays a list", got["kind"], "list")
check("and carries no id", got["res_id"], None)

# The prompt has to keep asking for this, or the planner stops supplying it.
check("prompt documents screen.domain", '"domain"' in review.PLAN_SYSTEM, True)
check("prompt forbids a list where a record is meant",
      "ALMOST EVERY SCENARIO SHOULD PHOTOGRAPH ONE RECORD" in review.PLAN_SYSTEM, True)



# ------------------------------------------ the verdict must be honest -------
# `partial` was returned for any mix of passes and failures, so 1 check holding
# out of 11 read as a near miss when it was the opposite. It now has to mean
# mostly working.

def verdict(p, f, b=0):
    run = type("R", (), {"steps": [type("S", (), {
        "phase": "execute", "state": "done",
        "detail": {"scenarios": [{"passed": p, "failed": f, "blocked": b}]}})()]})()
    return review.compute_verdict(run)[0]


check("everything holding is a pass", verdict(11, 0), "pass")
check("mostly holding is partial", verdict(9, 1), "partial")
check("a bare majority holding is still partial", verdict(6, 5), "partial")
check("half failing is a fail, not a near miss", verdict(5, 5), "fail")
check("one of eleven holding is a fail", verdict(1, 10), "fail")
check("nothing holding is a fail", verdict(0, 10), "fail")
check("blocked checks do not turn a fail into a pass", verdict(1, 10, 4), "fail")
check("blocked checks alone give no verdict", verdict(0, 0, 7), "")
check("blocked alongside passes still passes", verdict(3, 0, 4), "pass")


# ------------------------------------- a draft record cannot be posted -------
# The plan created an account.payment and asserted it reached a live state.
# `create` leaves it in draft with no journal entry, so the check could never
# hold and the review blamed the developer for a record the gate half built.
from qa_gate import fixtures as fixtures_mod  # noqa: E402

check("a transition allowlist exists",
      "action_post" in fixtures_mod.ALLOWED_ACTIONS, True)
for banned in ("unlink", "write", "create", "action_cancel"):
    check(f"{banned} is not a transition", banned in fixtures_mod.ALLOWED_ACTIONS, False)
check("the prompt warns that creations are drafts",
      "ANYTHING YOU CREATE IS A DRAFT" in review.PLAN_SYSTEM, True)
check("the prompt shows the `then` key", '"then": ["action_post"]' in review.PLAN_SYSTEM, True)


# --------------------------- an impossible expectation is not a failure ------
# `account.payment.state` has no "posted" value on Odoo 18: it is draft,
# in_process, paid, canceled, rejected. Asserting "posted" can never hold, and
# calling that a failure blames the change for the plan's version mistake.

class SelConn:
    def __init__(self, values, actual):
        self.values, self.actual = values, actual

    def call(self, model, method, args=None, kwargs=None):
        if method == "fields_get":
            return {"state": {"type": "selection",
                              "selection": [(v, v) for v in self.values]}}
        if method == "search_read":
            return [{"id": 1, "state": self.actual}]
        return True

    def model_exists(self, model):
        return True


review._SELECTION_CACHE.clear()
sc = SelConn(["draft", "in_process", "paid"], "draft")
A = {"id": "A1", "text": "posted", "check": "read", "model": "account.payment",
     "field": "state", "expect": "posted", "domain": [["id", "=", 1]]}
got = review._check_assertion(sc, A)
check("an impossible value is blocked, not failed", got["state"], "blocked")
check("and the note names the real values", "in_process" in got.get("note", ""), True)
check("and says it is the plan's mistake", "not a fault in the change" in got.get("note", ""), True)

review._SELECTION_CACHE.clear()
got = review._check_assertion(SelConn(["draft", "in_process"], "draft"),
                             dict(A, expect="in_process"))
check("a possible value that does not hold is a real failure", got["state"], "failed")

review._SELECTION_CACHE.clear()
got = review._check_assertion(SelConn(["draft", "in_process"], "in_process"),
                             dict(A, expect="in_process"))
check("and a matching one passes", got["state"], "passed")

check("the prompt forbids guessing state values",
      "STATE VALUES DIFFER BY ODOO VERSION" in review.PLAN_SYSTEM, True)

print(f"\n  {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
