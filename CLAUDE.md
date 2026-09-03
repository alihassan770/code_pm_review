# Odoo PM Agent — project context

> Read this first, every session. It is the handover document.
> Last updated: 2026-09-01, after the phase-D groundwork (projects, repos,
> personas, branch guard, task views).

---

## ⚠️ Read before touching any file

**This repository is the only source of truth.**
`/home/ali-hassan/Programming/Odoo/code_PM_review/` (the parent directory) contains an
**older, partial copy** of the same app — phase A only. It exists because the project was
scaffolded there before this repo was created.

**Never `cp` between the two.** That has already destroyed work once: a wholesale copy from
the parent overwrote this repo's `base.html` and `client_detail.html` and removed every
navigation link to the phase B pages, which were then unreachable despite working. If a
change needs to exist in both places, make it twice by hand, or delete the parent copy.

The dev server now runs **from this repo** (`.venv/` here, `qa-gate serve`). The parent copy
is inert and carries only a signpost file pointing here. Deleting it is safe and would remove
the drift risk entirely — the only reason it survives is that nobody has said to.

---

## What this is

A gate between "the developer says it is done" and "a human looks at it". It takes an Odoo
`project.task` plus a git diff, runs against the client's **own managed staging instance**
on their real data and real installed modules, and returns a verdict with an evidence
bundle.

Not a test runner. A test runner answers "did my tests pass". The question the business has
is "is this safe to show the client".

Internal tooling for **HST**. Odoo is the identity provider; there is no signup.

## The plan is authoritative

`plan_files/odoo-qa-gate-build-plan-rev3.html` (+ `.pdf`) — **revision 3, 23 sections,
61 pages.** Read it before proposing any architecture change. Section numbers referenced
throughout this file (§3, §7, §9…) point at it.

### Why revision 3 exists

- **Rev 1** tested on disposable database clones in stacks we controlled.
- **Rev 2** moved to in-place testing on the client's real staging instance, keeping a
  database-clone escalation ("tier 4") for high-risk diffs.
- **Rev 3 (current)** removes the clone entirely. Clients run on **Odoo.sh, Cloudpepper and
  similar managed hosts that will not let you clone, duplicate, or create a database.**
  There is one staging database, provided by the host. Rev 3 also adds pause/resume,
  because that one database is shared with humans.

### What rev 3 changed

1. **All Docker removed.** No containers anywhere, including the dev fixture.
2. **Tier 4 / the `db` capability / the clone path deleted**, and roadmap phase 6 with it.
3. **Three use cases lost automation**, marked as such rather than quietly dropped:
   UC-07 module **uninstall** cannot be tested; UC-08 upgrade **dry run** became upgrade
   **verification** (after the host upgrades, not before); UC-09 migration scripts are
   **never executed**, only statically analysed plus a row-count probe.
4. **Temporal baseline risk raised `high` → `critical`**, its mitigation being gone. For
   high-risk diffs: run the suite against current staging code, deploy head, run again —
   same database minutes apart, lock held across both.
5. **New §7 "Pausing and resuming a run"**, promoted to a load-bearing decision.

### Mechanisms to understand before coding

- **§5 The probe.** Scenarios travel as *data* in one JSON-RPC call and execute in-process
  inside an Odoo savepoint that is unconditionally rolled back. `flush_all()` before,
  `invalidate_all()` after — both easy to omit, both cause real bugs.
- **§3 The instance contract.** Opt-in sentinel, DB-name allowlist, an 8-check pre-flight
  audit that refuses the run, re-run every time and never cached. Plus in-transaction
  guards patching mail, `requests`, `commit` and DDL. **Implemented — see phase B.**
- **§6 Three tiers.** 1 read-only, 2 write/assert/rollback (most value), 3 committed browser
  flows with a creation registry and reverse cleanup. **There is no tier 4.**
- **§7 Pause/resume.** `release` (lock dropped, branch stays) vs `yield` (lock dropped,
  pre-run branch restored). Tiers 1/2 checkpoint nearly free — the rollback ending each
  scenario *is* the boundary. Resume passes four gates: re-audit, deployed-commit check,
  fingerprint comparison, lock re-acquisition. Fingerprint drift causes **partial**
  invalidation, never a full restart, or people stop pausing.
- **§8 Baseline.** Temporal (nightly), with a fingerprint so a stale comparison declares
  itself rather than lying.
- **§9 Knowledge, four layers.** 1 instance census + 2 source map are **re-derived every run
  and never stored**; 3 curated overlay lives in the client's git repo at
  `qa/knowledge.yml`; 4 regression memory is append-only, written by the gate.
- **§11 The test plan**, posted to the task *before* execution, every step carrying a
  `from:` provenance tag. The doc argues this is the highest-value output in the tool.
- **AI is never in the decision path.** Verdicts are computed deterministically. The AI
  writes prose about a decision already made and proposes scenarios a human ratifies.

---

## Where the build has got to

