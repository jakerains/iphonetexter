#!/usr/bin/env bash
# Quick-start launcher for iphonetexter.
#
# Bootstraps prereqs (Swift CLI, Python venv, deps), then hands off to
# scripts/launcher.py for the interactive control panel.
#
# Usage:
#   ./start.sh                  -- bootstrap, then show menu
#   ./start.sh start            -- bootstrap, then launch the web UI directly
#   ./start.sh update           -- bootstrap, then run update flow
#   ./start.sh install          -- bootstrap, then run reinstall flow
#   ./start.sh diag             -- bootstrap, then run diagnostics
#   ./start.sh logs             -- bootstrap, then show recent logs

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

VENV_DIR=".venv"
WEB_DIR="scripts/web_ui"
LAUNCHER="scripts/launcher.py"
IMSG_BIN_PATH="$REPO_ROOT/bin/imsg"
HOST="${IMSG_WEB_HOST:-127.0.0.1}"
PORT="${IMSG_WEB_PORT:-8765}"

# ---------- styling ----------
# Set up colors only if stdout is a TTY; degrade gracefully otherwise.
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
  C_BOLD="$(tput bold)"
  C_DIM="$(tput dim)"
  C_RED="$(tput setaf 1)"
  C_GREEN="$(tput setaf 2)"
  C_YELLOW="$(tput setaf 3)"
  C_CYAN="$(tput setaf 6)"
  C_RESET="$(tput sgr0)"
else
  C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""; C_RESET=""
fi

# Section header: ==> Building Swift CLI
step() { printf "\n%s==>%s %s%s%s\n" "$C_CYAN" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }

# Indented info line, optionally with status tag (OK/FAIL/skip)
info()      { printf "    %s\n" "$*"; }
info_ok()   { printf "    %-30s %s%s%s\n" "$1" "$C_GREEN" "OK" "$C_RESET"; }
info_skip() { printf "    %-30s %sskip%s\n" "$1" "$C_DIM" "$C_RESET"; }
info_warn() { printf "    %-30s %swarn%s  %s\n" "$1" "$C_YELLOW" "$C_RESET" "${2:-}"; }
info_fail() { printf "    %-30s %sFAIL%s  %s\n" "$1" "$C_RED" "$C_RESET" "${2:-}"; }

die() {
  printf "\n%serror:%s %s\n" "$C_RED" "$C_RESET" "$*" >&2
  exit 1
}

hr() { printf "%s\n" "------------------------------------------------------------"; }

# ---------- run-with-tail: pipe a command's output through a single overwriting status line ----------
# Usage: run_with_tail "label" cmd args...
run_with_tail() {
  local label="$1"; shift
  local log_file
  log_file="$(mktemp -t imsg-bootstrap.XXXXXX)"
  local cols
  cols="$(tput cols 2>/dev/null || echo 100)"
  local trunc=$((cols - 8))

  # Disable errexit around the pipe so we can capture PIPESTATUS ourselves;
  # pipefail would otherwise short-circuit before we read it.
  set +e
  "$@" 2>&1 | tee "$log_file" | while IFS= read -r line; do
    line="${line//$'\r'/}"
    [[ -z "$line" ]] && continue
    printf "\r\033[K    %s>%s %s" "$C_DIM" "$C_RESET" "${line:0:$trunc}"
  done
  local rc=${PIPESTATUS[0]}
  set -e

  if [[ $rc -ne 0 ]]; then
    printf "\r\033[K    %sFAIL%s (exit %d)\n" "$C_RED" "$C_RESET" "$rc"
    printf "    Last 20 lines of output (full log: %s):\n" "$log_file"
    tail -n 20 "$log_file" | sed 's/^/      /'
    return "$rc"
  fi

  printf "\r\033[K    %sdone%s\n" "$C_GREEN" "$C_RESET"
  rm -f "$log_file"
  return 0
}

# ---------- header banner ----------
clear_top() { [[ -t 1 ]] && printf "\033c" || true; }
clear_top

cat <<EOF
${C_BOLD}${C_CYAN}iphonetexter${C_RESET}  ${C_DIM}local web UI for imsg${C_RESET}
${C_DIM}repo:${C_RESET} https://github.com/jakerains/iphonetexter
EOF

# ---------- platform + prereq checks ----------
step "Checking prerequisites"
[[ "$(uname -s)" == "Darwin" ]] || die "iphonetexter is macOS-only (it drives Messages.app)."
info_ok "macOS"

