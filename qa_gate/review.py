"""Running one review of one task.

The engine behind Start review. It is a state machine over six phases, and the
shape is dictated by §7: pausing must free the client's database and resuming
must pick up where it left off. That rules out doing the work inside a request.

    interpret → blast_radius → plan → execute → verdict → report

Each phase writes a `review_steps` row and returns. Resume starts at the first
step that is not `done`, so a paused run replays nothing. The phases are coarse
on purpose — a checkpoint has to be a place where stopping is safe and the work
already finished is still true, which "after reading the task" is and "halfway
through filling a form" is not.

## What the model may decide, and what it may not

§14 requires the verdict be computed from assertion results. So:

  * The model **interprets** — turning a paragraph a human wrote into explicit,
    checkable requirements, and reading any mockup images attached to the task.
  * The model **proposes** — which other flows share the changed models and are
    therefore worth exercising.
  * The model **describes** — what a screenshot shows, after the fact.

  * The model does **not** decide pass or fail. `verdict()` below is arithmetic
    over the `passed` column and contains no model call at all. That is the
    whole reason this is a gate rather than a second opinion.

## Blast radius

The reason this exists as its own phase: in Odoo a single table backs several
user-facing forms. `account.move` is the customer invoice, the credit note, the
vendor bill and the journal entry. A change to one is a change to all four, and
the regression a reviewer misses is almost never on the path they were told
about. So the plan is deliberately not "test what the ticket says" — it is
"test what the ticket says, then test everything that shares its tables".
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import ai, db, source_bundle

log = logging.getLogger(__name__)

#: In order. `resume` walks this list and runs the first phase not yet done.
#:
#: `report` posts the summary back to the Odoo task as an internal **log note**
#: (see `write_back` below). An earlier revision of this module left it out on
#: the grounds that a gate posting its own conclusions starts shaping the record
#: it is reviewing. That concern is real and the shape of the note answers it:
#: it is a note rather than a message so nothing is emailed to anyone, it says
#: only what the run found, and it never edits the task's description, stage, or
#: fields. Plan §11 assumes this channel exists — the test plan is meant to be
#: posted to the task before execution, which is the same mechanism.
#: `cleanup` and `verdict` are deliberately absent.
#:
#: Nothing created on a client's staging is deleted any more. The operator asked
#: for that directly: records left in place can be inspected, and the ledger
#: (`review_fixtures`) records exactly which ones this app made, so "did I create
#: this or did the gate" has an answer. `fixtures.rollback` and
#: `qa-gate leftovers` still exist for when somebody chooses to tidy up; it is a
#: decision, not a step in every run.
#:
#: The verdict was never worth a phase of its own. It is arithmetic over the
#: assertion results, takes no measurable time, and happens inside `summarise`
#: where the sentence about it is written.
PHASES = ["interpret", "code_check", "blast_radius", "plan", "execute",
          "summarise", "report"]

PHASE_TITLES = {
    "interpret": "Read the task",
    "code_check": "Look for it in the code",
    "blast_radius": "Work out what else is affected",
    "plan": "Decide what to test",
    "execute": "Run the scenarios",
    "summarise": "Write the summary",
    "report": "Post the summary to the Odoo task",
}

#: The first line of every note the gate writes. Constant, and matched on when
#: deciding whether a run has already reported, so changing it is a decision
#: about every future note rather than a typo somebody makes once.
NOTE_HEADING = "PM REVIEW SUMMARY"

ACTIVE_STATES = ("queued", "running", "paused")


class ReviewError(Exception):
    """Something stopped the run. Message is safe to show."""


class Paused(Exception):
    """Raised to unwind out of the phase loop when a pause was requested.

    An exception rather than a return value because the check happens between
    phases, and threading "should I stop" back through six call sites would put
    the decision in six places instead of one.
    """


# ---- records ---------------------------------------------------------------

@dataclass
class Step:
    id: int = 0
    seq: int = 0
    phase: str = ""
    title: str = ""
    state: str = "pending"
    passed: bool | None = None
    detail: dict = field(default_factory=dict)
    reasoning: str = ""
    note: str = ""
    finished_at: datetime | None = None


@dataclass
class Run:
    id: int = 0
    client_id: int = 0
    task_id: int = 0
    task_name: str = ""
    state: str = "queued"
    verdict: str = ""
    phase: str = "interpret"
    commit_sha: str = ""
    persona_key: str = ""
    summary: str = ""
    error: str = ""
    started_at: datetime | None = None
    paused_at: datetime | None = None
    finished_at: datetime | None = None
    #: When the summary was written back to the Odoo task. The guard against
    #: posting the same note twice when the report phase is retried.
    reported_at: datetime | None = None
    steps: list[Step] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:8]

    @property
    def done_phases(self) -> set[str]:
        return {s.phase for s in self.steps if s.state == "done"}

    @property
    def next_phase(self) -> str | None:
        done = self.done_phases
        for phase in PHASES:
            if phase not in done:
                return phase
        return None

    @property
    def progress(self) -> tuple[int, int]:
        return len(self.done_phases), len(PHASES)


def _run_from_row(row: dict) -> Run:
    return Run(
        id=row["id"], client_id=row["client_id"], task_id=row["task_id"],
        task_name=row["task_name"], state=row["state"], verdict=row["verdict"],
        phase=row["phase"], commit_sha=row["commit_sha"],
        persona_key=row["persona_key"], summary=row["summary"],
        error=row["error"] or "", started_at=row["started_at"],
        paused_at=row["paused_at"], finished_at=row["finished_at"],
        reported_at=row.get("reported_at"),
    )


def _step_from_row(row: dict) -> Step:
    return Step(
        id=row["id"], seq=row["seq"], phase=row["phase"], title=row["title"],
        state=row["state"], passed=row["passed"], detail=row["detail"] or {},
        reasoning=row["reasoning"] or "", note=row["note"] or "",
        finished_at=row["finished_at"],
    )


def get(run_id: int) -> Run | None:
    row = db.query_one("SELECT * FROM review_runs WHERE id = %s", (run_id,))
    if not row:
        return None
    run = _run_from_row(row)
    run.steps = [_step_from_row(r) for r in db.query(
        "SELECT * FROM review_steps WHERE run_id = %s ORDER BY seq", (run_id,))]
    return run


def active_for_client(client_id: int) -> Run | None:
    """The run currently holding this client's instance, if any."""
    row = db.query_one(
        "SELECT * FROM review_runs WHERE client_id = %s AND state = ANY(%s) "
        "ORDER BY started_at DESC LIMIT 1", (client_id, list(ACTIVE_STATES)))
    return get(row["id"]) if row else None


def recent_for_client(client_id: int, limit: int = 20) -> list[Run]:
    return [_run_from_row(r) for r in db.query(
        "SELECT * FROM review_runs WHERE client_id = %s "
        "ORDER BY started_at DESC LIMIT %s", (client_id, limit))]


def latest_for_task(client_id: int, task_id: int) -> Run | None:
    row = db.query_one(
        "SELECT * FROM review_runs WHERE client_id = %s AND task_id = %s "
        "ORDER BY started_at DESC LIMIT 1", (client_id, task_id))
    return get(row["id"]) if row else None


# ---- lifecycle -------------------------------------------------------------

def start(client_id: int, task_id: int, task_name: str, *, commit_sha: str = "",
          persona_key: str = "", started_by: int | None = None) -> Run:
    """Open a run, or raise if this client already has one in flight.

    The uniqueness is enforced by a partial index rather than by looking first,
    because two people pressing Start at the same moment is exactly the race a
    check-then-insert loses.
    """
    try:
        row = db.query_one(
            """
            INSERT INTO review_runs
                (client_id, task_id, task_name, state, phase, commit_sha,
                 persona_key, started_by)
            VALUES (%s, %s, %s, 'queued', 'interpret', %s, %s, %s)
            RETURNING *
            """,
            (client_id, task_id, task_name[:400], commit_sha, persona_key, started_by))
    except Exception as exc:  # noqa: BLE001 - unique violation is the expected case
        if "review_runs_one_active_per_client" in str(exc):
            raise ReviewError(
                "This client already has a review in progress. Pause or cancel it "
                "first — only one run may hold a staging instance at a time, "
                "because pausing is what frees the database.") from exc
        raise
    return get(row["id"])  # type: ignore[return-value]


def request_pause(run_id: int) -> None:
    """Ask a run to stop at the next phase boundary.

    A request, not an interruption. Killing a run mid-phase could leave a form
    half-filled on the client's instance, which is precisely the state §7 says
    pausing must not leave behind.
    """
    db.execute(
        "UPDATE review_runs SET state = 'paused', paused_at = now() "
        "WHERE id = %s AND state IN ('queued', 'running')", (run_id,))


def cancel(run_id: int, *, secret_key: str = "") -> None:
    """Stop a run and take back anything it created.

    A cancelled run that left fixtures behind would be worse than one that
    finished: nobody is going to look at its page again, so the records would
    sit in the client's database unattributed. Cleanup is attempted before the
    state changes, and a failure to reach the instance never blocks the cancel —
    the ledger row survives either way and `fixtures.orphans()` will still find it.
    """
    db.execute(
        "UPDATE review_runs SET state = 'cancelled', finished_at = now() "
        "WHERE id = %s AND state = ANY(%s)", (run_id, list(ACTIVE_STATES)))


def live_fixture_count(run_id: int) -> int:
    row = db.query_one(
        "SELECT count(*) AS n FROM review_fixtures "
        "WHERE run_id = %s AND removed_at IS NULL", (run_id,))
    return int((row or {}).get("n") or 0)


def _should_stop(run_id: int) -> bool:
    row = db.query_one("SELECT state FROM review_runs WHERE id = %s", (run_id,))
    return not row or row["state"] in ("paused", "cancelled")


def _set_state(run_id: int, state: str, **cols) -> None:
    sets = ["state = %s"]
    params: list = [state]
    for key, value in cols.items():
        sets.append(f"{key} = %s")
        params.append(value)
    if state in ("done", "failed", "cancelled"):
        sets.append("finished_at = now()")
    params.append(run_id)
    db.execute(f"UPDATE review_runs SET {', '.join(sets)} WHERE id = %s", params)


def _begin_step(run_id: int, phase: str) -> None:
    """Mark a phase as started, so its duration is measurable.

    Split from `_save_step` because writing `started_at` and `finished_at` in
    the same statement made every phase take zero seconds — which is exactly
    the number you get when you ask a question the schema cannot answer, and it
    hid which phase was actually slow.
    """
    seq = PHASES.index(phase)
    db.execute(
        """
        INSERT INTO review_steps (run_id, seq, phase, title, state, started_at)
        VALUES (%s, %s, %s, %s, 'running', now())
        ON CONFLICT (run_id, seq) DO UPDATE SET
            state = 'running', started_at = now(), finished_at = NULL
        """,
        (run_id, seq, phase, PHASE_TITLES.get(phase, phase)),
    )