| | Phase | State | Tests |
|---|---|---|---|
| **A** | App shell, login, clients | ✅ done | 42 |
| **B** | Instance census + hygiene audit (UC-16) | ✅ done, **verified** | 52 |
| **C** | Knowledge base from GitHub | ✅ done | 65 |
| **D** | Review engine: interpret → blast radius → plan → execute | 🟡 built, execute is read-only | — |
| **E** | Probe module + instance contract (doc phase 1) | ⬜ | — |
| **F** | Tiers 1–2, baseline, verdict, pause/resume (doc phase 2) | 🟡 pause/resume + verdict done | — |
| **G** | Tier 3, evidence, cleanup (doc phase 3) | 🟡 screenshots done, no cleanup yet | — |
| **H** | AI scenario proposal + task interpretation (doc phase 4) | ✅ done | — |
| **I** | Blocking, 17/19 profiles, dashboard merge (doc phase 5) | ⬜ | — |

### The review engine (`review.py`, `browser.py`)

Six phases, one `review_steps` row each, resumed by walking to the first that is
not `done`:

    interpret → code_check → blast_radius → plan → execute → summarise → report

There is no `cleanup` phase and no `verdict` phase. Nothing created on a staging
instance is deleted, so the user can tell gate-created records from their own,
and the verdict is arithmetic computed inside `summarise` rather than a step.

* **The summary is written back to the Odoo task** as an internal **log note**
  headed `PM REVIEW SUMMARY`, followed by the summary text and nothing else.
  The user asked for this on 2026-09-03, reversing the earlier "no write-back"
  decision. The shape is what answers the original objection:
  * `mail.mt_note`, never `mail.mt_comment` — a note reaches the Log note tab
    and nobody is notified or emailed. A tool that mails a client on every run
    gets switched off in a week.
  * **No attachments**, and no argument exposes them. No links, no verdict
    badge, no phase table — everything beyond the text is a decision about how
    the record should read, and the record belongs to the people working the
    task.
  * Nothing else on the task is touched: no stage change, no field write.
  * `review_runs.reported_at` guards against posting twice; a failure leaves it
    NULL, the run still `done`, and the run page offers "Post it again". The
    usual cause is the service account lacking write access on `project.task`,
    and the error says so.
  * `projects.post_note` falls back to `message_type='notification'` when a
    version rejects `subtype_xmlid`; both land as notes. `tests/test_report.py`
    (26 assertions) exercises both paths and asserts on what reached the fake
    Odoo, not on what the code says it sent.
* **Writing is gated on `database.is_neutralized`.** `fixtures.assert_writable`
  refuses unless Odoo itself reports the database as neutralized — the flag Odoo
  sets on staging/duplicate databases that disables outgoing mail and external
  calls. Deliberately not a URL or name check: "staging" in a hostname is a
  habit somebody gets wrong once; `is_neutralized` is the database's own
  statement. Verified `true` on the Custom Tours instance.
* **Every created record is ledgered before use** (`review_fixtures`), in
  Postgres not memory, so a crash between create and cleanup still leaves a row
  pointing at the orphan. `qa-gate leftovers` lists them; nothing deletes from a
  client instance unattended.
* **`FORBIDDEN_MODELS`** — res.users, res.groups, res.company, res.currency,
  ir.cron, ir.config_parameter and friends can never be created by a review.
* **Cleanup runs on pause and cancel too**, not just on completion — §7 says a
  pause frees the database, and records left behind would be exactly the mess it
  promises not to leave. Odoo refusing to unlink a posted entry is recorded
  against the ledger row and surfaced, never swallowed.
* **`execute` is read-only for assertions and enforced.** `_read()` checks every call against
  `READ_ONLY_METHODS`; `create`/`write`/`unlink` raise `WriteAttempted`. An
  assertion needing a record that does not exist returns **`blocked`**, which is
  counted as neither pass nor fail. Creating fixtures is a separate act that
  belongs behind the §3 audit.
* **Phase model choice is measured.** `plan` runs on `deepseek-v4-flash`
  (143–165s, ~144 tok/s) instead of `deepseek-v4-pro` (213–265s, ~60 tok/s);
  quality checked, not assumed — full requirement coverage, regressions present,
  zero forbidden models. Everything else stays on pro. **Do not "optimise" by
  lowering `reasoning_effort`:** measured, `medium` was *slower* than `high` on
  pro (301s vs 265s). Time tracks output volume, not reasoning depth.
* **The run page badge shows the verdict, not the run state.** `done` means the
  pipeline finished, which on a run where every check failed produced a green
  DONE badge beside a red `fail` panel: two true statements reading as a
  contradiction, and the green one is larger and higher up, so it won. A
  finished run with no verdict reads "no verdict" in grey rather than an emerald
  "done". The progress ring is drawn in the verdict's colour for the same
  reason: it measures how much of the pipeline ran, which is worth measuring,
  but a large emerald ring next to a red verdict is the loudest thing on the
  page telling the wrong story.
* **`partial` has to mean mostly working.** It was returned for any mix of
  passes and failures, so 1 check holding out of 11 was reported as "partial",
  which reads as a near miss when it is the opposite. A mixed result is partial
  only when the passes OUTNUMBER the failures; ties fail, because half the
  checks failing is not a qualified success and a gate in doubt should say the
  more cautious thing.