check_cmd() {
  local name="$1" hint="$2"
  if command -v "$name" >/dev/null 2>&1; then
    local version
    version="$($name --version 2>/dev/null | head -1 | tr -d '\n' || true)"
    if [[ -n "$version" ]]; then
      printf "    %-30s %s%s%s  %s%s%s\n" "$name" "$C_GREEN" "OK" "$C_RESET" "$C_DIM" "$version" "$C_RESET"
    else
      info_ok "$name"
    fi
  else
    info_fail "$name" "$hint"
    return 1
  fi
}

check_cmd python3 "install via 'brew install python' or python.org" || die "python3 is required."
check_cmd swift   "install Xcode Command Line Tools: 'xcode-select --install'" || die "swift is required."
check_cmd make    "install Xcode Command Line Tools: 'xcode-select --install'" || die "make is required."

# ---------- Full Disk Access check ----------
step "Checking Full Disk Access"

chat_db_has_access() {
  local db="$HOME/Library/Messages/chat.db"
  [[ -e "$db" ]] && head -c 1 "$db" >/dev/null 2>&1
}

terminal_app_name() {
  case "${TERM_PROGRAM:-}" in
    Apple_Terminal) echo "Terminal.app" ;;
    iTerm.app)      echo "iTerm.app" ;;
    vscode)         echo "Visual Studio Code" ;;
    WarpTerminal)   echo "Warp.app" ;;
    Hyper)          echo "Hyper.app" ;;
    Tabby)          echo "Tabby.app" ;;
    Ghostty)        echo "Ghostty.app" ;;
    Alacritty)      echo "Alacritty.app" ;;
    kitty)          echo "kitty.app" ;;
    "")             echo "your terminal app" ;;
    *)              echo "your terminal app (TERM_PROGRAM=${TERM_PROGRAM})" ;;
  esac
}

prompt_for_fda() {
  local term_app
  term_app="$(terminal_app_name)"
  echo
  hr
  printf "  %sFull Disk Access required (one-time setup)%s\n" "$C_BOLD" "$C_RESET"
  hr
  cat <<EOF
  iphonetexter reads your iMessage history from
  ~/Library/Messages/chat.db, which macOS protects behind
  Full Disk Access (FDA). Unlike most permissions, FDA is
  ${C_BOLD}NOT auto-prompted${C_RESET} -- you have to grant it yourself.

  Steps when System Settings opens:
    1. Click the '+' button under Full Disk Access.
    2. Add: ${C_CYAN}${term_app}${C_RESET}
    3. Toggle the switch ON if it isn't already.
    4. macOS may ask you to quit and relaunch ${term_app}.
       If so: quit, relaunch, and run ./start.sh again.
       Otherwise: come back here and press Enter.
EOF
  hr
  echo
  printf "Open System Settings -> Full Disk Access now? [Y/n] "
  local answer=""
  read -r answer || true
  if [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; then
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null \
      || info_warn "open System Settings" "could not auto-open; navigate manually"
    echo
    echo "After granting access (and relaunching the terminal if macOS asked),"
    printf "press Enter to continue, or Ctrl+C to abort: "
    read -r _ || true
  fi
}

if chat_db_has_access; then
  info_ok "chat.db readable"
else
  info_warn "chat.db readable" "FDA not granted to $(terminal_app_name)"
  prompt_for_fda
  if chat_db_has_access; then
    info_ok "chat.db readable (re-check)"
  else
    info_warn "chat.db readable (re-check)" "still missing -- the UI will load but /chats and /inbound will be empty"
    info_warn "" "if you just granted FDA, quit and relaunch $(terminal_app_name) and re-run ./start.sh"
  fi
fi

# ---------- build imsg CLI if missing ----------
step "Swift CLI"
if [[ -x "$IMSG_BIN_PATH" ]]; then
  info_skip "bin/imsg already built"
else
  info "building (~1-2 min on first run)..."
  run_with_tail "make build" make build || die "Swift build failed."
fi
[[ -x "$IMSG_BIN_PATH" ]] || die "Build finished but $IMSG_BIN_PATH is missing or not executable."

# ---------- Python venv ----------
step "Python environment"
if [[ -d "$VENV_DIR" ]]; then
  info_skip ".venv already created"
else
  info "creating $VENV_DIR/..."
  python3 -m venv "$VENV_DIR"
  info_ok ".venv created"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

info "installing dependencies..."
run_with_tail "pip install" pip install --disable-pip-version-check --upgrade pip
run_with_tail "pip install -r $WEB_DIR/requirements.txt" pip install --disable-pip-version-check -r "$WEB_DIR/requirements.txt"

# ---------- handoff to Python launcher ----------
export IMSG_BIN="$IMSG_BIN_PATH"
export IMSG_WEB_HOST="$HOST"
export IMSG_WEB_PORT="$PORT"

# Tiny pause so the user sees the final 'done' line before the menu clears it.
sleep 0.2

exec python3 "$LAUNCHER" "$@"