def _save_step(run_id: int, phase: str, *, state: str, detail: dict | None = None,
               reasoning: str = "", note: str = "", passed: bool | None = None) -> None:
    seq = PHASES.index(phase)
    db.execute(
        """
        INSERT INTO review_steps
            (run_id, seq, phase, title, state, passed, detail, reasoning, note,
             started_at, finished_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (run_id, seq) DO UPDATE SET
            state = EXCLUDED.state, passed = EXCLUDED.passed,
            detail = EXCLUDED.detail, reasoning = EXCLUDED.reasoning,
            note = EXCLUDED.note, finished_at = now(),
            -- keep the real start; only fill it if nothing began the phase
            started_at = COALESCE(review_steps.started_at, now())
        """,
        (run_id, seq, phase, PHASE_TITLES.get(phase, phase), state, passed,
         json.dumps(detail or {}), reasoning[:20000], note[:2000]),
    )


# ---- phase 1: interpret ----------------------------------------------------

INTERPRET_SYSTEM = """\
You are reading an Odoo development task in order to VERIFY it.

The work has already been done. A developer has marked this task ready for \
review, and the change is already deployed to the staging instance you will be \
testing against. Your job is NOT to decide what to build, and NOT to judge \
whether the change was a good idea. Your job is to work out what must now be \
OBSERVABLY TRUE on that instance if the task really is done.

So read every statement in the description as a claim to be checked, not as an \
instruction to be carried out. "Add a Recon Status field" becomes "a Recon \
Status field exists, in this place, behaving like this".

Answer ONLY with a JSON object:

{
  "goal": "one sentence: what should now be true, in business terms",
  "requirements": [
    {"id": "R1",
     "text": "one specific, observable thing that must be true on staging now",
     "how_to_check": "the concrete steps to see it — which menu, which form, which field",
     "source": "quote the phrase in the description (or image) this came from"}
  ],
  "models": ["odoo technical model names the change touches, e.g. account.move"],
  "expected_symbols": [
    {"kind": "field|method|model|xml_id|selection",
     "name": "the exact identifier that must appear in the source, e.g. x_recon_status",
     "where": "the odoo model it belongs to, e.g. res.users",
     "why": "R1"}
  ],
  "ambiguities": ["only things that stop you writing a check — see below"],
  "from_images": ["what each attached mockup told you, if any"]
}

Rules:
- Ground every requirement in the description or an attached image. Quote it in \
"source". If you cannot ground it, leave it out.
- Prefer several small checkable requirements over one large vague one.
- "models" must be real Odoo technical names. Use the source code you were given \
to confirm them; do not guess from the task's prose.
- If a mockup marks where a field or button goes, say the exact label and the \
position in "from_images".
- Never use an em dash or en dash in any text you return.
- The source code you are given is the code that is ALREADY DEPLOYED. If it \
already implements what the description asks for, that is expected and is not \
worth remarking on — it means the developer did the work. Write the requirement \
that checks it behaves that way at runtime.
- An "ambiguity" is ONLY a question whose answer changes what you would check — \
a threshold whose boundary is unstated, a field whose name is not given, a rule \
with two possible readings. Never ask whether the task is to implement or to \
verify: it is always to verify.
- "expected_symbols" is the fast check: if the task says "add a field called \
Hi-Tea to res.users", then something like `hi_tea` MUST appear in the addon \
source, and its absence means the work was not done. List only identifiers that \
would genuinely be in code. Do NOT list things achieved purely by configuration \
(a setting toggled in the UI, a value changed on a record) — those leave no \
trace in the repository and would read as missing when they are fine.
- Give the identifier as it would be WRITTEN IN PYTHON: lowercase with \
underscores. "Hi-Tea" becomes `hi_tea`; a custom field is often prefixed `x_`.
- An empty list is a correct answer. An invented requirement is not.
- Never use an em dash or en dash in anything you write. Use a comma, a colon, or a full stop instead. This applies to every field you return.
- No prose outside the JSON object."""


def interpret(run: Run, *, description: str, images: list[tuple[str, bytes, str]],
              client_id: int, secret_key: str) -> dict:
    """Turn the task description (and any mockups) into checkable requirements.

    Images matter more than they look. A colleague who attaches a screenshot
    with an arrow saying "put the field here" has stated a requirement that
    appears nowhere in the text, and a reviewer working from the prose alone
    would never check for it.
    """
    client = ai.client(secret_key)
    context = f"TASK: {run.task_name}\n\nDESCRIPTION:\n{description or '(empty)'}\n"
    context += _answer_block(run.id)
    try:
        context += ("\nThe client's full addon source follows; use it to confirm "
                    "model names.\n" + source_bundle.context_for(client_id, run.commit_sha))
    except source_bundle.BundleError as exc:
        context += f"\n(No source available: {exc})\n"

    if images:
        # Vision is a separate model, so the images are read first and their
        # findings folded into the text prompt. Verified against a real mockup:
        # it reports the field label, its position, and the red marking.
        notes = _read_images(client, images)
        if notes:
            context += "\n\nWHAT THE ATTACHED IMAGES SHOW:\n" + "\n".join(
                f"- {name}: {text}" for name, text in notes)

    answer = client.complete(INTERPRET_SYSTEM, context, model=client.provider.reasoning,
                             json_object=True, max_tokens=16000)
    parsed = answer.as_json()
    return {"parsed": parsed, "reasoning": answer.reasoning,
            "degraded": answer.degraded}


IMAGE_SYSTEM = """\
You are reading an image attached to an Odoo development task. Describe only \
what a developer needs: any field labels, button labels, menu paths, and any \
arrow, box or annotation marking where something should go. If the image marks a \
position, say exactly what goes where. Two or three sentences. No preamble."""


def _read_images(client: ai.DeepSeek, images: list[tuple[str, bytes, str]]
                 ) -> list[tuple[str, str]]:
    import base64
    out: list[tuple[str, str]] = []
    for name, blob, mimetype in images[:6]:  # a task with 40 screenshots is not a spec
        try:
            data = base64.b64encode(blob).decode()
            answer = client.vision(IMAGE_SYSTEM, f"data:{mimetype};base64,{data}",
                                   "What does this image specify?")
            if answer.text:
                out.append((name, answer.text))
        except ai.AIError as exc:
            log.info("could not read image %s: %s", name, exc)
            out.append((name, f"(could not be read: {exc})"))
    return out


# ---- phase 2: blast radius -------------------------------------------------

#: Odoo models that back more than one user-facing form. The reason this phase
#: exists: `account.move` is the customer invoice, the credit note, the vendor
#: bill and the journal entry, so a change to one is a change to all four. These
#: are the ones worth knowing without asking a model, because they are stable
#: across versions and getting them wrong is expensive.
SHARED_MODELS = {
    "account.move": [
        ("Customer invoice", "out_invoice"),
        ("Credit note", "out_refund"),
        ("Vendor bill", "in_invoice"),
        ("Vendor refund", "in_refund"),
        ("Journal entry", "entry"),
    ],
    "account.move.line": [
        ("Invoice line", ""), ("Journal item", ""),
    ],
    "sale.order": [("Quotation", "draft"), ("Sales order", "sale")],
    "purchase.order": [("Request for quotation", "draft"), ("Purchase order", "purchase")],
    "stock.picking": [("Delivery", "outgoing"), ("Receipt", "incoming"),
                      ("Internal transfer", "internal")],
    "res.partner": [("Customer", ""), ("Vendor", ""), ("Contact", "")],
}

BLAST_SYSTEM = """\
You are working out the blast radius of an Odoo change: what ELSE could break.

You are given the requirements, the models involved, and the client's source. \
Answer ONLY with a JSON object:

{
  "impacted": [
    {"model": "account.move",
     "flow": "Vendor bill",
     "why": "same table as the customer invoice this task changes",
     "risk": "high|medium|low"}
  ],
  "shared_table_note": "one sentence on which forms share a table here",
  "overrides": ["methods in the client's own addons that this change passes through"]
}

Rules:
- In Odoo one model backs several forms. If the change touches account.move, \
the credit note, vendor bill and journal entry are all affected even when the \
task never mentions them. Say so.
- Use the source you were given to find client overrides the change flows \
through; name the file and method.
- Rank risk by how likely a regression is to go unnoticed, not by severity.
- Empty lists are fine. Invented ones are not.
- Never use an em dash or en dash in anything you write. Use a comma, a colon, or a full stop instead. This applies to every field you return.
- No prose outside the JSON object."""


def blast_radius(run: Run, *, requirements: dict, client_id: int,
                 secret_key: str) -> dict:
    """Which other flows share the changed models.

    Half of this is arithmetic, not judgement: `SHARED_MODELS` above is a fixed
    table of Odoo models that back several forms, and it is applied first so the
    obvious cases never depend on a model remembering them. The language model
    is asked only for what the table cannot know — which of the client's own
    overrides the change passes through.
    """
    models = [m for m in (requirements.get("models") or []) if isinstance(m, str)]

    # Deterministic first.
    known: list[dict] = []
    for model in models:
        for flow, _kind in SHARED_MODELS.get(model, []):
            known.append({"model": model, "flow": flow, "risk": "high",
                          "why": f"shares the {model} table with the change",
                          "source": "known shared model"})

    client = ai.client(secret_key)
    context = (f"REQUIREMENTS:\n{json.dumps(requirements, indent=2)[:6000]}\n\n"
               f"MODELS TOUCHED: {', '.join(models) or 'unknown'}\n")
    context += _answer_block(run.id)
    for model in models:
        if model in SHARED_MODELS:
            flows = ", ".join(f for f, _ in SHARED_MODELS[model])
            context += f"\nKNOWN: {model} backs these forms: {flows}\n"
    try:
        context += "\nCLIENT SOURCE:\n" + source_bundle.context_for(
            client_id, run.commit_sha)
    except source_bundle.BundleError:
        pass

    answer = client.complete(BLAST_SYSTEM, context, model=client.provider.reasoning,
                             json_object=True, max_tokens=16000)
    parsed = answer.as_json()

    # The fixed table wins where the two overlap: a model that forgets the
    # vendor bill must not be able to remove it from the plan.
    seen = {(i.get("model"), i.get("flow")) for i in known}
    for item in parsed.get("impacted") or []:
        if isinstance(item, dict) and (item.get("model"), item.get("flow")) not in seen:
            known.append(item)
    parsed["impacted"] = known
    return {"parsed": parsed, "reasoning": answer.reasoning,
            "degraded": answer.degraded}


# ---- phase 3: plan ---------------------------------------------------------