* **Anything a fixture creates is a DRAFT, and that caused a false failure.**
  The plan built an `account.payment` and asserted it was live. `create` leaves
  it in draft with `move_id = False`, so it has no journal entry and can never
  reconcile: ten checks failed on a customisation that worked. Fixtures now take
  `"then": ["action_post"]`, run through `fixtures.run_action` against the
  `ALLOWED_ACTIONS` allowlist (post/confirm/validate/done only, never unlink,
  write or cancel) and under the same `assert_writable` and `FORBIDDEN_MODELS`
  rules as `create`.
* **An impossible expectation is `blocked`, not `failed`.** On Odoo 18
  `account.payment.state` has no `posted` value at all, it is draft, in_process,
  paid, canceled, rejected. A plan written against 17 asserted `posted`, which
  could never hold, and the gate reported it as the developer's bug.
  `_selection_values` asks the instance what a selection field allows, and an
  expectation outside that set is recorded as not-tested with the real values
  named. Blocked already means "this was not actually checked", which is the
  truth, and it keeps the plan's mistake out of the verdict.
* **The plan prompt is given the client's Odoo version**, read from the client
  row by `_client_version` and NOT from `conn`: the connection is not opened
  until `execute`, several phases later, so reaching for it there raises
  NameError. Version-shaped mistakes need the version in the prompt.
* **The verdict is arithmetic.** `compute_verdict` counts assertion outcomes and
  contains no model call. `summarise` writes prose *about* it and is told not to
  contradict it.
* **Screenshots** come from Playwright with the persona's `session_id` cookie
  injected - never by driving the login form, whose markup differs across 17/18/19.
* **The engine has seven phases; the run page shows five steps.**
  `review.PHASE_GROUPS` folds `code_check` + `blast_radius` + `plan` into one
  visible "Work out what to test". The split stays in the engine because each
  phase is a checkpoint, so a pause between them replays nothing; collapsing
  them into one function would trade that for a tidier list. Grouping is a
  presentation decision and lives in the presentation layer.
  A phase with no `review_steps` row reads as **skipped**, not pending, when the
  run got past it or ended. Without that, runs predating a phase showed a step
  spinning for ever above finished ones.
* **Running order is the ring; reading order is `review.RESULT_ORDER`.** They
  are different lists on purpose: somebody opening a finished run wants the
  task, the summary, the records and the evidence first, and how the plan was
  arrived at below. `_run_findings.html` is included from inside the phase loop
  so it can sit between two cards rather than after all of them.
* **Em dashes are stripped at render, not only banned in prompts.**
  `templates.env.finalize` in `web/app.py` maps them to hyphens across every
  template. The prompts still forbid them, but rows written before the ban hold
  them and a model told not to emit a character sometimes does. The only place
  that sees all the text is the render boundary.
* **The task link is `/mail/view`, never `/web#id=...`.** Half our staff are
  portal users, and `web_client` (`web/controllers/home.py`) ends with
  `if not is_user_internal(...): return request.redirect('/web/login_successful')`
  - a backend link strands them on a page with no way to the task. `/mail/view`
  branches through `_get_access_action` on `user.share`: internal users get the
  backend form, portal users get `/my/tasks/<id>`. It also survives the login
  round-trip, verified live: `/web#...` redirects to
  `login?redirect=%2Fweb%3F` with the task id gone (a browser never sends the
  fragment), while `/mail/view` keeps `res_id` intact. No `access_token` is
  attached, so access stays whatever Odoo already grants the person signed in.
* **One screen, one screenshot.** A plan once wrote seven scenarios about one
  wizard, one per field, and the run produced seven identical pictures of it.
  Three things now prevent that, and the order matters:
  1. `PLAN_SYSTEM` says a scenario is one screen and one behaviour, NEVER one
     per field. Seven new fields are one scenario with seven assertions.
  2. Each scenario carries a `screen` block: `kind` (form/list/wizard/settings/
     groups/access), `model`, `record` (a fixture ref), and `highlight` (Odoo
     field names, which get a red ring drawn round them by
     `browser.Session.highlight`). Verified live against Odoo 19: the ring lands
     on the right widgets and an unknown name is ignored rather than raising.
  3. `merge_screens()` is the backstop, and it is the part that actually holds.
     **Dedupe on the screen alone, `(model, record)`, never on the screen plus
     its highlights** - seven one-field scenarios have seven different rings and
     so seven different keys, which is exactly the case that has to collapse.
     Rings from every scenario sharing a screen are unioned into the one shot.
  A `screen.record` naming a fixture makes the capture open **that record's
  form** rather than a list view, which is what shows the fields filled in.
  Configuration is photographed as itself: `groups` shoots `res.groups`,
  `access` shoots `ir.model.access`, and the governed record is dropped.
  `tests/test_screens.py` (24 assertions) locks all of this down.
* **`_read` turns `active_test` off, and this is a correctness fix, not a
  convenience.** Odoo defaults it to True, so `search` silently drops archived
  rows. Measured live on Custom Tours: `ir.cron.search([])` returned **1**
  record; with `active_test: False` it returned **41**. Every check about an
  inactive scheduled action was therefore recorded as "no record matches, this
  needs a fixture", which reads like a badly written plan rather than a record
  sitting right there archived. Worse, "the cron is disabled" is exactly the
  kind of thing a review should catch and the gate could not see it. Archived
  state is now a fact to assert on, not a filter. A caller passing its own
  `active_test` is not overridden.
