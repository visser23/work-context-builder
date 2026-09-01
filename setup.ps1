# ──────────────────────────────────────────────────────────────────
# Work Context Mirror — First-Run Setup (Windows PowerShell)
# ──────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"
$step = 0

function Step($msg) { $script:step++; Write-Host "`n[$script:step] $msg" -ForegroundColor White -NoNewline; Write-Host "" }
function Ok($msg)   { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "  ✗ $msg" -ForegroundColor Red }
function Ask($msg)  {
    $ans = Read-Host "  $msg [Y/n]"
    return ($ans -eq "" -or $ans -match "^[Yy]")
}

Write-Host @"

╔══════════════════════════════════════════════════════════════╗
║          Work Context Mirror — First-Run Setup              ║
║                                                              ║
║  Creates a local, LLM-friendly mirror of your work          ║
║  knowledge from Confluence, Jira, and SharePoint.            ║
║                                                              ║
║  This script will:                                           ║
║    1. Check prerequisites (Python, uv)                       ║
║    2. Install dependencies                                   ║
║    3. Guide you through configuration                        ║
║    4. Validate the setup                                     ║
║    5. Optionally run the first sync and install the daemon   ║
╚══════════════════════════════════════════════════════════════╝

"@ -ForegroundColor Cyan

# ── 1. Python ──────────────────────────────────────────────────────

Step "Checking Python"
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) { $pyCmd = Get-Command python3 -ErrorAction SilentlyContinue }

if ($pyCmd) {
    $pyVersion = & $pyCmd.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $parts = $pyVersion -split "\."
    if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 12) {
        Ok "Python $pyVersion found"
    } else {
        Fail "Python $pyVersion found, but 3.12+ is required"
        Warn "Install from https://www.python.org/downloads/"
        Warn "Or: winget install Python.Python.3.13"
        exit 1
    }
} else {
    Fail "Python not found"
    Warn "Install from https://www.python.org/downloads/"
    Warn "Or: winget install Python.Python.3.13"
    exit 1
}

# ── 2. uv ─────────────────────────────────────────────────────────

Step "Checking uv package manager"
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCmd) {
    Ok "uv found"
} else {
    Warn "uv not found"
    if (Ask "Install uv now?") {
        irm https://astral.sh/uv/install.ps1 | iex
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($uvCmd) {
            Ok "uv installed"
        } else {
            Fail "uv installation failed"
            exit 1
        }
    } else {
        Fail "uv is required. Install from https://docs.astral.sh/uv/"
        exit 1
    }
}

# ── 3. Dependencies ───────────────────────────────────────────────

Step "Installing dependencies"
uv sync
Ok "Core dependencies installed"

# ── 4. SharePoint browser mode ────────────────────────────────────

Step "SharePoint browser mode (optional)"
Write-Host "  Browser mode lets you sync SharePoint libraries without"
Write-Host "  OneDrive local sync. Uses Playwright for authentication."
if (Ask "Install Playwright for SharePoint browser access?") {
    uv pip install playwright
    uv run playwright install chromium
    Ok "Playwright + Chromium installed"
} else {
    Warn "Skipped — install later with:"
    Write-Host "    uv pip install playwright; uv run playwright install chromium"
}

# ── 5. Interactive config ─────────────────────────────────────────

Step "Configuration"
$configs = Get-ChildItem -Filter "*.yaml" -ErrorAction SilentlyContinue
if ($configs) {
    $configFile = $configs[0].Name
    Ok "Existing config found: $configFile"
    if (Ask "Run interactive setup to create a new config instead?") {
        uv run workctx init
        $configFile = (Get-ChildItem -Filter "*.yaml" | Sort-Object LastWriteTime -Descending | Select-Object -First 1).Name
    }
} else {
    Write-Host "  No config file found. Let's create one."
    uv run workctx init
    $configFile = (Get-ChildItem -Filter "*.yaml" | Sort-Object LastWriteTime -Descending | Select-Object -First 1).Name
    Ok "Config created: $configFile"
}

# ── 6. Doctor ─────────────────────────────────────────────────────

Step "Validating setup"
Write-Host ""
uv run workctx doctor --config $configFile --verbose

# ── 7. First sync ─────────────────────────────────────────────────

Step "First sync"
Write-Host "  The first sync downloads all content from your configured"
Write-Host "  sources. This can take a while for large workspaces."
if (Ask "Run the first full sync now?") {
    uv run workctx sync --config $configFile --full
    Ok "First sync complete"
} else {
    Warn "Skipped — run later with: uv run workctx sync --config $configFile --full"
}

# ── 8. Background service ─────────────────────────────────────────

Step "Background daemon"
Write-Host "  The daemon runs in the background, syncing daily and"
Write-Host "  accepting Telegram commands (/sync, /status, /help)."
if (Ask "Install the background daemon service?") {
    uv run workctx install-service --config $configFile
    Ok "Daemon service installed"
} else {
    Warn "Skipped — install later with: uv run workctx install-service"
}

# ── Done ──────────────────────────────────────────────────────────

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host @"

  Useful commands:
    uv run workctx sync          # Run incremental sync
    uv run workctx status        # Show sync status
    uv run workctx search "..."  # Search the corpus
    uv run workctx doctor        # Validate setup
    uv run workctx daemon        # Run daemon in foreground
    uv run workctx service-status # Check daemon status

  Telegram commands (if configured):
    /sync     — trigger sync from your phone
    /status   — check sync status
    /help     — list commands

"@