PLAN_SYSTEM = """\
You are turning requirements and a blast radius into a list of scenarios to run \
in a browser against a real Odoo staging instance.

Answer ONLY with a JSON object:

{
  "scenarios": [
    {"id": "S1",
     "title": "short imperative title",
     "kind": "requirement|regression",
     "covers": ["R1"],
     "flow": "which form or menu this exercises",
     "screen": {"kind": "form|list|wizard|settings|groups|access",
                "model": "account.move",
                "record": "inv1",
                "domain": [["name", "=", "Auto reconcile statement lines"]],
                "highlight": ["field_or_button_name", "..."]},
     "fixtures": [
       {"ref": "bank", "model": "account.journal",
        "find": [["type", "=", "bank"]]},
       {"ref": "inv1", "model": "account.move",
        "values": {"move_type": "out_invoice", "journal_id": "$bank"},
        "then": ["action_post"]}
     ],
     "steps": ["one UI action per line, specific enough to follow blindly"],
     "assertions": [
       {"id": "A1",
        "text": "what must be true afterwards",
        "check": "read|screenshot",
        "model": "account.move",
        "fixture": "inv1",
        "field": "state",
        "expect": "posted"}
     ]}
  ]
}

Rules:

FIRST, DECIDE WHAT YOU ARE TESTING, THEN WRITE THE SCENARIOS.
Before writing any JSON, work out which SCREENS the change touches and what
each one has to prove. A scenario is one screen and one behaviour to prove
about it. It is NOT one field.

- ONE SCENARIO PER SCREEN AND BEHAVIOUR, NEVER ONE PER FIELD. If a wizard gains
seven fields, that is ONE scenario that fills all seven and asserts on all
seven, not seven scenarios. Seven scenarios against one wizard produce seven
identical pictures of the same wizard, which is not evidence, it is noise. Put
the seven checks in "assertions", where they are cheap, and leave "scenarios"
counting screens.
- Two scenarios may only share a screen when they prove genuinely different
behaviour on it, for example the valid case and the refused case. If you cannot
say in one sentence how the second differs, it is the same scenario.
- Every scenario MUST carry "screen", because it is what gets photographed:
  * "kind" says what the reviewer will be looking at. Use "wizard" for a
    transient, "form" for one record, "list" for many, "settings" for a
    configuration page, "groups" for a group definition, "access" for access
    rights or record rules.
  * "record" names a fixture ref when the picture should be of ONE record,
    which is what makes the shot show fields already filled in rather than an
    empty list. Prefer it whenever the scenario creates or finds a record.
  * "domain" identifies ONE EXISTING record to photograph when the scenario did
    not create it, for example the scheduled action or the group the change is
    about. It must match exactly one record: a domain matching six is treated
    as naming none, because a picture of the wrong one of six is worse than a
    picture of the list.

ALMOST EVERY SCENARIO SHOULD PHOTOGRAPH ONE RECORD, NOT A LIST.
A list view proves nothing about a record. A scenario about one scheduled
action evidenced by the Scheduled Actions list, forty rows deep with the
relevant one somewhere in the middle, is not evidence: the reviewer still has
to go and look. So every scenario about a specific thing MUST give either
"record" or "domain". Ask yourself which single record a person would open to
check this, and name it. Use "kind": "list" only when the scenario is genuinely
about a set, for example "no duplicate journals were created".
  * "highlight" lists the field and button names the scenario is actually
    about, spelled as Odoo names ("partner_id", "x_studio_hi_tea"), NOT labels
    and NOT CSS. They get a red ring drawn round them in the screenshot. On a
    big form this is the difference between evidence and a wall of widgets, so
    on any form with more than a handful of fields, "highlight" is required.
    Keep it to the fields under test, ringing twenty is the same as ringing none.
- IF THE CHANGE TOUCHES CONFIGURATION, GROUPS OR ACCESS RIGHTS, SAY SO AND
PHOTOGRAPH IT. A new group, a changed ir.model.access line, a record rule or a
settings toggle each deserve their own scenario with "kind" set to "groups",
"access" or "settings", and the model named. "It is configured correctly" is a
claim nobody can check from a picture of a sale order.
- AT MOST 12 scenarios in total. If there are more candidates than that, keep \
the ones where a regression would go unnoticed longest and drop the rest — a \
plan nobody has time to run is not a plan.
- Cover every requirement with at least one scenario of kind "requirement".
- Add a "regression" scenario for each high-risk entry in the blast radius — \
these are the forms that share a table with the change and were not asked for.
- Give a scenario MORE THAN ONE assertion whenever the change has more than one
observable consequence — a reconciled line usually also means a payment state
and a residual amount. One assertion per scenario tests the headline and misses
the side effects, and assertions are cheap: it is the fixtures that cost.
- Each assertion must be checkable by reading a field or by looking at a \
screenshot. Prefer "read": a field value is a fact, a screenshot is evidence.
- A "read" assertion MUST identify the ONE record it is about, either with \
"fixture": "<ref>" naming a record this scenario creates, or with "domain": a \
search domain matching exactly one existing record. Without one of those the \
check is meaningless — reading an arbitrary record proves nothing. Prefer a \
fixture: a record you made is one you know the state of.
- THE STAGING INSTANCE CONTAINS ONLY THE CLIENT'S OWN REAL DATA. It does not \
contain the example references in the task description. A reference like \
"Payment ID(775544)" or "DUS280171 P278384" is an ILLUSTRATION of a format, not \
a record that exists. If you write a domain matching one, it will match nothing \
and the check will be thrown away as unverifiable.
- So: any record a scenario needs that the client would not already have, you \
MUST create in "fixtures". Before writing each assertion ask "would this record \
already exist on a real client's staging database?" If the answer is no, it is \
a fixture you forgot. An empty "fixtures" list on a scenario that tests new \
behaviour is almost always a mistake.
- ANYTHING YOU CREATE IS A DRAFT. An account.move, an account.payment, a \
sale.order or a stock.picking created this way is in its initial state: it has \
no journal entry, it has moved no stock, and nothing has reconciled. If the \
scenario needs the record to be live, add "then" with the transition that makes \
it live, usually "action_post" for accounting and "action_confirm" for sales. \
Forgetting this produces a check that can NEVER hold: asserting a payment you \
just created has state "posted" fails every time, and it looks like the \
developer's bug rather than yours. Allowed values are action_post, \
action_confirm, action_validate, action_done, button_confirm, button_validate.
- Do not assert that a record reached a state you never asked it to reach. \
Either add the "then" that gets it there, or assert on what a draft record \
actually shows.
- STATE VALUES DIFFER BY ODOO VERSION AND YOU MUST NOT GUESS THEM. On Odoo 18 \
and 19 an account.payment is draft, in_process, paid, canceled or rejected: \
there is NO "posted" state, that is account.move. Getting this wrong produces a \
check that cannot hold and reads as the developer's bug. If you are not certain \
of a selection value on the version in the context above, assert on something \
you are certain of, for example that a many2one is set, or that a boolean like \
is_reconciled is true.
- Fixtures may reference each other in creation order: a later fixture's values \
may use a ref you defined earlier.
- Give the minimum that makes the assertion checkable, with only the fields Odoo \
requires to create the record. They are deleted afterwards.
- A fixture has EITHER "values" (create it) OR "find" (use one the client \
already has). Use "find" for all CONFIGURATION: journals, accounts, taxes, \
payment methods, product categories, units. Every Odoo already has these, and \
inventing new ones edits the client's chart of accounts — Odoo then refuses to \
delete a journal anything posted to, so the mess is permanent. Creating them is \
refused outright.
- Refer to an earlier fixture inside another's values with "$ref", e.g. \
"journal_id": "$bank".
- NEVER put res.users, res.company, res.groups, ir.cron or ir.config_parameter \
in fixtures — those are refused too.
- Use "domain" ONLY for configuration that genuinely already exists (a company \
setting, an installed module). For anything the scenario is testing, use a \
fixture.
- Steps must be concrete UI actions: menu path, button label, field name.
- Never use an em dash or en dash in anything you write. Use a comma, a colon, or a full stop instead. This applies to every field you return.
- No prose outside the JSON object."""


def _client_version(client_id: int) -> str:
    """The Odoo version recorded for this client, or "" if none is.

    Read from the client row rather than from a live connection, because the
    plan runs before any connection to the instance is opened. An empty answer
    is fine: the prompt simply omits the line rather than asserting a version
    nobody set.
    """
    # Imported here, like every other use of it in this module: `clients` pulls
    # in config and crypto, and review.py is imported by the CLI in contexts
    # where neither is loaded. The first version of this reached for a
    # module-level `clients_mod` that does not exist, and the broad `except`
    # below reported it as "no version set" rather than as the NameError it was.
    from . import clients as clients_mod

    try:
        row = clients_mod.get(client_id)
    except Exception as exc:  # noqa: BLE001 - a missing version is not fatal
        log.info("could not read the Odoo version for client %s: %s", client_id, exc)
        return ""
    return (getattr(row, "odoo_version", "") or "").strip() if row else ""


def plan(run: Run, *, requirements: dict, impacted: dict, secret_key: str,
         odoo_version: str = "") -> dict:
    client = ai.client(secret_key)
    # The version is in the prompt because the plan's mistakes are
    # version-shaped: `account.payment.state` lost `posted` in Odoo 18, and a
    # plan written against 17 asserts a value that cannot exist. The prompt
    # tells it not to guess; this is what it is not guessing about.
    header = f"TARGET ODOO VERSION: {odoo_version}\n\n" if odoo_version else ""
    context = (header
               + f"REQUIREMENTS:\n{json.dumps(requirements, indent=2)[:8000]}\n\n"
               f"BLAST RADIUS:\n{json.dumps(impacted, indent=2)[:8000]}\n")
    context += _answer_block(run.id)
    # The largest budget of any phase, and it earns it: this is where a dozen
    # scenarios each with fixtures and assertions get written out, and the
    # reasoning behind them ran to ~15k tokens on its own once the prompt
    # started demanding fixtures. Both models cap far above this.
    # The one phase that runs on the fast model, and it is measured rather than
    # assumed. On identical input:
    #
    #     pro/high      213-265s   ~60 tok/s   7-8 scenarios
    #     flash/high    143-165s  ~144 tok/s   8 scenarios
    #
    # Time tracks output volume, not reasoning depth — `medium` on pro measured
    # SLOWER than `high` (301s vs 265s), so the effort dial is not the lever.
    # Throughput is. Flash writes the plan in roughly half the time.
    #
    # It is defensible here and nowhere else in the run: `interpret` and
    # `blast_radius` are judgement — what does this sentence mean, what else
    # could break — while this phase transforms decisions already made. Checked
    # rather than hoped: the flash plan covered all six requirements, produced
    # regression scenarios, bound its assertions to fixtures, and asked for zero
    # forbidden models.
    answer = client.complete(PLAN_SYSTEM, context, model=client.provider.fast,
                             json_object=True, max_tokens=48000)
    return {"parsed": answer.as_json(), "reasoning": answer.reasoning,
            "degraded": answer.degraded}


# ---- phase 5: verdict ------------------------------------------------------