* **A screenshot is of a record, not of a list.** `screen.domain` names one
  existing record when the scenario did not create it, and
  `_resolve_screen_record` opens its form. A domain matching several resolves
  to **none**: a picture of the wrong one of six is worse than a picture of the
  list, because it looks precise. Resolution order is fixture ref, then an
  assertion's `res_id`, then `screen.domain`, then an assertion's `domain`.
* **`highlight()` is scoped by view kind.** The first version ran
  `querySelectorAll` on the document and ringed every match, so on a list view
  `[name="active"]` ringed the cell in all forty rows and the picture pointed at
  nothing. Now: on a form, every matching widget inside `.o_form_view`; on a
  list, the **column header only** (`th[data-name]`), one element; anywhere
  else, the first match. Verified against a live Odoo 19 list and form.
* **Scenario `steps` are rendered on the run page** under "How to do this
  yourself". The gate exists so somebody can repeat the check without it; steps
  that live only in the prompt teach nobody the flow.
* **`summarise` writes the verdict to `review_runs`, and that line is the
  point.** When `verdict` was its own phase, that phase persisted it. Folding
  the arithmetic into `summarise` moved the computation and left the write
  behind, so runs completed all seven phases with an empty verdict. The symptom
  was the two pages disagreeing: the run page showed the state ("Done") and the
  task list showed an older run's verdict, because a sticky verdict is the last
  run that produced one.
* **A finished run with no verdict is not a pass.** It happens when every check
  was blocked, so nothing was verified. The run page says "Nothing could be
  checked" with the reason rather than showing a bare "Done", which reads as
  success.
* **The chatter note is plain text unless the account is internal.**
  `message_post` escapes a plain string (`'body': escape(body)`), and the only
  RPC-reachable way round it, `body_is_html=True`, is gated on
  `self.env.user._is_internal()`. Our service account is a **portal** user, so
  the HTML version posted markup that Odoo escaped and the note read as literal
  `<p><b>PM REVIEW SUMMARY</b>` on the task. `Identity.can_post_html()` asks,
  and `note_body(summary, html=...)` supplies the form the credential can
  actually use. Verified live: the note now renders as
  `<p>PM REVIEW SUMMARY: ...</p>`.
* **A verdict is sticky, and that is why there are two lookups.**
  `latest_by_task` is the last run of any kind; `verdicts_by_task` is the last
  run that reached a verdict. They differ when a retry dies, and conflating them
  lost real results: a task reviewed to `partial` and retried unsuccessfully
  dropped back to "Start Review" as though it had never been looked at. A
  verdict stops being the answer when a newer run produces a different one, not
  when a newer run fails to produce any. The row shows both, verdict plus a
  "last retry failed" badge.
* **The task list is split into Not reviewed / Reviewed / All**, with the
  verdict tally on the reviewed tab. The stage filter still scopes which tasks
  are in view; the tabs say what has happened to them. An empty "not reviewed"
  tab falls through to "reviewed" rather than being a dead end.
* **`message_post` returns a list, not an id.** `odoo/service/model.py:99`
  serialises any returned recordset to `.ids`, so `int()` on it raised
  "int() argument must be ... not 'list'" *after* the note had already posted:
  the run reported a failure that had not happened, and pressing "Post it again"
  would have duplicated the note. `Identity._message_id` normalises it and never
  raises, since the id is only used for logging.
* **The portal service account cannot post with an explicit subtype.**
  Confirmed live: `subtype_xmlid='mail.mt_note'` gives AccessError (create on
  Message), and the `message_type='notification'` fallback succeeds. The
  fallback is load-bearing, not belt-and-braces.
* **Ambiguities are answerable.** The run page offers a box per open question;
  answering stores it, marks it authoritative in the prompt, and `replan()`
  rebuilds from interpretation so the plan reflects the answer.

**302 tests passing** (42 + 52 + 69 + 30 + 58 + 51). The user numbers phases from 1, so their "phase 1" is A, "phase 2"
is B, "phase 3" is C.

**Phases A–D write nothing to any client database.** Deliberate: a working app and real
client value land before the gate ever touches a staging instance. **Phase E is the first
one that writes**, and nothing above it can be trusted without it — resist building
scenarios before the probe.

### Phase A — app shell (done)

`config.py` `crypto.py` `db.py` `paths.py` `users.py` `sessions.py` `clients.py`
`odoo_client.py` · routes `setup` `auth` `dashboard` `clients` · migration `001_initial.sql`
(`users`, `sessions`, `clients`, `user_clients`, `instance_secrets`).

- First-run `/setup`; Odoo-backed login, **no signup**; server-side sessions storing only a
  SHA-256 of the cookie token; CSRF on every mutating form.
- Client registry, and per-client RPC credentials **verified against the live instance
  before being stored**, encrypted at rest, never rendered back.
- `/setup` re-openable to fix a wrong URL/database: allowed **while no user account exists**
  (nothing to take over), **admin-only** after that. `qa-gate set-identity` is the CLI
  escape hatch for a full lockout.

### Phase B — census + hygiene audit (done, verified 2026-09-01)

`census.py` `audit.py` `fingerprint.py` `instance.py` · route `audit` · migration
`002_phase_b.sql` (`instance_audits`, `instance_fingerprints`).

Verified independently, not just by running its own suite:

