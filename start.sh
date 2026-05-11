#!/usr/bin/env bash
# Quick-start launcher for iphonetexter.
# Builds the imsg Swift CLI if needed, sets up a Python venv, installs the
# web UI deps, starts the server, and opens it in your browser.
#
# Usage:
#   ./start.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

VENV_DIR=".venv"
WEB_DIR="scripts/web_ui"
IMSG_BIN_PATH="$REPO_ROOT/bin/imsg"
HOST="${IMSG_WEB_HOST:-127.0.0.1}"
PORT="${IMSG_WEB_PORT:-8765}"
URL="http://${HOST}:${PORT}/"

log()  { printf "==> %s\n" "$*"; }
warn() { printf "    %s\n" "$*"; }
die()  { printf "error: %s\n" "$*" >&2; exit 1; }

# ---------- platform + prereq checks ----------
[[ "$(uname -s)" == "Darwin" ]] || die "iphonetexter is macOS-only (it drives Messages.app)."

command -v python3 >/dev/null 2>&1 || die "python3 not found. Install via 'brew install python' or python.org."
command -v swift   >/dev/null 2>&1 || die "swift not found. Install Xcode Command Line Tools: 'xcode-select --install'."
command -v make    >/dev/null 2>&1 || die "make not found. Install Xcode Command Line Tools: 'xcode-select --install'."

# ---------- build imsg CLI if missing ----------
if [[ ! -x "$IMSG_BIN_PATH" ]]; then
  log "Building imsg CLI (first run only -- this can take a minute or two)..."
  make build
else
  log "Found existing imsg binary at bin/imsg (run 'make clean && ./start.sh' to rebuild)."
fi

[[ -x "$IMSG_BIN_PATH" ]] || die "Build finished but $IMSG_BIN_PATH is missing or not executable."

# ---------- Python venv ----------
if [[ ! -d "$VENV_DIR" ]]; then
  log "Creating Python virtual environment at ${VENV_DIR}..."
  python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

log "Installing / updating web UI Python dependencies..."
pip install --quiet --disable-pip-version-check --upgrade pip
pip install --quiet --disable-pip-version-check -r "$WEB_DIR/requirements.txt"

# ---------- launch ----------
export IMSG_BIN="$IMSG_BIN_PATH"
export IMSG_WEB_HOST="$HOST"
export IMSG_WEB_PORT="$PORT"

log "Starting web UI at $URL"
warn "Press Ctrl+C to stop."
warn ""
warn "First-time setup: macOS will prompt for Full Disk Access and Messages.app"
warn "Automation. Grant both under System Settings -> Privacy & Security, then"
warn "restart this script if needed."
warn ""

# Open browser once server is responsive (background; gives up after ~30s).
(
  for _ in $(seq 1 60); do
    if curl -sS -o /dev/null -m 1 "$URL" 2>/dev/null; then
      open "$URL" || true
      exit 0
    fi
    sleep 0.5
  done
) &

exec python3 "$WEB_DIR/server.py"
