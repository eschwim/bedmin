#!/usr/bin/env bash
# minectl setup — installs dependencies, configures PATH, and optionally
# installs the systemd user service for the scheduler daemon.
# Safe to re-run; all steps are idempotent.

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

ok()   { echo -e "${GREEN}✔${RESET}  $*"; }
info() { echo -e "${CYAN}→${RESET}  $*"; }
warn() { echo -e "${YELLOW}!${RESET}  $*"; }
err()  { echo -e "${RED}✘${RESET}  $*" >&2; }
hdr()  { echo -e "\n${BOLD}$*${RESET}"; }

# ---------------------------------------------------------------------------
# Locate project root (directory containing this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

hdr "minectl setup"
info "Project directory: $SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 1. Check Python >= 3.10
# ---------------------------------------------------------------------------
hdr "Checking prerequisites"

PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major="${ver%%.*}"
        minor="${ver##*.}"
        if [[ "$major" -ge 3 && "$minor" -ge 10 ]]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    err "Python 3.10 or higher is required but was not found."
    err "Install it via your package manager, e.g.: sudo apt install python3.12"
    exit 1
fi
ok "Python: $($PYTHON --version)"

# Check systemd --user is available (non-fatal)
SYSTEMD_AVAILABLE=false
if systemctl --user status &>/dev/null 2>&1 || systemctl --user list-units &>/dev/null 2>&1; then
    SYSTEMD_AVAILABLE=true
    ok "systemd user session: available"
else
    warn "systemd user session not detected — service installation will be skipped."
fi

# ---------------------------------------------------------------------------
# 2. Create virtual environment
# ---------------------------------------------------------------------------
hdr "Setting up virtual environment"

VENV_DIR="$SCRIPT_DIR/venv"
if [[ -d "$VENV_DIR" && -f "$VENV_DIR/bin/python" ]]; then
    ok "Virtual environment already exists: $VENV_DIR"
else
    info "Creating virtual environment at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtual environment created."
fi

PIP="$VENV_DIR/bin/pip"
PYTHON_VENV="$VENV_DIR/bin/python"

# Upgrade pip silently
"$PIP" install --quiet --upgrade pip

# ---------------------------------------------------------------------------
# 3. Install dependencies
# ---------------------------------------------------------------------------
hdr "Installing dependencies"

info "Installing from requirements.txt ..."
"$PIP" install --quiet -r "$SCRIPT_DIR/requirements.txt"
ok "Dependencies installed."

# ---------------------------------------------------------------------------
# 4. Install minectl package
# ---------------------------------------------------------------------------
hdr "Installing minectl"

"$PIP" install --quiet -e "$SCRIPT_DIR"
ok "minectl installed."
info "CLI:  $VENV_DIR/bin/minectl"
info "TUI:  $VENV_DIR/bin/minectl-tui"

# ---------------------------------------------------------------------------
# 5. Add venv/bin to PATH in shell config
# ---------------------------------------------------------------------------
hdr "Configuring PATH"

BIN_DIR="$VENV_DIR/bin"
PATH_LINE="export PATH=\"$BIN_DIR:\$PATH\""
PATH_MARKER="# minectl"

# Detect which shell config files to update
SHELL_CONFIGS=()
[[ -f "$HOME/.bashrc" ]]  && SHELL_CONFIGS+=("$HOME/.bashrc")
[[ -f "$HOME/.zshrc" ]]   && SHELL_CONFIGS+=("$HOME/.zshrc")
# Only fall back to .profile if neither rc file exists
if [[ ${#SHELL_CONFIGS[@]} -eq 0 ]]; then
    SHELL_CONFIGS+=("$HOME/.profile")
fi

for cfg in "${SHELL_CONFIGS[@]}"; do
    if grep -qF "$BIN_DIR" "$cfg" 2>/dev/null; then
        ok "PATH already configured in $cfg"
    else
        {
            echo ""
            echo "$PATH_MARKER"
            echo "$PATH_LINE"
        } >> "$cfg"
        ok "Added PATH entry to $cfg"
    fi
done

# Make it available in the current shell session too
export PATH="$BIN_DIR:$PATH"

# ---------------------------------------------------------------------------
# 6. Verify commands are reachable
# ---------------------------------------------------------------------------
hdr "Verifying installation"

if "$BIN_DIR/minectl" --help &>/dev/null; then
    ok "minectl CLI works."
else
    err "minectl CLI check failed. Something went wrong."
    exit 1
fi

if [[ -x "$BIN_DIR/minectl-tui" ]]; then
    ok "minectl-tui installed."
else
    warn "minectl-tui not found in $BIN_DIR."
fi

# ---------------------------------------------------------------------------
# 7. Systemd user service (optional)
# ---------------------------------------------------------------------------
hdr "Systemd service"

UNIT_PATH="$HOME/.config/systemd/user/minectl.service"

if [[ "$SYSTEMD_AVAILABLE" != "true" ]]; then
    warn "Skipping systemd setup (user session not available)."
elif [[ -f "$UNIT_PATH" ]]; then
    ok "Service already installed: $UNIT_PATH"
    info "To reinstall:  minectl daemon uninstall && minectl daemon install --start"
else
    echo ""
    read -r -p "$(echo -e "${CYAN}?${RESET}  Install systemd user service for the scheduler daemon? [Y/n] ")" REPLY
    REPLY="${REPLY:-Y}"
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        read -r -p "$(echo -e "${CYAN}?${RESET}  Start the daemon now? [Y/n] ")" START_NOW
        START_NOW="${START_NOW:-Y}"

        if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
            "$BIN_DIR/minectl" daemon install --start
        else
            "$BIN_DIR/minectl" daemon install
        fi
    else
        info "Skipped. To install later:  minectl daemon install --start"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
hdr "Setup complete"
echo -e "
  ${BOLD}Commands available after reloading your shell:${RESET}

    minectl --help                            CLI reference
    minectl-tui                               Interactive TUI
    minectl server create --name NAME         Add a server
    minectl daemon install --start            Install scheduler

  ${BOLD}Reload your shell now:${RESET}

    source ~/.bashrc    (bash)
    source ~/.zshrc     (zsh)
"
