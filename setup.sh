#!/usr/bin/env bash
# setup.sh — install the Odoo QA Gate control plane on a Linux machine.
#
# Usage:
#   ./setup.sh             full setup (idempotent — safe to re-run)
#   ./setup.sh --check     diagnostic only, no install
#   ./setup.sh --help      this message
#
# What it does:
#   1. Verifies a Python 3.12+ interpreter is on PATH
#   2. Reports on external tools (git, gh, psql, google-chrome)
#   3. Creates ./.venv and installs the project into it
#   4. Creates the Postgres database if it is missing
#   5. Applies database migrations
#   6. Prints next steps
#
# What it does NOT do:
#   - Run as root or install system packages
#   - Configure which Odoo to authenticate against (that is the /setup page)
#   - Start the server

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CHECK_ONLY=false
DB_NAME="${QA_GATE_DB:-odoo_qa_gate}"
DB_USER="${QA_GATE_DB_USER:-$(whoami)}"

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=true ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  c_red=$'\033[31m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'
  c_blue=$'\033[34m'; c_dim=$'\033[2m';  c_reset=$'\033[0m'
else
  c_red=''; c_green=''; c_yellow=''; c_blue=''; c_dim=''; c_reset=''
fi

ok()   { printf '  %s✓%s %s\n' "$c_green"  "$c_reset" "$1"; }
warn() { printf '  %s⚠%s %s\n' "$c_yellow" "$c_reset" "$1"; }
err()  { printf '  %s✗%s %s\n' "$c_red"    "$c_reset" "$1"; }
info() { printf '  %s→%s %s\n' "$c_blue"   "$c_reset" "$1"; }
hdr()  { printf '\n%s== %s ==%s\n' "$c_blue" "$1" "$c_reset"; }

# ---- step 1: python --------------------------------------------------------

hdr "1. Python interpreter"

find_python() {
  local cand ver major minor
  for cand in python3.13 python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || continue
      major=${ver%%.*}; minor=${ver#*.}
      if [[ "$major" -ge 3 && "$minor" -ge 12 ]]; then echo "$cand"; return 0; fi
    fi
  done
  return 1
}

if PYTHON=$(find_python); then
  ok "found $PYTHON ($("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"
else
  err "no Python 3.12+ on PATH"
  info "Debian/Ubuntu: sudo apt install python3.12 python3.12-venv"
  exit 1
fi

# ---- step 2: external tools ------------------------------------------------

hdr "2. External tools"

check_tool() {
  if command -v "$1" >/dev/null 2>&1; then
    ok "$2 — $("$1" --version 2>/dev/null | head -n1 || echo '(version unknown)')"
  else
    warn "$2 not found"; [[ -n "${3:-}" ]] && info "$3"
  fi
}

check_tool psql          "psql (PostgreSQL client)" "needed to create the database"
check_tool git           "git"
check_tool gh            "gh (GitHub CLI)"          "needed from phase C onward"
check_tool google-chrome "google-chrome"            "used to render evidence bundles to PDF"

if $CHECK_ONLY; then
  hdr "Check complete"
  [[ -x .venv/bin/qa-gate ]] && .venv/bin/qa-gate check || info "not installed yet; re-run without --check"
  exit 0
fi

# ---- step 3: virtualenv ----------------------------------------------------

hdr "3. Virtual environment"

VENV="$SCRIPT_DIR/.venv"
if [[ -d "$VENV" && -x "$VENV/bin/python" ]]; then
  ok ".venv exists (Python $("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])'))"
else
  [[ -d "$VENV" ]] && { err ".venv exists but bin/python is missing"; info "rm -rf .venv && re-run"; exit 1; }
  info "creating .venv with $PYTHON"
  "$PYTHON" -m venv "$VENV"
  ok ".venv created"
fi

# ---- step 4: dependencies --------------------------------------------------

hdr "4. Dependencies"
"$VENV/bin/python" -m pip install --upgrade pip --quiet
ok "pip upgraded"
"$VENV/bin/pip" install -e . --quiet
ok "project installed"

# ---- step 5: database ------------------------------------------------------

hdr "5. Database"

if psql -U "$DB_USER" -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw "$DB_NAME"; then
  ok "database $DB_NAME exists"
else
  if createdb -U "$DB_USER" "$DB_NAME" 2>/dev/null; then
    ok "database $DB_NAME created"
  else
    warn "could not create $DB_NAME as role $DB_USER"
    info "create it by hand, then set database_url in the config file"
    info "  createdb -U <role> $DB_NAME"
  fi
fi

if "$VENV/bin/qa-gate" migrate 2>/dev/null; then
  ok "migrations applied"
else
  warn "migrations did not run — check database_url in the config"
fi

# ---- step 6: next steps ----------------------------------------------------

hdr "Setup complete"
cat <<EOF

  ${c_green}→${c_reset} start the server:  ${c_dim}./.venv/bin/qa-gate serve${c_reset}
  ${c_green}→${c_reset} open in browser:   ${c_dim}http://127.0.0.1:8770${c_reset}

  The first visit redirects to ${c_dim}/setup${c_reset}, where you point the gate at the Odoo
  that holds your team's user accounts. Staff then sign in with their normal Odoo
  credentials — there is no signup.

  Diagnostics: ${c_dim}./.venv/bin/qa-gate check${c_reset}

EOF
