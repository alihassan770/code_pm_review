#!/usr/bin/env bash
#
# Odoo PM Agent — one-command setup.
#
#   ./setup.sh            install everything, then say what to do next
#   ./setup.sh --check    diagnose an existing install, change nothing
#   ./setup.sh --help
#
# Safe to run more than once. Every step checks whether it has already been done
# and skips it, so re-running after a failure resumes rather than restarts.
#
# What it will NEVER do, so you can run it against a working install without
# reading it first: drop a database, delete a config file, overwrite an existing
# secret_key, or touch anything belonging to a client. The secret key encrypts
# every stored client credential — regenerating it would silently orphan all of
# them, so if a config already exists this script leaves it exactly alone.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="$HERE/.venv"
DB_NAME="${QA_GATE_DB_NAME:-odoo_qa_gate}"
MIN_PY_MINOR=12
CHECK_ONLY=0
CFG="${QA_GATE_CONFIG:-$HOME/.config/odoo-qa-gate/config.yaml}"

# Colour only when attached to a terminal, so piping to a file or a CI log does
# not fill it with escape codes.
if [ -t 1 ]; then
  R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[1m'; D=$'\033[2m'; N=$'\033[0m'
else
  R=""; G=""; Y=""; B=""; D=""; N=""
fi

ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
info() { printf '  %s·%s %s\n' "$D" "$N" "$1"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '  %s✗%s %s\n' "$R" "$N" "$1" >&2; exit 1; }
step() { printf '\n%s%s%s\n' "$B" "$1" "$N"; }

usage() {
  sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -h|--help) usage ;;
    *) die "Unknown option: $arg (try --help)" ;;
  esac
done

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
step "Python"

PY=""
for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    minor="$("$candidate" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)"
    major="$("$candidate" -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo 0)"
    if [ "$major" -eq 3 ] && [ "$minor" -ge "$MIN_PY_MINOR" ]; then
      PY="$candidate"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo
  warn "Python 3.$MIN_PY_MINOR or newer is required and was not found."
  if command -v apt-get >/dev/null 2>&1; then
    info "Debian/Ubuntu:  sudo apt install python3.12 python3.12-venv"
  elif command -v dnf >/dev/null 2>&1; then
    info "Fedora/RHEL:    sudo dnf install python3.12"
  elif command -v brew >/dev/null 2>&1; then
    info "macOS:          brew install python@3.12"
  fi
  die "Install it, then run this script again."
fi
ok "$("$PY" --version) at $(command -v "$PY")"

# `venv` ships separately on Debian/Ubuntu and its absence is a confusing
# failure three steps later, so find out now.
if ! "$PY" -c 'import venv' >/dev/null 2>&1; then
  die "$PY has no venv module. On Debian/Ubuntu: sudo apt install ${PY}-venv"
fi

# ---------------------------------------------------------------------------
# 2. PostgreSQL
# ---------------------------------------------------------------------------
step "PostgreSQL"

if ! command -v psql >/dev/null 2>&1; then
  warn "The psql client was not found."
  if command -v apt-get >/dev/null 2>&1; then
    info "Debian/Ubuntu:  sudo apt install postgresql postgresql-client"
  elif command -v dnf >/dev/null 2>&1; then
    info "Fedora/RHEL:    sudo dnf install postgresql-server postgresql"
  elif command -v brew >/dev/null 2>&1; then
    info "macOS:          brew install postgresql@16 && brew services start postgresql@16"
  fi
  die "Install PostgreSQL, make sure it is running, then run this script again."
fi
ok "psql found at $(command -v psql)"

if ! pg_isready -q 2>/dev/null; then
  warn "PostgreSQL is installed but not accepting connections."
  info "Try:  sudo systemctl start postgresql    (or: brew services start postgresql@16)"
  die "Start PostgreSQL, then run this script again."
fi
ok "server is accepting connections"

db_exists() { psql -lqtA 2>/dev/null | cut -d'|' -f1 | grep -qx "$DB_NAME"; }

# An existing install may well connect as a role that is not the OS user — the
# default `postgresql:///odoo_qa_gate` uses peer auth, but a config can name
# anything. Testing the *configured* URL first stops this script reporting a
# missing role on a machine where everything already works, and stops it
# offering to create a role nobody needs.
# Precedence matches config.py: the environment wins over the file, so that a
# container (Railway injects DATABASE_URL) is diagnosed against the database it
# will actually use rather than against a file it does not have.
CONFIGURED_URL="${DATABASE_URL:-}"
if [ -z "$CONFIGURED_URL" ] && [ -f "$CFG" ]; then
  CONFIGURED_URL="$(sed -n 's/^database_url:[[:space:]]*//p' "$CFG" \
                    | head -1 | tr -d '"'"'"'' | tr -d "\r")"
fi

if [ -n "$CONFIGURED_URL" ] && psql "$CONFIGURED_URL" -c 'SELECT 1' >/dev/null 2>&1; then
  ok "configured database reachable"
  info "$CONFIGURED_URL"

# Otherwise this is a first run (or a broken one), and the default connection
# string uses peer authentication as the current OS user — so a missing role is
# the single most common first-run failure.
elif psql -lqt >/dev/null 2>&1; then
  ok "role '$USER' can connect"
  if db_exists; then
    ok "database '$DB_NAME' already exists — leaving its contents alone"
  elif [ "$CHECK_ONLY" -eq 1 ]; then
    warn "database '$DB_NAME' does not exist (would be created)"
  else
    createdb "$DB_NAME" && ok "created database '$DB_NAME'"
  fi
elif [ "$CHECK_ONLY" -eq 1 ]; then
  warn "role '$USER' cannot connect to PostgreSQL"
else
  warn "role '$USER' cannot connect to PostgreSQL yet."
  info "This needs one privileged command to create a role for you."
  printf '    %sRun:%s sudo -u postgres createuser --createdb "%s"\n' "$B" "$N" "$USER"
  # Only offer to run it when there is a human to answer. Piped into a log or
  # run from CI, this would otherwise block forever on a prompt nobody sees.
  if [ ! -t 0 ]; then
    die "Run the command above, then run this script again."
  fi
  if command -v sudo >/dev/null 2>&1; then
    printf '    Then re-run this script. '
    read -r -p "Try it now? [y/N] " reply
    case "$reply" in
      [yY]*)
        sudo -u postgres createuser --createdb "$USER" \
          && ok "role '$USER' created" \
          || die "Could not create the role. Run the command above by hand."
        createdb "$DB_NAME" && ok "created database '$DB_NAME'"
        ;;
      *) die "Create the role, then run this script again." ;;
    esac
  else
    die "Create a PostgreSQL role for '$USER' with CREATEDB, then run this again."
  fi