- **All eight §3 checks present**: sentinel, db-name allowlist, `ir.cron`, mail servers,
  payment providers, `web.base.url`, integration parameters, integration stubs.
- **Verdicts are `pass` / `refuse` / `error`**, and `error` is deliberately distinct from
  `refuse` — "unsafe" and "unknown" are different answers, and collapsing them hides outages.
- **`matches_pattern` treats `_` literally**, not as a SQL `LIKE` wildcard. Confirmed:
  `lmmstaging` does *not* match `%_staging`. Getting this wrong would silently widen every
  client's allowlist, since Odoo database names are full of underscores.
- **The census is not persisted anywhere** — no table, no insert. Correct per §9: a cached
  census lies the moment somebody installs an app through the hosting panel.
- Fingerprint stores a `payload` explaining what the hash covered, so drift can say *which*
  module appeared rather than only that a hash moved.

### Phase C — knowledge base (done, by another session)

`github.py` `repo_sync.py` `knowledge.py` `coverage.py` `scenarios.py` · route `knowledge`
· migration `003_phase_c.sql` (`client_repo_cache`).

### Built after phase C — the groundwork phase D needs

Not a numbered phase; this is the plumbing that had to exist before a review can
be started on a task. All of it is live and covered by the suites.

**`branches.py` — the write guard.** The rule: *read any branch, only ever create
and commit under `staging`.* Refuses `main` `master` `prod` `production` `live`
`release` `develop` `trunk` `stable` `default` in **any casing**, through
`refs/heads/`, `origin/`, `refs/remotes/…`, plus prefix families (`release/2024.1`,
`production/eu`, `hotfix/*`). Allowing is **by prefix, not by exclusion** — a
branch is writable because it is ours, so an unfamiliar name is refused by default
rather than accepted by omission. Tested against 26 dangerous names.

> **A base branch is a read reference, and `main` is correct there.** I once made
> `validate_base_branch` refuse protected names and phase C's tests caught it: the
> base branch is where the code *lives* and what task branches are diffed against.
> The write ban belongs at `assert_writable`, never at configuration.

**`projects.py` — our Odoo, not the client's.** Rule of thumb for this codebase:
`instance.py` / `census.py` / `audit.py` point at a *client's* Odoo; `projects.py`
points at *ours*. Reads `project.project`, `project.task.type`, `project.task`,
`ir.attachment`. Tasks are read live, never mirrored — a cached task list is wrong
the moment a PM drags a card.

**`repos.py` — several repositories per client**, each with its own base branch and
`per_task`/`shared` mode. `clients.github` is kept as a **mirror of the primary
repo** because phase C's `repo_sync` still reads that single column; anything new
should use `repos.for_client()`, and the knowledge base should eventually walk
every repo rather than just the first.

**`personas.py` — browser logins for tier 3.** Real passwords, encrypted. Verified
through `open_session()` (a web login), **not** `authenticate()` — an API key
passes the second and fails the first, and finding that out mid-run as a flow that
cannot log in is exactly what this check prevents.

**`app_secrets.py` — two service credentials**, encrypted, verified before storing:
`identity_rpc` (reads tasks; a nightly run has no signed-in person to borrow from)
and `github_token` (private repos 404 identically to missing ones without it).

**`html_clean.py` — allowlist sanitizer** for task descriptions. Arbitrary HTML
injected into a page holding every client's staging credentials; "our staff wrote
it" is not a security boundary. Unknown tags are dropped, not unwrapped. Images
are **off by default** and return a dropped-count so the UI can say so.

Migrations: `004_projects_repos_personas.sql` (project link, `client_repos`,
`client_personas`, `app_secrets`) and `005_multi_project.sql` (`client_projects`,
several projects per client, `db_name_pattern` default cleared).

#### Decisions worth not re-litigating

- **Several Odoo projects per client**, and the review stage is stored **by name**
  on the client. `project.task.type` ids differ per project, so an id would force a
  stage choice per project and make "everything waiting for review" the awkward
  case instead of the default.
- **`db_name_pattern` is optional and defaults to empty.** It is check 2 of §3 and
  real defence in depth, but the old `%_staging` default is wrong for Odoo Online
  (`company-main-1234567`) and silently failed the audit. Blank makes the audit
  report that the check proves nothing — honest, one control weaker.
- **App admin ≠ Odoo `base.group_system`.** The lead who runs the gate is often not
  an Odoo sysadmin — ours is a *portal* user. Admin is granted by that group **or by
  being the first user to sign in**, and is never removed by a later login.
  `qa-gate grant-admin <login>` promotes anyone after that.
- **The login page offers to adopt the credential** as the service account, ticked
  by default, admin-only, only when none is set. Login is the one moment the app
  legitimately holds a working credential, because it authenticates and discards it.

#### Live Odoo facts (this deployment)

- Identity Odoo `https://hsxtech.odoo.com`, db `hsxtech1-main-17222046`, **19.0+e**.
- The operator account is a **portal** user: `base.group_user` is False. Tasks and
  descriptions read fine; **`ir.attachment` is refused**, so attachments and inline
  images need an Internal User account on the service credential. The code reports
  that rather than rendering "no attachments", which would be a lie.
- Project 116 = `P116 - Custom Tours - Odoo Implementation`, with stages including
  **PM Review** (33) and **AI Code Review** (497).

