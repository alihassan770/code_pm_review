# Odoo PM Agent

An automated verification stage between "the developer says it is done" and "a human looks
at it". It takes an Odoo `project.task` plus a git diff, runs against the client's **own
managed staging instance** on their real data and real installed modules, and returns a
verdict with an evidence bundle.

It is a **gate**, not a test runner. A test runner answers "did my tests pass". The question
the business has is "is this safe to show the client".

The full design is in [`plan_files/odoo-qa-gate-build-plan-rev3.html`](plan_files/)
(23 sections, 61 pages). Read it before changing architecture.
[`CLAUDE.md`](CLAUDE.md) is the working context: decisions, verified constraints, and the
build plan.

## Status

**Phases A–C of I complete — 158 tests passing.** The app authenticates staff against Odoo,
registers clients, audits their staging instances read-only, and builds a per-client
knowledge base from their repositories. **Nothing writes to a client's Odoo database yet;**
the first phase that does is E.

| | Phase | Ships | Writes to a client DB? | Tests |
|---|---|---|---|---|
| ✅ **A** | App shell, login, clients | Log in, manage clients | No | 41 |
| ✅ **B** | Instance census + hygiene audit (UC-16) | Which staging instances are unsafe | No | 52 |
| ✅ **C** | Knowledge base from GitHub | Coverage map, source map | No | 65 |
| **D** | Static analysis + impact engine | Blast radius | No | |
| **E** | Probe module + instance contract | The safety core | Rolled back | |
| **F** | Tiers 1–2, baseline, verdict, pause/resume | Regression detection | Rolled back | |
| **G** | Tier 3, evidence, cleanup | Screenshots | Bounded + reported | |
| **H** | AI scenario proposal | Suite growth | — | |
| **I** | Blocking, multi-version, dashboard merge | Steady state | — | |

## Running locally

Needs Python 3.12+ and a reachable PostgreSQL.

```bash
./setup.sh                  # idempotent; --check for diagnostics only
./.venv/bin/qa-gate serve   # http://127.0.0.1:8770
```

First visit redirects to `/setup`, where you point the gate at the Odoo holding your team's
accounts. Staff then sign in with their normal Odoo credentials — **there is no signup**.
Removing someone in Odoo removes their access here.

Other commands:

```bash
./.venv/bin/qa-gate check     # config, Postgres, and identity-Odoo reachability
./.venv/bin/qa-gate migrate   # apply pending migrations
```

It binds to `127.0.0.1` by default, deliberately: it holds client credentials and nothing is
terminating TLS on a laptop.

## Deploying to Railway

1. **Add a PostgreSQL service.** Railway injects `DATABASE_URL`; the app reads it directly.
   Migrations run automatically on boot, so there is no release command to configure.
2. **Set the environment variables** in [`.env.example`](.env.example). Two are not optional:

   - `QA_GATE_SECRET_KEY` — generate once with
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
     It encrypts every client's Odoo credentials. A container has no persistent filesystem,
     so **without this a fresh key is minted on every deploy and every stored credential
     becomes undecryptable.**
   - `QA_GATE_SECURE_COOKIES=true` — Railway terminates TLS. Leave it false and the session
     cookie is dropped, which looks like login silently failing.

   Set `QA_GATE_ODOO_URL` and `QA_GATE_ODOO_DB` too. That skips `/setup` entirely, which is
   what you want in a container — `/setup` writes to a config file that does not survive a
   redeploy, and the app refuses rather than pretending it saved.
3. Deploy. `railway.json` sets the start command and points the healthcheck at `/healthz`,
   which touches Postgres so a process that is up but cannot reach its database reports
   unhealthy rather than lying.

Environment variables always override the config file, so the same code path serves a
laptop and a container.

## Tests

Real PostgreSQL, real HTTP against a fake Odoo (`tests/fake_odoo.py`) that reproduces
Odoo's actual credential rules — including that a 2FA account rejects a password over RPC
and accepts only an API key.

```bash
createdb odoo_qa_gate_test
.venv/bin/python tests/test_phase_a.py    # 41
.venv/bin/python tests/test_phase_b.py    # 52
.venv/bin/python tests/test_phase_c.py    # 65
dropdb odoo_qa_gate_test
```

Each suite creates its own schema, so drop and recreate the database between suites.

## Two constraints worth knowing before contributing

**There is no second database.** Clients run on Odoo.sh, Cloudpepper, and similar managed
hosts that will not let you clone, duplicate, or create one. Revision 3 removed the
database-clone path entirely, along with three use cases that depended on it (module
uninstall testing, upgrade dry runs, and executing migration scripts). Nothing in this
codebase may assume shell or PostgreSQL access to a client instance — authenticated
JSON-RPC is the only floor.

**An API key cannot open a browser session.** Verified in Odoo's `res_users.py`: the
API-key branch of `_check_credentials` sits behind `if not interactive:`. So the RPC plane
uses API keys, and screenshot evidence in phase G needs real passwords for dedicated QA
persona users. Conversely, a 2FA account cannot use a password over RPC at all.