fi

# ---------------------------------------------------------------------------
# 3. Virtual environment and dependencies
# ---------------------------------------------------------------------------
step "Python environment"

if [ "$CHECK_ONLY" -eq 1 ]; then
  if [ -x "$VENV/bin/qa-gate" ]; then
    ok "virtualenv present at .venv"
  else
    warn "no virtualenv at .venv (would be created)"
  fi
else
  if [ -d "$VENV" ]; then
    ok "reusing existing virtualenv at .venv"
  else
    "$PY" -m venv "$VENV" && ok "created virtualenv at .venv"
  fi

  # --upgrade is what makes a re-run pick up changed dependencies rather than
  # silently keeping whatever was installed the first time.
  info "installing dependencies (this takes a minute on a first run)…"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet --upgrade -e . \
    || die "Dependency install failed. The output above says why."
  ok "dependencies installed"

  # Chromium is a ~150MB download that pip cannot do, and without it every
  # review runs fine right up to the point where it takes a screenshot.
  if "$VENV/bin/python" -c 'from playwright.sync_api import sync_playwright
with sync_playwright() as p: p.chromium.launch(headless=True).close()' \
       >/dev/null 2>&1; then
    ok "chromium ready (screenshots will work)"
  else
    info "downloading chromium for screenshots (~150MB, first run only)…"
    if "$VENV/bin/python" -m playwright install chromium >/dev/null 2>&1; then
      ok "chromium installed"
    else
      warn "chromium could not be installed. Reviews will run but take no"
      warn "screenshots. Fix later with: $VENV/bin/python -m playwright install chromium"
    fi
  fi
fi

QA_GATE="$VENV/bin/qa-gate"
[ -x "$QA_GATE" ] || die "qa-gate did not install into .venv. Re-run without --check."

# ---------------------------------------------------------------------------
# 4. Configuration and schema
# ---------------------------------------------------------------------------
step "Configuration"

# The app generates its own config with a fresh encryption key on first load, so
# there is nothing to write here — only something to report. Never regenerate:
# a new secret_key makes every stored client credential undecryptable.
if [ -f "$CFG" ]; then
  ok "existing config kept at $CFG"
  info "its secret_key is untouched — rotating it would orphan stored credentials"
else
  info "no config yet; one will be generated with a fresh encryption key"
fi

if [ "$CHECK_ONLY" -eq 0 ]; then
  step "Database schema"
  "$QA_GATE" migrate || die "Migrations failed. The output above says why."
fi

step "Diagnostics"
"$QA_GATE" check || true

# ---------------------------------------------------------------------------
# 5. What to do next
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" -eq 1 ]; then
  printf '\n%sCheck complete.%s Re-run without --check to install.\n\n' "$B" "$N"
  exit 0
fi

cat <<EOF

$B Setup complete.$N

 Start it:
     $VENV/bin/qa-gate serve

 Then, in a browser:
   1. http://127.0.0.1:8770/setup   point it at the Odoo your staff log in to.
                                    The first person to sign in becomes admin.
   2. Settings → GitHub access      a read-only token, so private client repos
                                    resolve instead of looking like typos.
   3. Settings → AI provider        a DeepSeek key, if you want the agent to
                                    read client source. Optional.
   4. Add a client                  its Odoo project id, staging URL and repo.

 Notes:
   · Config and the encryption key live in
       $CFG
     OUTSIDE this folder, deliberately. Copying the folder to another machine
     does not copy the key — and without the key, stored client credentials
     cannot be decrypted. Move that file too, or re-enter the credentials.
   · Re-run ./setup.sh any time to update dependencies and apply new migrations.
   · ./setup.sh --check diagnoses without changing anything.

EOF
