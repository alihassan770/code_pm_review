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
PHASES = ["interpret", "code_check", "blast_radius", "plan", "execute", "cleanup",
          "verdict", "summarise", "report"]

PHASE_TITLES = {
    "interpret": "Read the task",
    "code_check": "Look for it in the code",
    "blast_radius": "Work out what else is affected",
    "plan": "Decide what to test",
    "execute": "Run the scenarios",
    "cleanup": "Remove the records it created",
    "verdict": "Compute the verdict",
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
    if secret_key:
        try:
            from . import clients as clients_mod
            from . import instance as instance_mod
            run = get(run_id)
            if run and live_fixture_count(run_id):
                _cleanup(run_id, instance_mod.connect(
                    clients_mod.get(run.client_id), secret_key))
        except Exception as exc:  # noqa: BLE001 - cancelling must always work
            log.warning("run %s cancelled but cleanup failed: %s", run_id, exc)
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

    answer = client.complete(INTERPRET_SYSTEM, context, model=ai.MODEL_REASONING,
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

    answer = client.complete(BLAST_SYSTEM, context, model=ai.MODEL_REASONING,
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
     "fixtures": [
       {"ref": "bank", "model": "account.journal",
        "find": [["type", "=", "bank"]]},
       {"ref": "inv1", "model": "account.move",
        "values": {"move_type": "out_invoice", "journal_id": "$bank"}}
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
- No prose outside the JSON object."""


def plan(run: Run, *, requirements: dict, impacted: dict, secret_key: str) -> dict:
    client = ai.client(secret_key)
    context = (f"REQUIREMENTS:\n{json.dumps(requirements, indent=2)[:8000]}\n\n"
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
    answer = client.complete(PLAN_SYSTEM, context, model=ai.MODEL_FAST,
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
        return "partial", f"{passed} of {passed + failed} checks held; {failed} did not.{tail}"
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

Six sentences at most. No headings, no bullet lists, no preamble. Lead with what \
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

def note_body(summary: str) -> str:
    """The chatter note: a heading, then the summary, and nothing else.

    HTML because Odoo's chatter body is HTML, but deliberately the least of it —
    a bold heading and paragraphs. No table of phases, no verdict badge, no link
    back to this app, and no attachments. Everything beyond the text is a
    decision about how the record should read, and the record belongs to the
    people working the task, not to the tool watching it.

    The summary is escaped before any markup is added. It is model-written prose
    about a task somebody else typed, so treating it as HTML would let a stray
    angle bracket in a task title reshape the note.
    """
    from html import escape

    text = (summary or "").strip()
    if not text:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
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
    body = note_body(run.summary)
    if not body:
        return {"posted": False, "skipped": "there is no summary to post"}
    if not run.task_id:
        return {"posted": False, "skipped": "this run is not attached to a task"}

    try:
        identity = projects.connect(cfg)
        message_id = identity.post_note(run.task_id, body)
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
                           secret_key=secret_key)
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
                        # §7: a pause frees the database. Records left behind
                        # would be exactly the mess it promises not to leave.
                        if ledger:
                            _cleanup(run_id, conn)
                        raise
                    if not writable:
                        out["not_writable"] = why
                    _save_step(run_id, phase, state="done", detail=out)

            elif phase == "cleanup":
                from . import clients as clients_mod
                from . import instance as instance_mod
                summary_row = {"removed": 0, "refused": []}
                try:
                    conn = instance_mod.connect(
                        clients_mod.get(run.client_id), secret_key)
                    summary_row = _cleanup(run_id, conn)
                except Exception as exc:  # noqa: BLE001
                    # An unreachable instance must not strand the run in a state
                    # with no verdict — but it must also not look like a clean
                    # sweep, so the reason is recorded on the step.
                    _save_step(run_id, phase, state="failed",
                               note=f"Could not remove created records: {exc}")
                    log.warning("run %s cleanup failed: %s", run_id, exc)
                else:
                    note = ""
                    if summary_row["refused"]:
                        note = (f"{len(summary_row['refused'])} record(s) could not "
                                "be removed and are still on the instance.")
                    _save_step(run_id, phase, state="done", detail=summary_row,
                               note=note)

            elif phase == "verdict":
                value, note = compute_verdict(run)
                _save_step(run_id, phase, state="done",
                           detail={"verdict": value, "note": note})
                _set_state(run_id, "running", verdict=value)

            elif phase == "summarise":
                value, note = compute_verdict(run)
                text = summarise(run, verdict=value, note=note, secret_key=secret_key)
                _save_step(run_id, phase, state="done", detail={"summary": text})
                # Still `running`: the summary exists but has not been posted,
                # and marking the run done here would stamp finished_at before
                # the last phase had run.
                _set_state(run_id, "running", summary=text[:4000])

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
    """
    if method not in READ_ONLY_METHODS:
        raise WriteAttempted(
            f"Execution may only read, but {model}.{method} was attempted. "
            "Creating fixtures is a separate step that belongs behind the "
            "pre-flight audit.")
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
    if field not in rows[0]:
        out["note"] = f"{model} has no field {field!r} on this instance."
        return out

    out["actual"] = rows[0][field]
    out["state"] = PASSED if _coerce(expect, out["actual"]) else FAILED
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
            fixtures_mod.create(conn, ledger, ref=ref,
                                model=str(spec.get("model") or ""),
                                values=_expand_refs(spec.get("values") or {}, ledger))
            made += 1
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

Two or three sentences. Say what view is on screen, what the notable values are, \
and whether anything looks wrong (an error dialog, an empty list where records \
were expected, a field missing). Do not speculate about code. Do not say whether \
a test passed — you are describing evidence, not judging it."""


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
            models = [c["model"] for c in checks if c["model"]]
            try:
                # Odoo has no /odoo/<model> route — guessing one produced a
                # "Missing Action" dialog in every screenshot of the first run.
                # The real path is through an action the instance actually has,
                # so ask it for one.
                action = _action_for(conn, models[0]) if models else 0
                page.goto(f"/odoo/action-{action}" if action else "/odoo")
                shot = page.shot(f"{scenario.get('id')} — {scenario.get('title')}")
            except browser.BrowserError as exc:
                log.info("no screenshot for %s: %s", scenario.get("id"), exc)
            except Exception as exc:  # noqa: BLE001
                log.info("screenshot failed for %s: %s", scenario.get("id"), exc)

            described = ""
            if shot and ai_client:
                import base64
                try:
                    described = ai_client.vision(
                        SHOT_SYSTEM,
                        "data:image/png;base64," + base64.b64encode(shot.png).decode(),
                        f"Scenario: {scenario.get('title')}. Describe this screen.",
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
                "checks": checks,
                "passed": sum(1 for c in checks if c["state"] == PASSED),
                "failed": sum(1 for c in checks if c["state"] == FAILED),
                "blocked": sum(1 for c in checks if c["state"] == BLOCKED),
                "described": described,
                "screenshot_url": shot.url if shot else "",
            })

    return {"scenarios": results}


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