### Roles and the AI provider (2026-09-03)

**Two roles, `user` and `admin`, and that is the whole model.** An admin sets the
Odoo connection, the service credential and the AI provider; those apply to every
user. A user runs reviews against them and cannot change them. Managed from
Settings, next to the things they govern, rather than on a page of its own.

* **Nobody is ever asked for a server or a database.** The sign in page takes a
  login and a password only, and shows which Odoo it authenticates against. The
  "change it" link is admin-gated by `setup.may_configure`, which is open only
  while no user account exists.
* **Two guards, both about lockout, not about tidiness.** You cannot demote
  yourself, and the last administrator cannot be demoted at all. A system with no
  administrator has nobody who can appoint one and the only way back is
  `qa-gate grant-admin` on the server. `users.admin_count()` ignores inactive
  rows, so a deactivated admin does not hold the door open. Verified over real
  HTTP: the self-demote is refused and a non-admin gets 403 on both the page and
  the POST.
* Odoo's `base.group_system` still re-grants admin on next sign in
  (`is_admin = is_admin OR EXCLUDED.is_admin`), so a demotion here is not
  permanent for a real Odoo sysadmin. The page says so.

**Three AI providers, two wire formats.** `ai.PROVIDERS` is a registry of
`Provider` dataclasses. DeepSeek and OpenAI both speak `/chat/completions` and
share `OpenAICompatible`; Anthropic gets its own class, because four things
differ at once: system prompt is top-level, the reply is typed content blocks,
thinking is a request field, and auth is `x-api-key`. That is a class, not four
branches. `DeepSeek` survives as an alias.

* **Model ids come from the provider, not from module constants.** `review` and
  `addon_digest` call `client.provider.reasoning` / `.fast`, so the measured
  choice of a fast model for `plan` survives switching provider.
* **One stored key per provider** (`ai_key_<name>`), not one shared row, so
  trying another provider and switching back needs no retyping. The selection
  itself lives in the new `app_settings` key/value table, migration 012: it is a
  choice, not a secret, and encrypting it would obscure a value whose job is to
  be read.
* Migration 012 copies an existing `deepseek_key` row to `ai_key_deepseek`, and
  `client()` reads the old row as a fallback, so an upgrade cannot turn a working
  key into "no AI provider configured".
* **Only DeepSeek is verified end to end.** The Anthropic and OpenAI paths are
  written from their documented request shapes and unit-tested against recorded
  payloads; without a key for either, nothing has been proved live. Say so rather
  than implying all three are equal.

### Phase D - next

Python `ast` + `lxml` parsers, the §10 signal table, blast-radius selection, and the three
cheap static checks the plan says to ship in week one: missing `super()`, edited
`noupdate="1"` data, stored compute with no migration.

---

## Running it

```bash
./setup.sh                    # idempotent; --check for diagnostics only
./.venv/bin/qa-gate serve     # http://127.0.0.1:8770
./.venv/bin/qa-gate check     # config, Postgres, identity-Odoo reachability
./.venv/bin/qa-gate migrate   # apply pending migrations
./.venv/bin/qa-gate set-identity --url https://… --db …   # lockout escape hatch
./.venv/bin/qa-gate grant-admin <login>       # app admin, not Odoo's group_system
./.venv/bin/qa-gate audit <slug>…             # hygiene audit from the shell
./.venv/bin/qa-gate knowledge <slug>…         # repo sync from the shell
```

Configure, in this order: **/setup** (which Odoo) → sign in with *"also use this to
read Odoo tasks"* ticked → **Settings → GitHub access** (token) → add a client.

**Restart the server after every code change** — Jinja auto-reloads templates, Python
changes are not picked up. The user tests in the browser immediately and will report a
fixed bug as still broken.

### Tests

Real PostgreSQL, real HTTP against fakes that reproduce the systems' actual rules
(`tests/fake_odoo.py`, `fake_staging.py`, `fake_github.py`).

```bash
createdb -U odoo odoo_qa_gate_test
.venv/bin/python tests/test_phase_a.py     # 42
.venv/bin/python tests/test_phase_b.py     # 52
.venv/bin/python tests/test_phase_c.py     # 65
dropdb -U odoo odoo_qa_gate_test
```

Each suite recreates its own schema, so **drop and recreate the database between suites** or
you will chase phantom failures.

---

## Decisions settled (not all in the plan document)

| Topic | Decision |
|---|---|
| Backend | **Python 3.12 + FastAPI + Uvicorn** |
| Frontend | **Jinja2 + HTMX + Tailwind (CDN)** — no React, no build step |
| Database | **Postgres 16** |
| Auth | **Odoo is the identity provider**; authenticate against `res.users`, no signup |
| Who logs in | **HST staff only.** Clients never log in; UC-14 stays a PDF you hand over |
| Docker | **Not used anywhere** |
| Browser automation | **Playwright**, not Hoot |
| Tier 3 credentials | **Real passwords** for QA persona users, not API keys |
| Product name | **Odoo PM Agent** (UI). Package/CLI are still `qa_gate` / `qa-gate` |

