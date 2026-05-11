#!/usr/bin/env bash
# Build an iPhoneTexter.app launcher you can drag into the Dock.
#
# The bundle is a minimal AppleScript app that opens Terminal and runs
# this repo's start.sh, so clicking it does the same thing as running
# ./start.sh by hand.
#
# Usage:
#   scripts/install-dock-app.sh                 -- write iPhoneTexter.app into the repo,
#                                                  clicking it runs './start.sh start'
#                                                  (skip menu, launch web UI directly)
#   scripts/install-dock-app.sh --menu          -- click runs './start.sh' (shows menu)
#   scripts/install-dock-app.sh -o <dir>        -- write the .app into <dir> instead
#   scripts/install-dock-app.sh --open          -- also launch the app once built

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="iPhoneTexter"
OUT_DIR="$REPO_ROOT"
DO_OPEN=0
START_ARG="start"   # default: skip the menu, jump straight to the web UI

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--out) OUT_DIR="${2:?missing dir for -o}"; shift 2 ;;
    --open)   DO_OPEN=1; shift ;;
    --menu)   START_ARG=""; shift ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

START_SH="$REPO_ROOT/start.sh"
[[ -x "$START_SH" ]] || { echo "error: $START_SH not found or not executable" >&2; exit 1; }
command -v osacompile >/dev/null 2>&1 || { echo "error: osacompile not found (macOS only)" >&2; exit 1; }

mkdir -p "$OUT_DIR"
APP_PATH="$OUT_DIR/${APP_NAME}.app"

tmp_script="$(mktemp -t iphonetexter-launcher.XXXXXX)"
trap 'rm -f "$tmp_script"' EXIT

# Build the shell command the AppleScript will run in Terminal. We append
# the start arg (if any) as a separate AppleScript string so it survives
# quoted-form-of escaping on the repo path.
if [[ -n "$START_ARG" ]]; then
  applescript_cmd='"cd " & quoted form of repoPath & " && ./start.sh '"$START_ARG"'"'
else
  applescript_cmd='"cd " & quoted form of repoPath & " && ./start.sh"'
fi

cat > "$tmp_script" <<APPLESCRIPT
on run
    set repoPath to "$REPO_ROOT"
    tell application "Terminal"
        activate
        do script ($applescript_cmd)
    end tell
end run
APPLESCRIPT

rm -rf "$APP_PATH"
osacompile -o "$APP_PATH" "$tmp_script"

cat <<EOF
Created: $APP_PATH
Points to: $START_SH${START_ARG:+ $START_ARG}

To add to your Dock:
  1. Open Finder at: $OUT_DIR
  2. Drag $APP_NAME.app into the Dock (left of the divider, with your other apps).
  Tip: you can also drag it into /Applications first if you prefer.

Test it now:
  open "$APP_PATH"
EOF

if [[ "$DO_OPEN" -eq 1 ]]; then
  open "$APP_PATH"
fi
