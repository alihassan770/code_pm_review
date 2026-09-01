# Odoo PM Bot — project context

> Read this first, every session. It is the handover document.
> Last updated: 2026-09-01, after verifying phases A–C.

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

The running dev server currently starts from the **parent** copy, so it does *not* show the
phase B/C pages. To run this one, create a venv here.

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
| **A** | App shell, login, clients | ✅ done | 41 |
| **B** | Instance census + hygiene audit (UC-16) | ✅ done, **verified** | 52 |
| **C** | Knowledge base from GitHub | ✅ done | 65 |
| **D** | Static analysis + impact engine (doc phase 0) | ⬜ next | — |
| **E** | Probe module + instance contract (doc phase 1) | ⬜ | — |
| **F** | Tiers 1–2, baseline, verdict, pause/resume (doc phase 2) | ⬜ | — |
| **G** | Tier 3, evidence, cleanup (doc phase 3) | ⬜ | — |
| **H** | AI scenario proposal + task interpretation (doc phase 4) | ⬜ | — |
| **I** | Blocking, 17/19 profiles, dashboard merge (doc phase 5) | ⬜ | — |

**158 tests passing.** The user numbers phases from 1, so their "phase 1" is A, "phase 2"
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

### Phase D — next

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
```

**Restart the server after every code change** — Jinja auto-reloads templates, Python
changes are not picked up. The user tests in the browser immediately and will report a
fixed bug as still broken.

### Tests

Real PostgreSQL, real HTTP against fakes that reproduce the systems' actual rules
(`tests/fake_odoo.py`, `fake_staging.py`, `fake_github.py`).

```bash
createdb -U odoo odoo_qa_gate_test
.venv/bin/python tests/test_phase_a.py     # 41
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
| Product name | **Odoo PM Bot** (UI). Package/CLI are still `qa_gate` / `qa-gate` |

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
- One call to action per screen — the dashboard's "Add client" is hidden when the empty
  state already offers one.

---

## Verified technical facts

Checked against the local Odoo source trees, not recalled. Trust these.

### Playwright, not Hoot

- **Hoot does not exist in Odoo 17** (`web/static/lib/hoot` is 18/19 only; 17 is QUnit).
  We target 17, 18 and 19.
- **Hoot mocks the world** — `lib/hoot/mock/` ships `network.js`, `date.js`, `window.js`,
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
