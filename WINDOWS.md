# Running Odoo PM Agent on Windows

`setup.ps1` does all of this for you. This file is what to read when it stops,
and why the Windows path differs from Linux at all.

---

## The short version

```powershell
cd C:\path\to\code_pm_review
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Then:

```powershell
.\.venv\Scripts\qa-gate.exe serve
```

and open <http://127.0.0.1:8770/setup>.

---

## Before you start: two installs

**Python 3.12 or newer** — <https://www.python.org/downloads/>

On the installer's first screen, tick **"Add python.exe to PATH"**. Do not use
the version from the Microsoft Store: it is sandboxed and cannot create the
virtual environment this project needs, and the failure it produces two steps
later does not mention the Store at all.

**PostgreSQL 16** — <https://www.postgresql.org/download/windows/>

**Write down the password you set for the `postgres` user.** The installer asks
for it once and never shows it again, and the setup script cannot create the
database without it. This is the single most common place a Windows install
stalls.

You do not need pgAdmin, Stack Builder, or any of the optional components.

---

## Why `setup.ps1` asks for a database password and `setup.sh` does not

On Linux the app connects with `postgresql:///odoo_qa_gate` — no host, no user,
no password. That works because of *peer authentication*: PostgreSQL talks over
a Unix socket and trusts the operating-system user on the other end.

**Windows has no Unix sockets and no peer authentication.** Every connection is
TCP with a username and password, so the default connection string cannot work
and a real one has to be built:

```
postgresql://postgres:YOURPASSWORD@localhost:5432/odoo_qa_gate
```

The script asks once, writes it into your config, and never asks again. If you
would rather not type it interactively:

```powershell
.\setup.ps1 -DbUrl "postgresql://postgres:secret@localhost:5432/odoo_qa_gate"
```

---

## "running scripts is disabled on this system"

This is PowerShell's default execution policy refusing to run *any* unsigned
script. It is not a problem with this file, and you do not need to change a
machine-wide setting to get past it:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

That applies the exception to this one command only.

---

## When something goes wrong

| Symptom | What it means |
|---|---|
| `Python 3.12 or newer was not found` | Either it is not installed, or "Add to PATH" was left unticked. Re-run the Python installer and choose **Modify → Add to PATH**. |
| `The psql client was not found` | PostgreSQL is probably installed but its `bin` folder is not on PATH — the EDB installer does not add it. The script looks in `C:\Program Files\PostgreSQL\*\bin` before giving up, so this usually means it genuinely is not installed. |
| `The PostgreSQL service is not running` | `Start-Service postgresql-x64-16` (adjust the version). |
| `Could not create or reach 'odoo_qa_gate'` | The user or password is wrong. Check it with `psql -U postgres -h localhost -c "SELECT 1"`. |
| `chromium could not be installed` | Only screenshots are affected; everything else runs. Fix later with `.\.venv\Scripts\python.exe -m playwright install chromium`. |

Diagnose without changing anything:

```powershell
.\setup.ps1 -Check
```

---

## Where your data lives

| What | Where |
|---|---|
| Config **and the encryption key** | `C:\Users\<you>\.config\odoo-qa-gate\config.yaml` |
| Database | PostgreSQL, `odoo_qa_gate` |
| The app itself | this folder |

The config file is **outside the project folder on purpose**, so the key that
encrypts every client's Odoo credentials never lands in git.

The consequence is worth stating plainly: **copying this folder to another
machine does not copy the key.** The app will start, generate a fresh one, and
every stored client credential will be undecryptable — not lost loudly, just
unreadable. To move an install, copy `config.yaml` separately over something
private, or plan to re-enter the credentials.

`setup.ps1` never regenerates that key. If a config already exists it is left
exactly alone.

---

## One caveat about this script

`setup.sh` has been run end-to-end on a clean copy and verified. `setup.ps1`
has **not been executed on a Windows machine** — it was written against the same
steps and its Python parts were tested, but the PowerShell itself is unproven.
If it fails, the steps above are the manual path and each one is short:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m playwright install chromium
$env:DATABASE_URL = "postgresql://postgres:secret@localhost:5432/odoo_qa_gate"
.\.venv\Scripts\qa-gate.exe migrate
.\.venv\Scripts\qa-gate.exe check
.\.venv\Scripts\qa-gate.exe serve
```