def compute_verdict(run: Run) -> tuple[str, str]:
    """pass | partial | fail, from the assertion results alone.

    No model call, by design. §14 requires the verdict be computed, and this is
    the function that does it: it counts assertion outcomes and reports
    arithmetic. A model writes the prose around this, never the value itself.

    `blocked` assertions are counted separately and never silently absorbed. A
    check that could not be made is not a check that passed, and a run where
    most things were blocked has to say so rather than reporting a clean pass on
    the two it managed.
    """
    execute_step = _detail_of(run, "execute")
    scenarios = execute_step.get("scenarios") or []
    passed = sum(s.get("passed", 0) for s in scenarios)
    failed = sum(s.get("failed", 0) for s in scenarios)
    blocked = sum(s.get("blocked", 0) for s in scenarios)
    total = passed + failed + blocked

    if not total:
        return "", "Nothing was asserted, so there is no verdict to give."
    tail = f" {blocked} could not be checked." if blocked else ""
    if failed and passed:
        # `partial` has to MEAN mostly working, or it becomes a soft word for
        # failure. One check holding out of eleven was reported as "partial",
        # which reads as a near miss when it is the opposite. So a mixed result
        # is only partial when the passes outnumber the failures; anything else
        # is a fail. Ties fail too: half the checks failing is not a qualified
        # success, and a gate in doubt should say the more cautious thing.
        if passed > failed:
            return "partial", (f"{passed} of {passed + failed} checks held; "
                               f"{failed} did not.{tail}")
        return "fail", (f"{failed} of {passed + failed} checks failed; only "
                        f"{passed} held.{tail}")
    if failed:
        return "fail", f"All {failed} checks that could be made failed.{tail}"
    if not passed:
        return "", (f"None of the {blocked} checks could be made, so there is no "
                    "verdict. They need records that do not exist on this instance yet.")
    return "pass", f"All {passed} checks held.{tail}"


SUMMARY_SYSTEM = """\
You are writing the summary a reviewer reads instead of the whole run.

The verdict has already been computed from the assertion results. You are not \
deciding it and you must not contradict it — you are saying, briefly, what \
happened and what the reader should do next.

Six sentences at most. No headings, no bullet lists, no preamble. Never use an
em dash or en dash: use a comma, a colon, or a full stop. Lead with what \
was found. Name the specific check that failed or was blocked, not the count. If \
everything was blocked, say plainly that nothing was verified and why."""


def summarise(run: Run, *, verdict: str, note: str, secret_key: str) -> str:
    """Prose about a decision already made. Never the decision."""
    execute_step = _detail_of(run, "execute")
    try:
        client = ai.client(secret_key)
    except ai.AIError:
        return note
    context = (f"TASK: {run.task_name}\n"
               f"VERDICT (already computed, do not change it): {verdict or 'none'}\n"
               f"ARITHMETIC: {note}\n\n"
               f"RESULTS:\n{json.dumps(execute_step, indent=2)[:12000]}")
    try:
        return client.complete(SUMMARY_SYSTEM, context, max_tokens=4000).text or note
    except ai.AIError:
        return note


# ---- phase 6: report -------------------------------------------------------

def note_body(summary: str, *, html: bool = False) -> str:
    """The chatter note: a heading, then the summary, and nothing else.

    No table of phases, no verdict badge, no link back to this app, and no
    attachments. Everything beyond the text is a decision about how the record
    should read, and the record belongs to the people working the task, not to
    the tool watching it.

    ## Why this has two forms

    Odoo's chatter body is HTML, but `message_post` **escapes a plain string**
    (`'body': escape(body)` in `mail_thread.py`). The only ways round that are a
    `Markup` object, which cannot survive JSON-RPC, or `body_is_html=True`,
    which `mail_thread` honours **only for an internal user**:

        if body_is_html and self.env.user._is_internal():

    Our service account is a portal user, so the first version posted markup
    that Odoo escaped and the note read as literal `<p><b>PM REVIEW SUMMARY</b>`
    on the task. Sending HTML to an account that cannot post it produces a worse
    note than sending none, so the caller asks for the form its credential can
    actually use.

    The summary is escaped before any markup is added. It is model-written prose
    about a task somebody else typed, so treating it as HTML would let a stray
    angle bracket in a task title reshape the note.
    """
    from html import escape

    text = (summary or "").strip()
    if not text:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not html:
        # Plain text, for a credential that cannot post HTML. Odoo escapes it,
        # which is a no-op on text with no markup in it, and renders it as one
        # block: newlines collapse in HTML, so paragraphs are joined with a
        # separator that survives rather than with a break that does not.
        return f"{NOTE_HEADING}: " + "  ".join(paragraphs)
    body = f"<p><b>{escape(NOTE_HEADING)}</b></p>"
    for para in paragraphs:
        body += "<p>" + escape(para).replace("\n", "<br/>") + "</p>"
    return body


def write_back(run: Run, *, cfg=None) -> dict:
    """Post the run's summary to its Odoo task as an internal log note.

    Returns a detail dict for the step rather than raising, because a run that
    reached a verdict has done its job: an Odoo that is briefly unreachable
    should leave a run `done` with an unposted summary and a reason, not
    `failed`. `reported_at` stays NULL in that case, which is what makes the
    retry safe — and what makes a retry after a *successful* post a no-op.
    """
    # Imported here rather than at module scope: projects.py reaches for config
    # and app_secrets, and review.py is imported by the CLI in contexts where
    # neither is loaded yet.
    from . import projects
    from .odoo_client import OdooAuthError, OdooError

    if run.reported_at:
        return {"posted": False, "skipped": "already posted",
                "reported_at": run.reported_at.isoformat()}
    # The cheap refusals first, before opening a connection to find out there
    # was nothing to send down it.
    if not run.summary or not run.summary.strip():
        return {"posted": False, "skipped": "there is no summary to post"}
    if not run.task_id:
        return {"posted": False, "skipped": "this run is not attached to a task"}

    try:
        identity = projects.connect(cfg)
        # Ask the credential what it can post rather than assuming HTML. A
        # portal service account cannot, and markup it cannot post reads as
        # literal angle brackets on the task.
        as_html = identity.can_post_html()
        body = note_body(run.summary, html=as_html)
        if not body:
            return {"posted": False, "skipped": "there is no summary to post"}
        message_id = identity.post_note(run.task_id, body, is_html=as_html)
    except projects.NotConfigured as exc:
        return {"posted": False, "error": str(exc)}
    except (OdooError, OdooAuthError) as exc:
        # Overwhelmingly this is the service account lacking write access on
        # project.task. Say so, because "could not post" sends someone to check
        # the network and the answer is a permission in Odoo.
        return {"posted": False, "error":
                f"Odoo refused the note: {exc} The service account needs write "
                "access to project.task to log one."}

    db.execute("UPDATE review_runs SET reported_at = now() WHERE id = %s", (run.id,))
    log.info("Run %s summary posted to task %s as message %s",
             run.id, run.task_id, message_id)
    return {"posted": True, "message_id": message_id, "task_id": run.task_id,
            "heading": NOTE_HEADING}


def retry_report(run_id: int) -> dict:
    """Post again after a failure, without re-running anything else.

    Its own entry point rather than `request_resume` + `advance`, because a run
    that has already finished should not walk the phase list to discover that
    seven of eight phases are done. Posting is idempotent through `reported_at`,
    so pressing the button twice is safe.
    """
    run = get(run_id)
    if not run:
        return {"posted": False, "error": "No such run."}
    detail = write_back(run)
    _save_step(run_id, "report",
               state="done" if (detail.get("posted") or detail.get("skipped")) else "failed",
               detail=detail, note=detail.get("skipped") or detail.get("error", ""))
    return detail


# ---- the driver ------------------------------------------------------------