The plan's §19 still specifies Express/TypeScript for the control plane, chosen because
`hst-pmo-dashboard` uses that stack. **We overrode it**: the runner must be Python regardless
(Odoo RPC, `ast`, Playwright, the probe module), so TypeScript would add a second language
without removing a second service. This is the known divergence between doc and code.

## UI conventions

- Violet scale `brand-50…950` around **`#8b5cf6`**; `accent` aliases kept so older markup
  still themes. Inter + JetBrains Mono.
- Shared classes in `base.html`: `.field` `.label` `.btn-primary` `.btn-ghost` `.card`.
- Left sidebar in the **same dark violet as the sign-in panel** — the surface you log in
  through becomes the surface you work in. Collapses to a top bar below `lg`, no JS.
- **Status is icon + label + colour, never colour alone.** Status hues are reserved
  (good `#0ca30c`, warning `#fab219`, serious `#ec835a`, critical `#d03b3b`) and never
  reused for decoration.
- **Motion primitives live in `base.html`**: `.rise` (staggered entrance, index
  passed as `--i`), `.card-lift` (hover), `.grow-x` (proportion bars), `.breathe`
  (a slow pulse for live runs, gentler than Tailwind's `animate-pulse`, which is
  tuned for skeletons and too insistent for a dot that sits there for twenty
  minutes). All of it is off under `prefers-reduced-motion`.
- **Entrance animations use `animation-fill-mode: backwards`, never `both`.**
  With `both` the final keyframe keeps applying for ever, and an
  animation-applied value beats an ordinary rule: `to { transform:none }` won
  against `.card-lift:hover`, so the hover lift silently did nothing while the
  box-shadow half of the same rule worked. Caught by measuring the element's
  box before and after hover, not by looking at it.
- **The dashboard leads with what is waiting, not with what already happened.**
  The recent-runs feed was a log: it told you what had been dealt with. The
  panel is now "Waiting for review", tasks sitting in a client's review stage
  with no verdict on record, with the count repeated as a sentence under the
  page title.
- **That panel is fetched after the page, never with it** (`/dashboard/pending`,
  HTMX `hx-trigger="load"`). Working it out means asking Odoo for the tasks of
  every project of every client; measured, the page is interactive in 0.9s and
  the panel lands at 2.7s. Blocking the dashboard on it would make the whole app
  feel slow to open when only one panel is. A skeleton holds the height so
  nothing jumps.
- **The sentence and the list come from one request.** The panel response also
  carries an `hx-swap-oob` update for the hero line, so the headline count can
  never disagree with the list under it.
- A client whose Odoo read fails is counted into a "could not be read" note
  rather than skipped silently: a short list that claims to be complete is worse
  than one that admits it is not.
- **The dashboard reports work, not setup.** It carries the verdict split, a
  live-run indicator and a recent-reviews feed. A landing page for a review tool
  that only counts how many clients are configured is a page about installation.
  `review.verdict_tally` counts the latest verdict **per task**, not per run, so
  one task retried six times cannot outvote five reviewed once.
- Client avatars take a hue from `id * 137`, the golden angle, so consecutive
  ids come out as different colours rather than neighbouring shades.
- One call to action per screen - the dashboard's "Add client" is hidden when the empty
  state already offers one.

---

## Verified technical facts

Checked against the local Odoo source trees, not recalled. Trust these.

### Playwright, not Hoot

- **Hoot does not exist in Odoo 17** (`web/static/lib/hoot` is 18/19 only; 17 is QUnit).
  We target 17, 18 and 19.
- **Hoot mocks the world** - `lib/hoot/mock/` ships `network.js`, `date.js`, `window.js`,
  `storage.js`, plus a `mock_server`. Rev 3's premise is real data, real modules, real config.
- It ships only in `web.assets_unit_tests_setup`, not `assets_backend`, and produces no
  screenshots. Odoo **tours** stay the documented fallback for widgets Playwright can't
  reach — never primary.

### Odoo authentication — shapes all of tier 3

In `odoo/addons/base/models/res_users.py`, `_check_credentials` puts the API-key branch
behind `if not interactive:`. Interactive means a web login.

| | RPC (probe, census, audit) | Browser session (Playwright) |
|---|---|---|
| API key | works, and is **required** with 2FA | **does not work** |
| Password | works unless 2FA | **required** |

- Each client needs **two credential types**. QA browser users must not have 2FA.
- **Tier 2 personas cost nothing** — the probe runs inside Odoo and switches user with
  `env(user=…)`. That is why §10 can call the full persona matrix cheap to run broadly.
- **Tier 3 personas need real credentials** per instance. Provision dedicated
  `qa_sales_user`, `qa_production_user`… during onboarding, generated passwords stored
  encrypted. **Never reuse an employee's account** — their password rotates and their
  groups drift, and both look like regressions.
- Session cookie is `session_id` in both 17 and 19.
- **Playwright login:** do not drive the form. POST `/web/session/authenticate` with
  `{db, login, password}`, capture the `session_id` cookie, inject it into the browser
  context. Version-independent; the login markup changes across versions, the endpoint
  does not. Keep one form-login smoke check.

### `common.version` ignores the database argument

It succeeds against a database that does not exist. **`OdooClient.check_database()` is the
real check** — it attempts an authentication with an impossible login and reads the failure
mode: credentials rejected means the database opened; a psycopg error means it does not
exist. A rejected login is the success signal. This bug shipped once and produced a raw
Postgres traceback at login instead of a useful message.

