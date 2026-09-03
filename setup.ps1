# Odoo PM Agent - Windows setup.
#
#   .\setup.ps1                 install everything, then say what to do next
#   .\setup.ps1 -Check          diagnose an existing install, change nothing
#   .\setup.ps1 -DbUrl "..."    use a connection string you already have
#
# If Windows refuses to run this ("running scripts is disabled on this system"),
# that is PowerShell's default execution policy and not a problem with the file.
# Run it this way instead, which applies the exception to this one command only:
#
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# Safe to run more than once. It never drops a database, deletes a config file,
# or overwrites an existing secret_key - that key encrypts every stored client
# credential, and regenerating it would silently orphan all of them.

param(
    [switch]$Check,
    [string]$DbUrl = "",
    [string]$DbName = "odoo_qa_gate"
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $Here

$Venv    = Join-Path $Here ".venv"
$VenvPy  = Join-Path $Venv "Scripts\python.exe"
$VenvGate= Join-Path $Venv "Scripts\qa-gate.exe"
$CfgDir  = Join-Path $env:USERPROFILE ".config\odoo-qa-gate"
$CfgFile = Join-Path $CfgDir "config.yaml"

function Say-Ok    ($m) { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Say-Info  ($m) { Write-Host "  .      $m" -ForegroundColor DarkGray }
function Say-Warn  ($m) { Write-Host "  [!]    $m" -ForegroundColor Yellow }
function Say-Step  ($m) { Write-Host "`n$m" -ForegroundColor White }
function Die       ($m) { Write-Host "  [x]    $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
Say-Step "Python"

# `py` is the Windows launcher and is the reliable way to ask for a version;
# `python` on PATH is often the Microsoft Store stub, which cannot make venvs.
$Py = $null
foreach ($try in @(@("py","-3.13"), @("py","-3.12"), @("py","-3"), @("python"))) {
    $exe = $try[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $args = @()
    if ($try.Count -gt 1) { $args += $try[1] }
    try {
        $v = & $exe @args -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
    } catch { continue }
    if (-not $v) { continue }
    $parts = $v.Trim().Split(".")
    if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
        $Py = @($exe) + $args
        break
    }
}

if (-not $Py) {
    Say-Warn "Python 3.12 or newer was not found."
    Say-Info "Install it from https://www.python.org/downloads/ and TICK"
    Say-Info "'Add python.exe to PATH' on the first screen of the installer."
    Say-Info "The Microsoft Store version will not work - it cannot create virtualenvs."
    Die "Install Python, then run this script again."
}
$PyVer = & $Py[0] @($Py[1..($Py.Count-1)]) --version
Say-Ok "$PyVer"

# ---------------------------------------------------------------------------
# 2. PostgreSQL
# ---------------------------------------------------------------------------
Say-Step "PostgreSQL"

$Psql = Get-Command psql -ErrorAction SilentlyContinue
if (-not $Psql) {
    # The EDB installer does not add its bin directory to PATH, so a working
    # Postgres very often looks like a missing one. Look before complaining.
    $guess = Get-ChildItem "C:\Program Files\PostgreSQL" -Directory -ErrorAction SilentlyContinue |
             Sort-Object Name -Descending | Select-Object -First 1
    if ($guess) {
        $candidate = Join-Path $guess.FullName "bin\psql.exe"
        if (Test-Path $candidate) {
            $env:PATH = "$($guess.FullName)\bin;$env:PATH"
            $Psql = Get-Command psql -ErrorAction SilentlyContinue
            Say-Info "found psql at $candidate and added it to PATH for this session"
        }
    }
}
if (-not $Psql) {
    Say-Warn "The psql client was not found."
    Say-Info "Install PostgreSQL 16 from https://www.postgresql.org/download/windows/"
    Say-Info "Remember the password you set for the 'postgres' user - it is needed below."
    Die "Install PostgreSQL, then run this script again."
}
Say-Ok "psql found"

$svc = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($svc -and $svc.Status -ne "Running") {
    Say-Warn "The PostgreSQL service '$($svc.Name)' is not running."
    Say-Info "Start it with:  Start-Service $($svc.Name)"
    Die "Start PostgreSQL, then run this script again."
}
if ($svc) { Say-Ok "service '$($svc.Name)' is running" }

# Windows Postgres has no peer authentication, so the app's default connection
# string (postgresql:///odoo_qa_gate) cannot work here - it relies on a Unix
# socket. A real host, user and password are required, which is why this asks.
if (-not $DbUrl) {
    if ($env:DATABASE_URL) {
        $DbUrl = $env:DATABASE_URL
        Say-Info "using DATABASE_URL from the environment"
    }
    elseif (Test-Path $CfgFile) {
        $line = Select-String -Path $CfgFile -Pattern '^database_url:\s*(.+)$' |
                Select-Object -First 1
        if ($line) {
            $DbUrl = $line.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'")
            Say-Info "using the database_url already in your config"
        }
    }
}
if (-not $DbUrl) {
    if ($Check) {
        Say-Warn "No database URL known yet (would be asked for)."
    } else {
        Write-Host ""
        Say-Info "PostgreSQL on Windows needs a user and password (there is no"
        Say-Info "passwordless local login as there is on Linux)."
        $dbUser = Read-Host "  PostgreSQL user [postgres]"
        if (-not $dbUser) { $dbUser = "postgres" }
        $secure = Read-Host "  Password for '$dbUser'" -AsSecureString
        $plain  = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        $DbUrl = "postgresql://${dbUser}:${plain}@localhost:5432/$DbName"
    }
}

if ($DbUrl) {
    $env:PGPASSWORD = $null
    $exists = $false
    try {
        & psql $DbUrl -c "SELECT 1" *> $null
        $exists = ($LASTEXITCODE -eq 0)
    } catch { $exists = $false }

    if ($exists) {
        Say-Ok "database reachable"
    }
    elseif ($Check) {
        Say-Warn "database '$DbName' is not reachable (would be created)"
    }
    else {
        # Connect to the always-present `postgres` database to create ours.
        $adminUrl = $DbUrl -replace "/$DbName(\?|$)", '/postgres$1'
        try {
            & psql $adminUrl -c "CREATE DATABASE `"$DbName`"" *> $null
        } catch { }
        & psql $DbUrl -c "SELECT 1" *> $null
        if ($LASTEXITCODE -eq 0) { Say-Ok "created database '$DbName'" }
        else { Die "Could not create or reach '$DbName'. Check the user and password." }
    }
}

# ---------------------------------------------------------------------------
# 3. Virtual environment and dependencies
# ---------------------------------------------------------------------------
Say-Step "Python environment"

if ($Check) {
    if (Test-Path $VenvGate) { Say-Ok "virtualenv present at .venv" }
    else { Say-Warn "no virtualenv at .venv (would be created)" }
}
else {
    if (Test-Path $Venv) { Say-Ok "reusing existing virtualenv at .venv" }
    else {
        & $Py[0] @($Py[1..($Py.Count-1)]) -m venv $Venv
        Say-Ok "created virtualenv at .venv"
    }

    Say-Info "installing dependencies (a few minutes on a first run)..."
    & $VenvPy -m pip install --quiet --upgrade pip
    & $VenvPy -m pip install --quiet --upgrade -e .
    if ($LASTEXITCODE -ne 0) { Die "Dependency install failed - see the output above." }
    Say-Ok "dependencies installed"

    # Chromium is a ~150MB download pip cannot do. Without it everything works
    # right up to the point where a review takes a screenshot.
    & $VenvPy -m playwright install chromium 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Say-Ok "chromium installed (screenshots will work)" }
    else { Say-Warn "chromium could not be installed; reviews will run but take no screenshots" }
}

if (-not (Test-Path $VenvGate)) { Die "qa-gate did not install into .venv. Re-run without -Check." }

# ---------------------------------------------------------------------------
# 4. Configuration and schema
# ---------------------------------------------------------------------------
Say-Step "Configuration"

if (Test-Path $CfgFile) {
    Say-Ok "existing config kept at $CfgFile"
    Say-Info "its secret_key is untouched - rotating it would orphan stored credentials"
} else {
    Say-Info "no config yet; one will be generated with a fresh encryption key"
}

if (-not $Check) {
    # Let the app mint its own config (and secret key), then set only the
    # database_url. Writing the whole file here would mean this script owning a
    # format the app is responsible for.
    $env:DATABASE_URL = $DbUrl
    & $VenvGate check *> $null

    $patch = @"
import sys, yaml, pathlib
p = pathlib.Path(r'$CfgFile')
raw = yaml.safe_load(p.read_text()) or {}
raw['database_url'] = r'$DbUrl'
p.write_text(yaml.safe_dump(raw, sort_keys=False))
print('database_url written to', p)
"@
    $patch | & $VenvPy -
    if ($LASTEXITCODE -ne 0) { Die "Could not write the database URL into the config." }
    Say-Ok "config points at your database"

    Say-Step "Database schema"
    & $VenvGate migrate
    if ($LASTEXITCODE -ne 0) { Die "Migrations failed - see the output above." }
}

Say-Step "Diagnostics"
& $VenvGate check

# ---------------------------------------------------------------------------
# 5. What to do next
# ---------------------------------------------------------------------------
if ($Check) {
    Write-Host "`nCheck complete. Re-run without -Check to install.`n"
    exit 0
}

Write-Host @"

 Setup complete.

 Start it:
     $VenvGate serve

 Then, in a browser:
   1. http://127.0.0.1:8770/setup   point it at the Odoo your staff log in to.
                                    The first person to sign in becomes admin.
   2. Settings > GitHub access      a read-only token, so private client repos
                                    resolve instead of looking like typos.
   3. Settings > AI provider        a DeepSeek key, if you want the agent to
                                    read client source. Optional.
   4. Add a client                  its Odoo project, staging URL and repo.

 Notes:
   * Config and the encryption key live in
       $CfgFile
     OUTSIDE this folder, deliberately. Copying the folder to another machine
     does not copy the key - and without it, stored client credentials cannot
     be decrypted. Move that file too, or re-enter the credentials.
   * Re-run .\setup.ps1 any time to update dependencies and apply migrations.
   * .\setup.ps1 -Check diagnoses without changing anything.

"@