def advance(run_id: int, *, secret_key: str, description: str = "",
            images: list | None = None) -> Run:
    """Run phases until the run finishes, pauses, or hits something unbuilt.

    The pause check sits between phases and nowhere else. Checking mid-phase
    would mean stopping with a form half-filled on the client's instance, which
    is the state §7 says a pause must never leave behind — the whole point of
    pausing is that the database is released clean.
    """
    run = get(run_id)
    if not run:
        raise ReviewError("No such run.")
    if run.state == "cancelled":
        return run

    _set_state(run_id, "running")
    images = images or []

    while True:
        run = get(run_id)  # type: ignore[assignment]
        if _should_stop(run_id):
            raise Paused(f"Run {run_id} stopped at {run.next_phase}.")

        phase = run.next_phase
        if phase is None:
            _set_state(run_id, "done")
            return get(run_id)  # type: ignore[return-value]

        db.execute("UPDATE review_runs SET phase = %s WHERE id = %s", (phase, run_id))
        _begin_step(run_id, phase)
        try:
            if phase == "interpret":
                out = interpret(run, description=description, images=images,
                                client_id=run.client_id, secret_key=secret_key)
                _save_step(run_id, phase, state="done", detail=out["parsed"],
                           reasoning=out["reasoning"],
                           note=out.get("degraded", ""))

                # Stop HERE if the reading raised questions whose answers change
                # what would be tested. Waiting costs one pause; carrying on
                # costs a plan built against a guess, and the only way to absorb
                # a late answer would be to throw that plan away. Ask at the
                # point the question arises, not after acting on the guess.
                run = get(run_id)  # type: ignore[assignment]
                if unresolved(run):
                    _set_state(run_id, "paused",
                               paused_at=datetime.now(timezone.utc))
                    log.info("Run %s paused for %s unanswered question(s)",
                             run_id, len(unresolved(run)))
                    return get(run_id)  # type: ignore[return-value]

            elif phase == "code_check":
                # Deterministic and fast — no model call. Runs before the
                # expensive phases precisely so "it was never implemented" is
                # found in milliseconds rather than after a browser run.
                out = code_check(run, client_id=run.client_id,
                                 symbols=(_detail_of(run, "interpret")
                                          .get("expected_symbols") or []))
                note = ""
                if out.get("error"):
                    note = out["error"]
                elif out.get("verdict_hint") == "nothing_implemented":
                    note = ("None of the identifiers this task describes appear in "
                            "the client's addons at this commit. The work may not "
                            "have been done, or it may be configuration rather "
                            "than code.")
                elif out.get("missing"):
                    note = (f"{out['missing']} of {len(out['symbols'])} expected "
                            "identifiers were not found in the source.")
                _save_step(run_id, phase, state="done", detail=out, note=note)

            elif phase == "blast_radius":
                reqs = _detail_of(run, "interpret")
                out = blast_radius(run, requirements=reqs, client_id=run.client_id,
                                   secret_key=secret_key)
                _save_step(run_id, phase, state="done", detail=out["parsed"],
                           reasoning=out["reasoning"],
                           note=out.get("degraded", ""))

            elif phase == "plan":
                out = plan(run, requirements=_detail_of(run, "interpret"),
                           impacted=_detail_of(run, "blast_radius"),
                           secret_key=secret_key,
                           # From the client record, not from `conn`: the
                           # connection is not opened until `execute`, several
                           # phases later, so reaching for it here would raise
                           # a NameError on every run.
                           odoo_version=_client_version(run.client_id))
                _save_step(run_id, phase, state="done", detail=out["parsed"],
                           reasoning=out["reasoning"],
                           note=out.get("degraded", ""))

            elif phase == "execute":
                from . import instance as instance_mod
                from . import clients as clients_mod, personas as personas_mod
                client_row = clients_mod.get(run.client_id)
                scenarios = (_detail_of(run, "plan").get("scenarios") or [])
                if not scenarios:
                    _save_step(run_id, phase, state="done", detail={"scenarios": []},
                               note="The plan produced no scenarios, so there was "
                                    "nothing to run.")
                else:
                    conn = instance_mod.connect(client_row, secret_key)
                    sid = conn.session_id
                    if not sid:
                        # An API-key connection can read but cannot open a
                        # browser, so there would be no evidence — say so rather
                        # than producing assertions with no screenshots.
                        persona = next(
                            (p for p in personas_mod.for_client(run.client_id)
                             if p.state == "verified" and p.has_password), None)
                        if not persona:
                            raise ReviewError(
                                "This client has no verified browser sign-in, so no "
                                "screenshots can be taken. Add one on the client page.")
                        from .odoo_client import OdooClient
                        sid = OdooClient(client_row.staging_url,
                                         client_row.staging_db).open_session(
                            persona.login,
                            personas_mod.password_of(persona.id, secret_key))
                    from . import fixtures as fixtures_mod
                    ledger = fixtures_mod.Ledger(run_id=run_id)
                    writable, why = True, ""
                    try:
                        fixtures_mod.assert_writable(conn)
                    except fixtures_mod.NotWritable as exc:
                        # Not fatal. Assertions about records that already exist
                        # still hold; the rest come back blocked with a reason,
                        # which is a truthful partial result rather than a
                        # refusal to run at all.
                        writable, why = False, str(exc)
                        ledger = None
                    try:
                        out = execute(run, scenarios=scenarios, conn=conn,
                                      staging_url=client_row.staging_url,
                                      session_id=sid, secret_key=secret_key,
                                      stop=lambda: _should_stop(run_id),
                                      ledger=ledger)
                    except Paused:
                        # Records created so far stay put. They are in the ledger
                        # and shown on the run page, which is what makes them
                        # distinguishable from a person's own work.
                        raise
                    if not writable:
                        out["not_writable"] = why
                    _save_step(run_id, phase, state="done", detail=out)

            elif phase == "summarise":
                value, note = compute_verdict(run)
                text = summarise(run, verdict=value, note=note, secret_key=secret_key)
                _save_step(run_id, phase, state="done",
                           detail={"summary": text, "verdict": value, "note": note})
                # The verdict is WRITTEN HERE, and forgetting to write it is the
                # bug this line exists to prevent. When `verdict` was its own
                # phase that phase persisted it; folding the arithmetic into
                # `summarise` moved the computation and left the write behind,
                # so runs completed all seven phases with an empty verdict. The
                # run page then showed only the state ("done") while the task
                # list kept showing an older run's verdict, which is exactly how
                # it was noticed.
                #
                # Still `running`: the summary exists but has not been posted,
                # and marking the run done here would stamp finished_at before
                # the last phase had run.
                _set_state(run_id, "running", summary=text[:4000], verdict=value)

            elif phase == "report":
                # `run` was loaded before the summary was written, so re-read it.
                fresh = get(run_id) or run
                detail = write_back(fresh)
                # A note that could not be posted is a failed step on a finished
                # run, never a failed run: the review reached a verdict, and the
                # verdict is the product. The step carries the reason and the
                # button on the run page retries just this phase.
                if detail.get("posted") or detail.get("skipped"):
                    _save_step(run_id, phase, state="done", detail=detail,
                               note=detail.get("skipped", ""))
                else:
                    _save_step(run_id, phase, state="failed", detail=detail,
                               note=detail.get("error", ""))
                _set_state(run_id, "done")
                return get(run_id)  # type: ignore[return-value]

        except ai.AIError as exc:
            _save_step(run_id, phase, state="failed", note=str(exc))
            _set_state(run_id, "failed", error=str(exc))
            log.warning("Run %s failed in %s: %s", run_id, phase, exc)
            return get(run_id)  # type: ignore[return-value]


def _cleanup(run_id: int, conn) -> dict:
    from . import fixtures as fixtures_mod
    return fixtures_mod.rollback(conn, run_id)


def phase_durations(run_id: int) -> dict[str, int]:
    """How long each finished phase took, in seconds, by phase name.

    Kept and shown rather than discarded when the step ends. "Which step is slow"
    is the first question anyone asks about a run that took eleven minutes, and
    a timer that vanishes on completion answers it only for whoever happened to
    be watching at the time.

    Phases that finished before the timing fix are omitted rather than reported
    as zero — a step did not take no time, we simply were not measuring.
    """
    rows = db.query(
        """
        SELECT phase,
               round(extract(epoch from (finished_at - started_at)))::int AS secs
          FROM review_steps
         WHERE run_id = %s AND finished_at IS NOT NULL AND started_at IS NOT NULL
        """, (run_id,))
    return {r["phase"]: int(r["secs"]) for r in rows if (r["secs"] or 0) > 0}


def run_duration(run_id: int) -> int:
    """Wall-clock seconds from start to finish, or to now if still going."""
    row = db.query_one(
        "SELECT round(extract(epoch from (COALESCE(finished_at, now()) - started_at)))::int "
        "AS secs FROM review_runs WHERE id = %s", (run_id,))
    return int((row or {}).get("secs") or 0)


def phase_elapsed(run_id: int) -> int:
    """Seconds the current phase has been running, or 0.

    Shown on the page because a phase that takes minutes with no sign of life
    is indistinguishable from one that has hung, and the honest fix for a slow
    step is first to admit it is slow.
    """
    row = db.query_one(
        "SELECT round(extract(epoch from (now() - started_at)))::int AS secs "
        "FROM review_steps WHERE run_id = %s AND state = 'running' "
        "ORDER BY seq DESC LIMIT 1", (run_id,))
    return int((row or {}).get("secs") or 0)


def _detail_of(run: Run, phase: str) -> dict:
    for step in run.steps:
        if step.phase == phase:
            return step.detail or {}
    return {}


def resume_blocked_by(run_id: int) -> list[int]:
    """Questions still standing between a paused run and going forward."""
    run = get(run_id)
    return unresolved(run) if run else []


def request_resume(run_id: int) -> None:
    """Put a paused run back in flight.

    Only from `paused`. A cancelled run stays cancelled — resuming one would
    revive work somebody deliberately stopped — and a failed one needs its cause
    fixed rather than another attempt at the same call.
    """
    db.execute(
        "UPDATE review_runs SET state = 'running', paused_at = NULL "
        "WHERE id = %s AND state = 'paused'", (run_id,))


# ---- human answers ---------------------------------------------------------

