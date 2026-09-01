#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────
# Work Context Mirror — First-Run Setup (macOS / Linux)
# ──────────────────────────────────────────────────────────────────

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

step=0
step() { step=$((step + 1)); echo -e "\n${BOLD}[$step] $1${RESET}"; }
ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
warn() { echo -e "  ${YELLOW}!${RESET} $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; }
ask()  { read -rp "  $1 [Y/n] " ans; [[ "${ans:-y}" =~ ^[Yy] ]]; }

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║          Work Context Mirror — First-Run Setup              ║"
echo "║                                                              ║"
echo "║  Creates a local, LLM-friendly mirror of your work          ║"
echo "║  knowledge from Confluence, Jira, and SharePoint.            ║"
echo "║                                                              ║"
echo "║  This script will:                                           ║"
echo "║    1. Check prerequisites (Python, uv)                       ║"
echo "║    2. Install dependencies                                   ║"
echo "║    3. Guide you through configuration                        ║"
echo "║    4. Validate the setup                                     ║"
echo "║    5. Optionally run the first sync and install the daemon   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${RESET}"

# ── 1. Python ──────────────────────────────────────────────────────

step "Checking Python"
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 12 ]]; then
        ok "Python $PY_VERSION found"
    else
        fail "Python $PY_VERSION found, but 3.12+ is required"
        if [[ "$(uname)" == "Darwin" ]] && command -v brew &>/dev/null; then
            warn "Install with: brew install python@3.13"
        elif command -v apt &>/dev/null; then
            warn "Install with: sudo apt install python3.13"
        fi
        exit 1
    fi
else
    fail "Python not found"
    if [[ "$(uname)" == "Darwin" ]]; then
        warn "Install with: brew install python@3.13"
    else
        warn "Install Python 3.12+ from https://www.python.org/downloads/"
    fi
    exit 1
fi

# ── 2. uv ─────────────────────────────────────────────────────────

step "Checking uv package manager"
if command -v uv &>/dev/null; then
    ok "uv found: $(uv --version 2>/dev/null || echo 'installed')"
else
    warn "uv not found"
    if ask "Install uv now?"; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        if command -v uv &>/dev/null; then
            ok "uv installed"
        else
            fail "uv installation failed"
            exit 1
        fi
    else
        fail "uv is required. Install from https://docs.astral.sh/uv/"
        exit 1
    fi
fi

# ── 3. Dependencies ───────────────────────────────────────────────

step "Installing dependencies"
export PATH="$HOME/.local/bin:$PATH"
uv sync
ok "Core dependencies installed"

# ── 4. SharePoint browser mode ────────────────────────────────────

step "SharePoint browser mode (optional)"
echo "  Browser mode lets you sync SharePoint libraries without"
echo "  OneDrive local sync. Uses Playwright for authentication."
if ask "Install Playwright for SharePoint browser access?"; then
    uv pip install playwright
    uv run playwright install chromium
    ok "Playwright + Chromium installed"
else
    warn "Skipped — you can install later with:"
    echo "    uv pip install playwright && uv run playwright install chromium"
fi

# ── 5. Interactive config ─────────────────────────────────────────

step "Configuration"
if ls ./*.yaml ./*.yml 2>/dev/null | head -1 &>/dev/null; then
    CONFIG_FILE=$(ls ./*.yaml ./*.yml 2>/dev/null | head -1)
    ok "Existing config found: $CONFIG_FILE"
    if ask "Run interactive setup to create a new config instead?"; then
        uv run workctx init
        CONFIG_FILE=$(ls -t ./*.yaml 2>/dev/null | head -1)
    fi
else
    echo "  No config file found. Let's create one."
    uv run workctx init
    CONFIG_FILE=$(ls -t ./*.yaml 2>/dev/null | head -1)
    ok "Config created: $CONFIG_FILE"
fi

# ── 6. Doctor ─────────────────────────────────────────────────────

step "Validating setup"
echo ""
uv run workctx doctor --config "$CONFIG_FILE" --verbose || true

# ── 7. First sync ─────────────────────────────────────────────────

step "First sync"
echo "  The first sync downloads all content from your configured"
echo "  sources. This can take a while for large workspaces."
if ask "Run the first full sync now?"; then
    uv run workctx sync --config "$CONFIG_FILE" --full
    ok "First sync complete"
else
    warn "Skipped — run later with: uv run workctx sync --config $CONFIG_FILE --full"
fi

# ── 8. Background service ─────────────────────────────────────────

step "Background daemon"
echo "  The daemon runs in the background, syncing daily and"
echo "  accepting Telegram commands (/sync, /status, /help)."
if ask "Install the background daemon service?"; then
    uv run workctx install-service --config "$CONFIG_FILE"
    ok "Daemon service installed"
else
    warn "Skipped — install later with: uv run workctx install-service"
fi

# ── Done ──────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}Setup complete!${RESET}"
echo ""
echo "  Useful commands:"
echo "    uv run workctx sync          # Run incremental sync"
echo "    uv run workctx status        # Show sync status"
echo "    uv run workctx search \"...\"  # Search the corpus"
echo "    uv run workctx doctor        # Validate setup"
echo "    uv run workctx daemon        # Run daemon in foreground"
echo "    uv run workctx service-status # Check daemon status"
echo ""
echo "  Telegram commands (if configured):"
echo "    /sync     — trigger sync from your phone"
echo "    /status   — check sync status"
echo "    /help     — list commands"
echo ""