### Odoo requires the database name

`exp_authenticate(db, login, password, …)` opens `Registry(db)` — there is no auth call
without it, and one server can host many databases with the same login in each.
`list_dbs()` raises `AccessDenied` when `list_db = False`, which is the default on Odoo
Online, so auto-discovery is not reliable.

---

## Storage model

| What | Where |
|---|---|
| Users, sessions, clients, audits, fingerprints, runs, findings, residue, queue | **Postgres** |
| Regression memory (§9 layer 4) | **Postgres**, append-only |
| Scenarios + curated knowledge (`qa/scenarios/`, `qa/knowledge.yml`) | **Git, the client's own repo** |
| Screenshots, traces, DOM dumps | **Object storage**; local dir under `state/` in dev |
| Instance census + source map | **Nowhere — recomputed every run** |
| Client instance credentials | **Encrypted, never resolvable from a browser session** |

The human-authored knowledge base lives in **git, not the database** — §9 opens by naming
the failure mode: *"The mistake to avoid is treating this as a document somebody
maintains."* Postgres holds a parsed read-model cache; UI edits open a **pull request**.

---

## Deployment (Railway)

Environment always overrides the config file, so one code path serves a laptop and a
container. See `.env.example`. Two variables are not optional:

- **`QA_GATE_SECRET_KEY`** — encrypts every client's Odoo credentials. A container has no
  persistent filesystem, so without it a fresh key is minted per deploy and **every stored
  credential becomes undecryptable**.
- **`QA_GATE_SECURE_COOKIES=true`** — Railway terminates TLS. Leave it false and the session
  cookie is dropped, which looks exactly like login silently failing.

Set `QA_GATE_ODOO_URL`/`QA_GATE_ODOO_DB` too, so `/setup` is skipped; `/setup` refuses to
pretend it saved when the filesystem is read-only. `railway.json` points the healthcheck at
`/healthz`, which touches Postgres so a process that cannot reach its database reports
unhealthy rather than lying.

---

## Environment

- Python 3.12.3 is `python3`. Node 20. **No Docker.** Postgres 16 local.
- **There is no `ali-hassan` Postgres role.** The `odoo` role works over the local socket and
  is a superuser: `postgresql://odoo@/odoo_qa_gate`. `setup.sh` defaults to `$(whoami)`, so
  pass `QA_GATE_DB_USER=odoo` here.
- Odoo source trees in `~/Programming/Odoo/` — `odoo_16`, `Odoo_17`, `Odoo_18`,
  `odoo_19/src/odoo19`. These are the §16 dev fixtures *and* the place to verify Odoo
  behaviour instead of guessing.
- Playwright's Chromium is cached in `~/.cache/ms-playwright`; only the Python package is
  missing.

### Regenerating the plan PDF

```bash
google-chrome --headless=new --disable-gpu --no-sandbox --no-pdf-header-footer \
  --print-to-pdf=plan_files/odoo-qa-gate-build-plan-rev3.pdf \
  --virtual-time-budget=25000 file://$PWD/plan_files/odoo-qa-gate-build-plan-rev3.html
```

**Never run a blanket `replace('@','')` over that HTML.** It destroys `@media print`,
`@page`, `@api.depends` and the `wght@400` in the fonts URL. The symptom is the PDF
ballooning 61 → 98 pages with the sidebar bleeding into the body. Sanity check: the file
holds exactly **24** `@` characters and `pdfinfo` reports ~61 pages.

---

## Prior art worth copying

`~/Downloads/claude/agents/` (`odoo-dev-loop`, the full FastAPI+Jinja+HTMX app) and
`~/Downloads/odoo_dev_runner-0.1.0-py3-none-any.whl/` (the same package shipped with only
the machine-touching half). Directly reusable: `git_ops.py`, `diffs.py`,
`runner/discovery.py`, `paths.py`, `appmode.py`, `runner/security.py`.

Patterns to keep:

- **Three-mode split** (`local` | `runner` | `cloud` via `APP_MODE`) — one implementation,
  topology by env var.
- **The runner is a dumb, stateless, policy-free executor**; policy arrives in the request
  body, *"so the two halves can never drift out of sync over a config file."*
- **The browser is the relay** — cloud never calls the runner directly.
- **Runner security**: custom header token (forces a CORS preflight), `compare_digest`,
  origin allowlist, `allow_credentials=False` **so CSRF cannot apply**. The web UI's session
  cookie is a *separate* surface and carries its own CSRF token — do not merge them.
- **Join key is the GitHub `owner/name` remote**, never a config id.
- Errors say *what was searched*, not just that it failed. Never auto-pull or auto-stash.

## House rules

- Match the prior art: dataclasses internally, Pydantic only at the HTTP boundary,
  `subprocess` for git/gh rather than wrapper libraries.
- Docstrings explain **why**, and name rejected alternatives.
- Do not introduce Docker.
- Do not put AI in the verdict path.
- Do not put the curated knowledge base in the database.
- Never assume shell or Postgres access to a client instance — RPC is the only floor.
- **Verify Odoo behaviour against the local source trees rather than recalling it.** Three
  real bugs in this project were caught that way and one shipped because it wasn't.