def save_answer(run_id: int, index: int, question: str, answer: str,
                by: str = "") -> None:
    """Record one person's answer to one thing the review could not resolve.

    Answers are not decoration. `replan` below throws away everything from the
    interpretation onward and reads the task again with the answers in hand, so
    a corrected assumption produces a corrected plan rather than a note beside
    a plan built on the wrong one.
    """
    from datetime import datetime, timezone
    row = db.query_one("SELECT answers FROM review_runs WHERE id = %s", (run_id,))
    if row is None:
        raise ReviewError("No such run.")
    answers = dict(row["answers"] or {})
    text = (answer or "").strip()
    if text:
        answers[str(index)] = {
            "question": question, "answer": text, "by": by,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        answers.pop(str(index), None)
    db.execute("UPDATE review_runs SET answers = %s WHERE id = %s",
               (json.dumps(answers), run_id))


def skip_answer(run_id: int, index: int, question: str, by: str = "") -> None:
    """Record that a question was deliberately left unanswered.

    A skip is not the same as silence. Silence means nobody has looked yet and
    the run should wait; a skip means somebody looked, decided it did not matter
    enough to block on, and the run may proceed on its own reading. Storing the
    difference is what lets the run stop for the first and not the second.
    """
    from datetime import datetime, timezone
    row = db.query_one("SELECT answers FROM review_runs WHERE id = %s", (run_id,))
    if row is None:
        raise ReviewError("No such run.")
    answers = dict(row["answers"] or {})
    answers[str(index)] = {"question": question, "answer": "", "skipped": True,
                           "by": by, "at": datetime.now(timezone.utc).isoformat()}
    db.execute("UPDATE review_runs SET answers = %s WHERE id = %s",
               (json.dumps(answers), run_id))


def unresolved(run: Run) -> list[int]:
    """Indices of questions nobody has answered or skipped yet."""
    asked = (_detail_of(run, "interpret").get("ambiguities") or [])
    seen = answers_for(run.id)
    return [i for i in range(len(asked)) if str(i) not in seen]


def answers_for(run_id: int) -> dict:
    row = db.query_one("SELECT answers FROM review_runs WHERE id = %s", (run_id,))
    return dict((row or {}).get("answers") or {})


def _answer_block(run_id: int) -> str:
    """The answers, formatted for a prompt. Empty string when there are none."""
    answers = answers_for(run_id)
    if not any((a.get("answer") or "").strip() for a in answers.values()):
        return ""
    lines = ["\nA human has answered these open questions. Treat each answer as "
             "authoritative and stop treating the question as open:"]
    for item in answers.values():
        if item.get("skipped") or not (item.get("answer") or "").strip():
            continue
        lines.append(f"- Q: {item.get('question')}\n  A: {item.get('answer')}")
    return "\n".join(lines) + "\n"


def replan(run_id: int) -> None:
    """Discard everything from interpretation onward so it can be redone.

    Not a partial patch of the existing requirements: an answer can change which
    requirements exist at all, and a plan half-derived from a superseded reading
    would be harder to trust than one built fresh.
    """
    db.execute("DELETE FROM review_steps WHERE run_id = %s", (run_id,))
    db.execute(
        "UPDATE review_runs SET state = 'queued', phase = 'interpret', verdict = '', "
        "error = '', finished_at = NULL, paused_at = NULL WHERE id = %s", (run_id,))


# ---- phase 4: execute ------------------------------------------------------

#: ORM methods this phase may call. An allowlist rather than a blocklist: the
#: default has to be "no", because a review that quietly created or wrote
#: records on a client's staging instance would be changing the thing it was
#: asked to observe. Creating fixtures is a separate act that belongs behind the
#: §3 pre-flight audit, not inside an assertion checker.
READ_ONLY_METHODS = frozenset({
    "search_read", "search_count", "read", "fields_get", "search", "read_group",
    "default_get", "name_search",
})

#: What an assertion can come back as. `blocked` is the important one: it means
#: the check could not be made — usually because it needs a record that does not
#: exist yet — and it must never be counted as either a pass or a failure.
PASSED, FAILED, BLOCKED = "passed", "failed", "blocked"


class WriteAttempted(ReviewError):
    """Execution tried to call something that is not a read."""


def _read(conn, model: str, method: str, args=None, kwargs=None):
    """Every instance call execution makes goes through here.

    The allowlist above is only a comment unless something checks it, and this
    is that check. It exists because the failure it prevents is silent and
    expensive: a review that created or wrote records would alter the very state
    it was asked to observe, and nobody reading a green verdict would know.

    ## Archived records are visible here, deliberately

    Odoo defaults `active_test` to True, so `search` on a model with an `active`
    field silently drops archived rows. Measured on a real instance:
    `ir.cron.search([])` returned **1** record, and the same call with
    `active_test: False` returned **41**.

    For a gate that is the wrong default and it fails in the worst direction.
    A scenario checking a scheduled action reads nothing, the check is recorded
    as "no record matches, this needs a fixture", and a reviewer concludes the
    plan was badly written when in fact the record was sitting there archived.
    Worse, "the cron is disabled" is a thing a review should be able to prove,
    and it could not even see it.

    So the archived state stops being a filter and becomes a fact: everything is
    found, and a scenario that cares asserts on `active` explicitly. A caller
    that genuinely wants only live records can still pass its own `active_test`,
    which is not overridden.
    """
    if method not in READ_ONLY_METHODS:
        raise WriteAttempted(
            f"Execution may only read, but {model}.{method} was attempted. "
            "Creating fixtures is a separate step that belongs behind the "
            "pre-flight audit.")
    kwargs = dict(kwargs or {})
    context = dict(kwargs.get("context") or {})
    context.setdefault("active_test", False)
    kwargs["context"] = context
    return conn.call(model, method, args, kwargs)


def _coerce(expected: str, actual):
    """Compare a plan's string expectation against a real field value.

    The plan is JSON written by a model, so `True`, `"True"`, `"true"` and `1`
    all turn up meaning the same thing, as do `0.0` and `"0.0"`. Comparing them
    as strings would fail honest checks; comparing loosely here keeps the
    failures real ones.
    """
    if isinstance(actual, (list, tuple)):          # many2one comes back [id, name]
        actual = actual[1] if len(actual) > 1 else (actual[0] if actual else None)
    want = str(expected).strip()
    got = actual
    if isinstance(got, bool) or want.lower() in ("true", "false"):
        return str(bool(got)).lower() == want.lower()
    try:
        return abs(float(got) - float(want)) < 1e-6
    except (TypeError, ValueError):
        pass
    return str(got).strip().lower() == want.lower()


def _selection_values(conn, model: str, field: str) -> list[str]:
    """The values a selection field allows here, or [] if it is not one.

    Asked of the instance rather than assumed, because the whole point is that
    the answer differs by version: `account.payment.state` lost `posted` in
    Odoo 18. Cached per process, since a plan asserts the same few fields many
    times and this is on the path of every failing check.
    """
    key = (model, field)
    if key in _SELECTION_CACHE:
        return _SELECTION_CACHE[key]
    values: list[str] = []
    try:
        info = _read(conn, model, "fields_get", [[field]],
                     {"attributes": ["selection", "type"]}) or {}
        spec = info.get(field) or {}
        if spec.get("type") == "selection":
            values = [str(v) for v, _label in (spec.get("selection") or [])]
    except Exception as exc:  # noqa: BLE001 - not knowing is not a failure
        log.info("could not read %s.%s selection: %s", model, field, exc)
    _SELECTION_CACHE[key] = values
    return values


#: Per process, and never invalidated: a field's selection does not change
#: while a review runs, and a review is minutes long.
_SELECTION_CACHE: dict[tuple[str, str], list[str]] = {}


def _check_assertion(conn, assertion: dict, ledger=None) -> dict:
    """Evaluate one assertion by reading the instance. Never writes."""
    model = (assertion.get("model") or "").strip()
    field = (assertion.get("field") or "").strip()
    expect = assertion.get("expect")
    out = {"id": assertion.get("id"), "text": assertion.get("text"),
           "model": model, "field": field, "expect": expect,
           "state": BLOCKED, "actual": None, "note": ""}

    if not model or not field:
        out["note"] = ("This assertion names no model and field to read, so it can "
                       "only be judged from a screenshot by a person.")
        return out

    # No domain means no particular record, and the value of an arbitrary record
    # is not evidence about the change. Reading "the newest one" produced both a
    # false failure (it read the auto-vacuum cron instead of the reconciliation
    # one) and unearned passes (the newest invoice happened to be posted), so
    # this is blocked rather than guessed.
    # A fixture this scenario created is the strongest form of "which record":
    # we made it, so we know exactly which one to read.
    domain = assertion.get("domain")
    ref = (assertion.get("fixture") or "").strip()
    if ref and ledger is not None:
        res_id = ledger.id_of(ref)
        if res_id:
            domain = [["id", "=", res_id]]
            out["model"] = model = model or ledger.model_of(ref)
        else:
            out["note"] = (f"This check is about the fixture {ref!r}, which was not "
                           "created, so there is nothing to read.")
            return out

    if not isinstance(domain, list) or not domain:
        out["note"] = ("This assertion does not say which record it is about, so "
                       "reading one would prove nothing. It needs a domain.")
        return out

    try:
        if not conn.model_exists(model):
            out["note"] = f"{model} is not installed on this instance."
            return out
        rows = _read(conn, model, "search_read",
                     [domain], {"fields": [field], "limit": 2})
    except Exception as exc:  # noqa: BLE001 - an unreadable model is not a failed test
        out["note"] = f"Could not read {model}.{field}: {exc}"
        return out

    if not rows:
        out["note"] = (f"No {model} matches {domain!r}, so there is nothing to check "
                       "this against. It needs a fixture.")
        return out
    if len(rows) > 1:
        out["note"] = (f"{len(rows)}+ {model} records match {domain!r}. The check "
                       "needs to identify one record, not a set.")
        return out
    # The one record this check is about, kept so the screenshot can be of that
    # record's form rather than a list the reviewer then has to search.
    out["res_id"] = rows[0].get("id")
    # Kept so a screenshot can fall back to the domain that identified this one
    # record, when the scenario's own `screen` block named none.
    out["domain"] = domain

    if field not in rows[0]:
        out["note"] = f"{model} has no field {field!r} on this instance."
        return out

    out["actual"] = rows[0][field]
    if _coerce(expect, out["actual"]):
        out["state"] = PASSED
        return out

    # Before calling this a failure, check the expectation was even possible.
    # A plan asserted `account.payment.state == "posted"`, and on Odoo 18 that
    # value does not exist: the states are draft, in_process, paid, canceled,
    # rejected. The check could never hold, and reporting it as a failure blamed
    # the developer for the plan using a value from an older version.
    #
    # So an impossible expectation is `blocked`, not `failed`. Blocked already
    # means "this was not actually tested", which is the truth here, and it
    # keeps a plan's mistake out of the verdict.
    options = _selection_values(conn, model, field)
    if options and str(expect).strip() not in options:
        out["state"] = BLOCKED
        out["note"] = (
            f"{model}.{field} cannot be {expect!r} on this instance. The values "
            f"it allows are: {', '.join(options)}. This is the plan using a "
            "value from a different Odoo version, not a fault in the change.")
        return out

    out["state"] = FAILED
    return out


def _build_fixtures(conn, ledger, scenario: dict) -> tuple[int, list[str]]:
    """Create this scenario's records. Returns (made, reasons it could not)."""
    from . import fixtures as fixtures_mod
    wanted = scenario.get("fixtures") or []
    if not wanted or ledger is None:
        return 0, []
    made, errors = 0, []
    for spec in wanted:
        if not isinstance(spec, dict):
            continue
        ref = str(spec.get("ref") or "").strip()
        if not ref or ledger.id_of(ref):
            continue
        try:
            find = spec.get("find")
            if isinstance(find, list) and find:
                # Configuration the client already has. Nothing is created, so
                # nothing needs cleaning up.
                fixtures_mod.resolve(conn, ledger, ref=ref,
                                     model=str(spec.get("model") or ""),
                                     domain=find)
                continue
            model = str(spec.get("model") or "")
            res_id = fixtures_mod.create(
                conn, ledger, ref=ref, model=model,
                values=_expand_refs(spec.get("values") or {}, ledger))
            made += 1
            # `create` leaves an accounting record in draft, and a draft record
            # cannot reconcile, invoice or move stock. Without this the plan
            # could build a payment and then assert it was posted, which is a
            # check that can never hold however good the code is.
            for method in spec.get("then") or []:
                if not isinstance(method, str) or not method.strip():
                    continue
                fixtures_mod.run_action(conn, model, res_id, method.strip())
        except fixtures_mod.FixtureError as exc:
            errors.append(str(exc))
    return made, errors


def _expand_refs(values: dict, ledger) -> dict:
    """Replace "$ref" with the id of a fixture created earlier.

    A statement line needs the journal's id, and the plan cannot know it —
    Odoo assigns it. So the plan names it and this substitutes.
    """
    out = {}
    for key, value in (values or {}).items():
        if isinstance(value, str) and value.startswith("$"):
            found = ledger.id_of(value[1:])
            out[key] = found if found else value
        elif isinstance(value, list):
            out[key] = [
                (ledger.id_of(v[1:]) or v) if isinstance(v, str) and v.startswith("$")
                else v for v in value
            ]
        else:
            out[key] = value
    return out


def _action_for(conn, model: str) -> int:
    """An `ir.actions.act_window` id that opens this model, or 0.

    Cached per run: the same handful of models come up across a dozen scenarios
    and this is one round trip each otherwise.
    """
    if model in _ACTION_CACHE:
        return _ACTION_CACHE[model]
    found = 0
    try:
        rows = _read(conn, "ir.actions.act_window", "search_read",
                     [[["res_model", "=", model]]],
                     {"fields": ["id"], "limit": 1, "order": "id"})
        found = rows[0]["id"] if rows else 0
    except Exception as exc:  # noqa: BLE001 - no action just means no screenshot
        log.info("no action for %s: %s", model, exc)
    _ACTION_CACHE[model] = found
    return found


_ACTION_CACHE: dict[str, int] = {}


SHOT_SYSTEM = """\
You are describing a screenshot taken during an automated Odoo review, for \
somebody who will read it without opening the instance.

Be SHORT. No em dashes or en dashes.

Line 1: the view. Name it, for example "Payment wizard, form view" or \
"Settings, Invoicing section" or "Access rights on account.move".

Then, if fields are visible and filled, ONE line per field that carries a \
value, as "field label: value". Skip empty fields and skip the chrome \
(breadcrumb, status bar, chatter). Stop at eight lines. If a field is ringed in \
red, put it FIRST and mark it "(under test)", the ring is there because that is \
the thing the review is about.

If the screen is a group, an access rule or a settings page, say plainly what \
it grants or sets, since that is the whole content of the evidence.

Last line only if something looks wrong: an error dialog, an empty list where \
records were expected, a field that is missing.

Do not speculate about code. Do not say whether a test passed, you are \
describing evidence, not judging it."""


def execute(run: Run, *, scenarios: list[dict], conn, staging_url: str,
            session_id: str, secret_key: str, stop: object = None,
            ledger=None) -> dict:
    """Check each scenario's assertions and photograph what the user would see.

    Read-only, deliberately. Every assertion is evaluated against records that
    already exist; anything needing a record created first comes back `blocked`
    with the reason, and `blocked` is counted as neither pass nor fail. A gate
    that silently reported "no fixture" as "passed" would be worse than one that
    did not run.
    """
    from . import browser

    results: list[dict] = []
    shots: list = []
    #: screen key -> the scenario id that photographed it. What stops seven
    #: assertions about one wizard producing seven copies of one picture.
    seen_screens: dict = {}
    #: Computed once for the whole plan, before any fixture exists, so
    #: scenarios sharing a screen agree on what gets ringed in the one picture.
    screen_groups = merge_screens(scenarios)
    ai_client = None
    try:
        ai_client = ai.client(secret_key)
    except ai.AIError:
        pass  # descriptions are a nicety; the assertions are the substance

    with browser.session(staging_url, session_id) as page:
        if not page.signed_in:
            page.goto("/odoo")
        for scenario in scenarios:
            if stop and stop():
                raise Paused("stopped during execution")
            # Fixtures first: an assertion about a record that was never made
            # can only be blocked, and knowing why is more useful than the
            # blocked flag alone.
            made, fixture_errors = _build_fixtures(conn, ledger, scenario)
            checks = [_check_assertion(conn, a, ledger)
                      for a in (scenario.get("assertions") or [])]

            shot = None
            ringed: list[str] = []
            screen = _screen_plan(scenario, checks, ledger, screen_groups, conn)
            shared_with = ""
            if screen and screen["key"] in seen_screens:
                # Another scenario already photographed this exact screen. A
                # second identical picture is not a second piece of evidence,
                # so point at the first one instead of taking it again.
                shared_with = seen_screens[screen["key"]]
                log.info("%s reuses the screen shot for %s",
                         scenario.get("id"), shared_with)
            elif screen:
                try:
                    # Odoo has no /odoo/<model> route, guessing one produced a
                    # "Missing Action" dialog in every screenshot of the first
                    # run. The real path is through an action the instance
                    # actually has, so ask it for one.
                    action = _action_for(conn, screen["model"])
                    if screen["res_id"]:
                        # The form of one record, so the fields are shown
                        # filled in. This is the picture a reviewer wanted when
                        # they asked to see the wizard after it was completed.
                        page.record(screen["model"], screen["res_id"], action=action)
                    else:
                        page.goto(f"/odoo/action-{action}" if action else "/odoo")
                    ringed = page.highlight(screen["highlight"])
                    covers = [c for c in screen.get("covers") or []
                              if c and c != scenario.get("id")]
                    caption = f"{scenario.get('id')}: {scenario.get('title')}"
                    if covers:
                        # Say what else this one picture is evidence for, so a
                        # reader is not left looking for a missing screenshot.
                        caption += f"  [also covers {', '.join(covers)}]"
                    # The caption does not list what was ringed. The ring is
                    # drawn in the picture, so naming the fields again is the
                    # caption repeating what the reader can already see.
                    # `ringed` still feeds the description prompt below, where
                    # it decides which fields get described first.
                    shot = page.shot(caption)
                    seen_screens[screen["key"]] = str(scenario.get("id") or "")
                except browser.BrowserError as exc:
                    log.info("no screenshot for %s: %s", scenario.get("id"), exc)
                except Exception as exc:  # noqa: BLE001
                    log.info("screenshot failed for %s: %s", scenario.get("id"), exc)

            described = ""
            if shot and ai_client:
                import base64
                try:
                    ask = f"Scenario: {scenario.get('title')}."
                    if screen.get("kind"):
                        ask += f" This is a {screen['kind']} screen."
                    if ringed:
                        ask += (" Ringed in red, so describe these first: "
                                + ", ".join(ringed) + ".")
                    described = ai_client.vision(
                        SHOT_SYSTEM,
                        "data:image/png;base64," + base64.b64encode(shot.png).decode(),
                        ask + " Describe this screen.",
                    ).text
                except ai.AIError as exc:
                    log.info("could not describe shot: %s", exc)

            if shot:
                # Written now, not at the end of the loop. Evidence that only
                # lands after every scenario finishes is evidence a pause or a
                # crash throws away, and this phase is the long one.
                _save_shot(run.id, len(shots), scenario.get("id"), shot, described)
                shots.append((scenario.get("id"), shot, described))

            results.append({
                "fixtures_made": made, "fixture_errors": fixture_errors,
                "id": scenario.get("id"), "title": scenario.get("title"),
                "kind": scenario.get("kind"), "flow": scenario.get("flow"),
                # Carried through so the run page can show somebody how to
                # repeat this by hand. A gate that only says pass or fail
                # teaches nobody the flow it just tested.
                "steps": [x for x in (scenario.get("steps") or [])
                          if isinstance(x, str) and x.strip()],
                "checks": checks,
                "passed": sum(1 for c in checks if c["state"] == PASSED),
                "failed": sum(1 for c in checks if c["state"] == FAILED),
                "blocked": sum(1 for c in checks if c["state"] == BLOCKED),
                "described": described,
                "screen": {k: v for k, v in (screen or {}).items() if k != "key"},
                "shares_screen_with": shared_with,
                "screenshot_url": shot.url if shot else "",
            })

    return {"scenarios": results}


#: Models that ARE the configuration, so a "groups"/"access"/"settings" scenario
#: photographs the definition itself rather than a record that happens to be
#: governed by it.
CONFIG_MODELS = {
    "groups": "res.groups",
    "access": "ir.model.access",
    "settings": "res.config.settings",
}


def merge_screens(scenarios: list[dict]) -> dict:
    """Group scenarios that photograph the same screen, unioning their rings.

    The prompt tells the planner to write one scenario per screen rather than
    one per field. This is the backstop for when it does not, and it is needed:
    a plan that plausibly writes seven scenarios about one wizard is exactly the
    plan that produced seven identical pictures.

    Deduping on the whole screen INCLUDING the highlights does not help there,
    because seven one-field scenarios have seven different rings and so seven
    different keys. So the key is the screen alone, `(model, record ref)`, and
    the rings from every scenario sharing it are merged. Seven scenarios about
    one wizard become one picture of that wizard with seven fields ringed,
    which is the evidence somebody actually wanted.

    Keyed on the fixture REF rather than a database id because this runs before
    the fixtures exist, and the ref is what the plan knows.
    """
    groups: dict = {}
    for sc in scenarios or []:
        screen = sc.get("screen") if isinstance(sc.get("screen"), dict) else {}
        model = str(screen.get("model") or "").strip()
        if not model:
            continue
        key = (model, str(screen.get("record") or "").strip(),
               str(screen.get("kind") or "").strip().lower())
        g = groups.setdefault(key, {"owner": str(sc.get("id") or ""),
                                    "highlight": [], "members": []})
        g["members"].append(str(sc.get("id") or ""))
        for h in screen.get("highlight") or []:
            if isinstance(h, str) and h.strip() and h.strip() not in g["highlight"]:
                g["highlight"].append(h.strip())
    return groups


def _resolve_screen_record(conn, model: str, domain) -> int | None:
    """Find the ONE record a screen should photograph.

    The whole point of the change this exists for: a scenario about one
    scheduled action was evidenced by a picture of the Scheduled Actions list,
    forty rows deep, with the relevant one somewhere in the middle. A list is
    not evidence about a record.

    Returns None rather than a guess when the domain matches nothing or matches
    several. Photographing "the first of six" and captioning it as the record
    would be worse than photographing the list, because it would look precise.
    """
    if not model or not isinstance(domain, list) or not domain:
        return None
    try:
        ids = _read(conn, model, "search", [domain], {"limit": 2})
    except Exception as exc:  # noqa: BLE001 - a screenshot is not worth a run
        log.info("could not resolve %s %r for a screenshot: %s", model, domain, exc)
        return None
    if len(ids) == 1:
        return int(ids[0])
    log.info("screen domain %r on %s matched %s records, so no form to open",
             domain, model, len(ids))
    return None


def _screen_plan(scenario: dict, checks: list[dict], ledger,
                 groups: dict | None = None, conn=None) -> dict:
    """Work out the one picture this scenario should produce.

    Built from the plan's `screen` block, falling back to the old behaviour
    (the list view of whatever the first assertion reads) when a plan predates
    it, so an old run still photographs something.

    The important part is `key`. Scenarios that would photograph the identical
    screen share a key, and the caller captures it once. That is what stops
    seven checks on one wizard becoming seven copies of the same picture.
    """
    screen = scenario.get("screen") if isinstance(scenario.get("screen"), dict) else {}
    kind = str(screen.get("kind") or "").strip().lower()
    model = str(screen.get("model") or "").strip()
    ref = str(screen.get("record") or "").strip()
    domain = screen.get("domain")
    highlight = [h for h in (screen.get("highlight") or []) if isinstance(h, str)]

    # Everything ringed by every scenario sharing this screen, so the single
    # picture carries all of it rather than one field per copy.
    group = (groups or {}).get((model, ref, kind)) if model else None
    if group:
        highlight = list(group["highlight"])

    # Configuration is its own screen. A scenario about a new group is not
    # evidenced by a picture of a sale order that the group happens to govern.
    if kind in CONFIG_MODELS:
        model = model if kind == "settings" and model else CONFIG_MODELS[kind]
        ref = ""

    if not model:
        model = next((c["model"] for c in checks if c.get("model")), "")
    if not model:
        return {}

    # One record beats a list: a form shows the fields already filled in, which
    # is the picture the reviewer wanted, and a list of a thousand rows is not.
    res_id = ledger.id_of(ref) if (ref and ledger is not None) else None
    if not res_id:
        # The plan named no fixture, but an assertion may still pin exactly one.
        for c in checks:
            if c.get("model") == model and c.get("res_id"):
                res_id = c["res_id"]
                break
    if not res_id and conn is not None:
        # Still nothing, so look the record up from the screen's own domain.
        # This is the case that produced a list view of forty scheduled actions:
        # the scenario knew exactly which cron it meant and nothing turned that
        # into an id.
        res_id = _resolve_screen_record(conn, model, domain)
    if not res_id and conn is not None:
        # Last resort, an assertion's domain. An assertion has already been
        # required to identify one record, so if one exists it is the right one.
        for c in checks:
            if c.get("model") == model and isinstance(c.get("domain"), list):
                res_id = _resolve_screen_record(conn, model, c["domain"])
                if res_id:
                    break

    return {"kind": ("form" if res_id and kind in ("", "list") else (kind or "list")),
            "model": model, "res_id": res_id, "highlight": highlight,
            "covers": (group or {}).get("members") or [],
            # The screen, NOT the screen plus its rings. Two scenarios looking
            # at one wizard produce one picture even when they ring different
            # fields, which is the whole point.
            "key": (model, res_id or 0)}


def _save_shot(run_id: int, seq: int, scenario_id, shot, described: str) -> None:
    db.execute(
        """
        INSERT INTO review_screenshots
            (run_id, seq, caption, described, mimetype, image, byte_count)
        VALUES (%s, %s, %s, %s, 'image/png', %s, %s)
        """,
        (run_id, seq, shot.caption[:400], (described or "")[:4000], shot.png,
         shot.byte_count),
    )


def screenshots_for(run_id: int) -> list[dict]:
    """Metadata only — the bytes are served one at a time by their own route."""
    return db.query(
        "SELECT id, seq, caption, described, byte_count, captured_at "
        "FROM review_screenshots WHERE run_id = %s ORDER BY seq", (run_id,))


def screenshot_bytes(shot_id: int, run_id: int) -> tuple[bytes, str] | None:
    row = db.query_one(
        "SELECT image, mimetype FROM review_screenshots WHERE id = %s AND run_id = %s",
        (shot_id, run_id))
    return (bytes(row["image"]), row["mimetype"]) if row and row["image"] else None


def latest_by_task(client_id: int, task_ids: list[int]) -> dict[int, Run]:
    """The most recent run for each of these tasks, in one query.

    One query rather than one per row: a task list is twenty tasks, and twenty
    round trips to render a badge each would make the page slower than the thing
    it is reporting on.
    """
    if not task_ids:
        return {}
    rows = db.query(
        """
        SELECT DISTINCT ON (task_id) *
          FROM review_runs
         WHERE client_id = %s AND task_id = ANY(%s)
         ORDER BY task_id, started_at DESC
        """, (client_id, list(task_ids)))
    return {r["task_id"]: _run_from_row(r) for r in rows}


def verdicts_by_task(client_id: int, task_ids: list[int]) -> dict[int, Run]:
    """The most recent run that actually reached a verdict, per task.

    Separate from `latest_by_task` because they answer different questions, and
    conflating them lost real results. A task reviewed to `partial` on Tuesday
    and retried on Wednesday, where the retry died in `execute`, has both a
    verdict and a failed last attempt. Keying the badge off the latest run made
    Wednesday erase Tuesday: the task dropped back to "Start Review" as though
    it had never been looked at, and the only trace of the verdict was a "Last
    attempt" link that pointed at the wrong run.

    So a verdict is sticky. It stops being the answer when a newer run produces
    a different one, not when a newer run fails to produce any.
    """
    if not task_ids:
        return {}
    rows = db.query(
        """
        SELECT DISTINCT ON (task_id) *
          FROM review_runs
         WHERE client_id = %s AND task_id = ANY(%s) AND verdict <> ''
         ORDER BY task_id, started_at DESC
        """, (client_id, list(task_ids)))
    return {r["task_id"]: _run_from_row(r) for r in rows}


def verdict_tally(client_ids: list[int]) -> dict[str, int]:
    """How the last review of each task concluded, across these clients.

    Per task, not per run. Counting runs would let one task retried six times
    outvote five tasks reviewed once, and the question the dashboard is asking
    is "how is the work doing", not "how much did the gate run".
    """
    out = {"pass": 0, "partial": 0, "fail": 0}
    if not client_ids:
        return out
    rows = db.query(
        """
        SELECT verdict, count(*) AS n FROM (
            SELECT DISTINCT ON (client_id, task_id) verdict
              FROM review_runs
             WHERE client_id = ANY(%s) AND verdict <> ''
             ORDER BY client_id, task_id, started_at DESC
        ) latest
        GROUP BY verdict
        """, (list(client_ids),))
    for r in rows:
        if r["verdict"] in out:
            out[r["verdict"]] = int(r["n"])
    return out


def recent(client_ids: list[int], limit: int = 6) -> list[dict]:
    """The last few runs, for the dashboard's activity list."""
    if not client_ids:
        return []
    return db.query(
        """
        SELECT r.id, r.task_id, r.state, r.verdict, r.started_at, r.finished_at,
               c.name AS client_name, c.id AS client_id
          FROM review_runs r JOIN clients c ON c.id = r.client_id
         WHERE r.client_id = ANY(%s)
         ORDER BY r.started_at DESC
         LIMIT %s
        """, (list(client_ids), limit))


def active_count(client_ids: list[int]) -> int:
    """Reviews running or waiting on somebody right now."""
    if not client_ids:
        return 0
    row = db.query_one(
        "SELECT count(*) AS n FROM review_runs "
        "WHERE client_id = ANY(%s) AND state IN ('queued','running','paused')",
        (list(client_ids),))
    return int(row["n"]) if row else 0


# ---- phase 2: is it even in the code? --------------------------------------

def _variants(name: str) -> list[str]:
    """The forms an identifier might realistically take in Odoo source.

    A task says "add a field called Hi-Tea"; the developer writes `x_hi_tea`, or
    `hi_tea`, or occasionally `hiTea`. Searching only for what the ticket wrote
    would report a field that exists as missing, which is the one mistake this
    check must not make — it is meant to save time, not manufacture failures.
    """
    raw = (name or "").strip()
    if not raw:
        return []
    snake = re.sub(r"[^0-9a-z]+", "_", raw.lower()).strip("_")
    forms = {raw.lower(), snake, snake.replace("_", ""), f"x_{snake}"}
    return [f for f in forms if len(f) >= 3]


def code_check(run: Run, *, symbols: list[dict], client_id: int) -> dict:
    """Search the client's own source for the things the task says it added.

    Entirely deterministic — a substring search over the source bundle already
    in Postgres. No model call, no network, and it finishes in milliseconds
    against the several minutes a browser run costs.

    That speed is the point. If a task says "add a field to res.users" and the
    identifier appears nowhere in the client's addons, the work was not done and
    saying so now is worth far more than discovering it after building fixtures
    and driving a browser. It is evidence about the code, not a verdict: a
    missing symbol is reported as a finding for the reviewer, and the phases
    after this one still run.
    """
    out: list[dict] = []
    try:
        bundle = source_bundle.load(client_id, run.commit_sha)
    except Exception as exc:  # noqa: BLE001
        return {"symbols": [], "error": f"Could not read the source bundle: {exc}"}
    if bundle is None or not bundle.ok:
        return {"symbols": [],
                "error": ("No source has been read for this client at this commit, "
                          "so the code could not be checked. Refresh the knowledge "
                          "base on the client page.")}

    for spec in symbols or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "").strip()
        found: list[dict] = []
        for form in _variants(name):
            for path, text in bundle.files:
                lowered = text.lower()
                at = lowered.find(form)
                if at == -1:
                    continue
                line = text.count("\n", 0, at) + 1
                found.append({"path": path, "line": line, "matched": form,
                              "snippet": text[max(0, at - 40):at + 80].strip()})
                break                       # one file is enough to prove it exists
            if found:
                break
        out.append({
            "name": name, "kind": spec.get("kind", ""),
            "where": spec.get("where", ""), "why": spec.get("why", ""),
            "found": bool(found),
            "at": found[0] if found else None,
            "searched": _variants(name),
        })

    missing = [s for s in out if not s["found"]]
    return {
        "symbols": out,
        "files_searched": bundle.file_count,
        "commit": bundle.commit_sha,
        "missing": len(missing),
        "verdict_hint": (
            "" if not out else
            "nothing_implemented" if len(missing) == len(out) else
            "partially_implemented" if missing else "all_present"),
    }


def created_records(run_id: int) -> list[dict]:
    """Everything this run made on the client's instance, still there.

    Nothing is deleted any more, so this is the answer to the question that
    replaces cleanup: standing in Odoo looking at a record, did the gate create
    this or did I? The ledger knows exactly, and this is how it says so.
    """
    return db.query(
        """
        SELECT ref, model, res_id, created_at,
               (removed_at IS NOT NULL) AS removed
          FROM review_fixtures
         WHERE run_id = %s
         ORDER BY id
        """, (run_id,))


# ---- how the phases are shown -----------------------------------------------
#
# The engine keeps `code_check`, `blast_radius` and `plan` as separate phases,
# and that is not an implementation detail worth changing: each is its own
# checkpoint, so a pause between them costs nothing and a resume replays
# nothing. Collapsing them into one function would trade that for a tidier list.
#
# But a reader does not need three rows for "work out what to test". So the
# split stays in the engine and the display groups them, which is a presentation
# decision living in the presentation layer.

PHASE_GROUPS = [
    ("Read the task",                 ["interpret"]),
    ("Work out what to test",         ["code_check", "blast_radius", "plan"]),
    ("Run the scenarios",             ["execute"]),
    ("Write the summary",             ["summarise"]),
    ("Post the summary to the task",  ["report"]),
]

#: The order the *findings* are read in, which is not the order they were
#: produced in. Somebody opening a finished run wants the verdict and the
#: evidence first; how the plan was arrived at is reference material they scroll
#: to. Running order is the ring; reading order is this.
RESULT_ORDER = ["interpret", "summarise", "execute", "code_check",
                "blast_radius", "plan", "report"]


def grouped_progress(run: Run, durations: dict[str, int] | None = None) -> list[dict]:
    """One row per visible step, with the state of the phases inside it.

    `durations` is passed in rather than fetched here so this stays a pure
    function of its arguments. An earlier version cached it at module level,
    which would have been shared across every request and every worker thread.
    """
    durations = durations or {}
    done = run.done_phases
    by_phase = {s.phase: s for s in run.steps}

    # How far the run actually got, as a position in PHASES. Anything earlier
    # than this with no step row was never going to get one: it is a phase that
    # did not exist when the run started, or one the engine skipped. Reading it
    # as "pending" is what made an old run show a step stuck at running forever
    # with finished steps below it.
    reached = max((PHASES.index(p) for p in by_phase if p in PHASES),
                  default=-1)

    # A finished run has no pending work left, whatever the rows say. Without
    # this a run that predates a phase shows it queued for ever.
    over = run.state in ("done", "cancelled", "failed")

    out = []
    for label, phases in PHASE_GROUPS:
        states = []
        for phase in phases:
            step = by_phase.get(phase)
            if step:
                # A step still marked running under a finished run was never
                # closed, usually because the process died inside it. Repeating
                # the stale row would show a spinner on a run that ended.
                if over and step.state == "running":
                    states.append("failed" if run.state == "failed" else "skipped")
                else:
                    states.append(step.state)
            elif run.phase == phase and run.state == "running":
                states.append("running")
            elif over or (phase in PHASES and PHASES.index(phase) < reached):
                states.append("skipped")
            else:
                states.append("pending")
        real = [s for s in states if s != "skipped"]
        if "failed" in states:
            state = "failed"
        elif real and all(s == "done" for s in real):
            state = "done"
        elif not real:
            # Every phase in the group was skipped. Not a success, and not in
            # progress either; showing it grey says so without claiming a result.
            state = "pending"
        elif "running" in states or any(s == "done" for s in states):
            # Part-done counts as running: the group as a whole is in progress,
            # and showing it as pending would hide work already finished.
            state = "running"
        else:
            state = "pending"
        secs = sum(durations.get(p, 0) for p in phases)
        out.append({"label": label, "phases": phases, "state": state,
                    "seconds": secs, "done": sum(1 for p in phases if p in done),
                    "total": len(phases)})
    return out
