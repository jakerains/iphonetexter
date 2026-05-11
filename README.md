# imsg

`imsg` is a macOS command-line tool for Messages.app. It reads your local
Messages database, streams new iMessage/SMS rows, sends messages through
Messages.app automation, and exposes the same surfaces over JSON and JSON-RPC.

Most read workflows need only Full Disk Access. Sending and standard tapbacks
also need macOS Automation permission for Messages.app. Advanced IMCore features
such as read receipts, typing indicators, and injection status are opt-in and
are increasingly limited by macOS 26.

## Quick start (web UI)

If you just want the easiest path — a local browser UI for browsing chats,
sending one-offs, and running bulk jobs — clone the repo and run:

```bash
./start.sh
```

To turn it into a global `iphonetexter` command (so you can launch from
anywhere instead of `cd`'ing into the repo):

```bash
make install            # drops a wrapper in ~/.local/bin
iphonetexter            # then launch from anywhere
```

(The wrapper is generated and points back at this clone. If you move the
repo, run `make install` again from the new location. Override the install
directory with `make install INSTALL_DIR=/usr/local/bin`. Remove with
`make uninstall`.)

### One-click Dock launcher

Prefer clicking an icon over typing? Build a tiny `.app` you can drag into
the Dock:

```bash
make dock-app           # writes iPhoneTexter.app into the repo root
open iPhoneTexter.app   # optional: smoke-test it
```

Then open Finder, drag `iPhoneTexter.app` into your Dock (left of the
divider, with your other apps), and clicking it will pop open Terminal and
run `./start.sh start` — bootstrap, then straight to the web UI, no menu.

The bundle is just an AppleScript that points back at this clone, so if
you move the repo, re-run `make dock-app`. It's git-ignored. Pass
`--menu` to `scripts/install-dock-app.sh` (or call the script directly) if
you'd rather the click show the full control-panel menu.

### Upgrading an existing clone

If you cloned this repo before the launcher was added (or any earlier
version), one `git pull` brings you forward:

```bash
cd /path/to/your/clone
git pull origin main         # gets the launcher + control panel
make install                 # one-time, if you want the global command
iphonetexter                 # status panel shows the new version
```

After that, the **Update from origin** action inside the control panel handles
every future update — `git pull`, conditional Swift rebuild, and pip refresh
in one step. `start.sh` also prints a one-line `* N commit(s) behind origin`
hint at launch when your local clone falls behind (cached state, no network
call), so you'll see when updates are available.

That single command bootstraps everything and drops you into an interactive
control panel:

1. Checks macOS prereqs (python3, swift, make) with pass/fail per check.
2. Detects Full Disk Access by trying to read `chat.db`; if missing, walks
   you through granting it (auto-opens the right System Settings pane and
   names your specific terminal app, e.g. Warp.app, iTerm.app).
3. Builds the `imsg` Swift CLI into `bin/imsg` (first run only, ~1–2 minutes,
   with a live tail of each "Compiling …" line).
4. Creates `.venv/` and installs the web UI + control-panel dependencies.
5. Hands off to `scripts/launcher.py` — a `rich`-based control panel with a
   status header (imsg version, repo SHA, FDA state, server status) and an
   arrow-key menu:

   - **Start web UI** — launches FastAPI on <http://127.0.0.1:8765/> and
     opens it in your browser.
   - **Update from origin** — `git pull`, then rebuilds the Swift CLI only
     if `Sources/` changed; re-runs pip in case dependencies moved.
   - **Reinstall from scratch** — nukes `.venv/` and `bin/`, then re-runs
     the bootstrap.
   - **Diagnose permissions** — re-checks FDA, exercises `imsg chats`
     end-to-end, optionally probes Messages.app Automation.
   - **View recent logs** — tails the last web UI session's stdout.

Re-runs are cheap — the bootstrap skips build/venv/pip steps it's already done.
For one-shot non-interactive invocations:

```bash
./start.sh start     # bootstrap + launch web UI directly (no menu)
./start.sh update    # bootstrap + run update flow
./start.sh diag      # bootstrap + run diagnostics
./start.sh install   # bootstrap + reinstall from scratch
```

`make start` works the same way; `make start ARGS=start` passes through.

**Requirements:** macOS 14+, Xcode Command Line Tools (`xcode-select --install`),
Python 3.10+, and macOS Full Disk Access + Messages.app Automation permission
for the terminal you launch from (see [Permissions Troubleshooting](#permissions-troubleshooting)).

## Highlights

- Read recent chats and message history without modifying `chat.db`.
- Stream new messages with `watch`, including a fallback poll when macOS misses
  file events.
- Send text and files through Messages.app AppleScript, without private send
  APIs.
- Inspect direct chats and groups, including participants, GUIDs, service, and
  account routing hints.
- Emit newline-delimited JSON for automation, agents, and scripts.
- Resolve Contacts names when permission is granted, while keeping raw handles
  in the output.
- Report attachment metadata, and optionally expose model-compatible converted
  receive-side CAF/GIF files.
- Use JSON-RPC over stdio for long-running integrations.

## Requirements

- macOS 14 or newer.
- Messages.app signed in to iMessage and/or SMS relay.
- Full Disk Access for the terminal or parent app that launches `imsg`.
- Automation permission for Messages.app when using `send` or `react`.
- Optional Contacts permission for name resolution.
- Optional `ffmpeg` on `PATH` for receive-side attachment conversion.

For SMS, enable Text Message Forwarding on your iPhone for this Mac.

## Install

```bash
brew install steipete/tap/imsg
```

Build from source:

```bash
make build
./bin/imsg --help
```

## Common Workflows

List recent chats:

```bash
imsg chats --limit 10
imsg chats --limit 10 --json
```

Inspect one chat before sending or wiring automation:

```bash
imsg group --chat-id 42 --json
```

Read history:

```bash
imsg history --chat-id 42 --limit 20
imsg history --chat-id 42 --limit 20 --attachments --json
imsg history --chat-id 42 --start 2026-05-01T00:00:00Z --end 2026-05-06T00:00:00Z --json
```

Stream new messages:

```bash
imsg watch --chat-id 42 --json
imsg watch --chat-id 42 --since-rowid 9000 --attachments --reactions --debounce 250ms --json
```

Send a message or file:

```bash
imsg send --to "+14155551212" --text "hi" --service imessage
imsg send --to "Jane Appleseed" --text "voice note" --file ~/Desktop/voice.m4a
imsg send --chat-id 42 --text "same thread"
```

Send a standard tapback:

```bash
imsg react --chat-id 42 --reaction like
```

Normalize a handle to its canonical form (E.164 for phones, unchanged for
emails). Use `--json` from scripts to also get a `valid` flag and `kind`:

```bash
imsg normalize --to "(415) 555-1212"
imsg normalize --to "+1 650-253-0000" --json
```

Generate integration help:

```bash
imsg completions zsh
imsg completions llm
```

## Commands

- `imsg chats [--limit 20] [--json]`
- `imsg group --chat-id <id> [--json]`
- `imsg history --chat-id <id> [--limit 50] [--attachments] [--convert-attachments] [--participants <handles>] [--start <iso>] [--end <iso>] [--json]`
- `imsg watch [--chat-id <id>] [--since-rowid <id>] [--debounce <duration>] [--attachments] [--convert-attachments] [--reactions] [--participants <handles>] [--start <iso>] [--end <iso>] [--json]`
- `imsg send (--to <handle-or-contact-name> | --chat-id <id> | --chat-identifier <id> | --chat-guid <guid>) [--text <text>] [--file <path>] [--service imessage|sms|auto] [--region US] [--json]`
- `imsg normalize --to <handle> [--region US] [--json]`
- `imsg react --chat-id <id> --reaction love|like|dislike|laugh|emphasis|question`
- `imsg read --to <handle> [--chat-id <id> | --chat-identifier <id> | --chat-guid <guid>]`
- `imsg typing --to <handle> [--duration 5s] [--stop true] [--service imessage|sms|auto]`
- `imsg status [--json]`
- `imsg launch [--dylib <path>] [--kill-only] [--json]`
- `imsg rpc`
- `imsg completions bash|zsh|fish|llm`

`react` intentionally sends only the standard tapbacks that Messages.app exposes
reliably through automation. Custom emoji tapbacks can be read from
history/watch output, but are not sent by the CLI.

## JSON Output

`--json` emits one JSON object per line, so consumers can stream it directly or
collect it with `jq -s`.

Chat objects include:

- `id`, `name`, `identifier`, `guid`, `service`, `last_message_at`
- `display_name`, `contact_name`
- `is_group`, `participants`
- `account_id`, `account_login`, `last_addressed_handle`

Message objects include:

- `id`, `chat_id`, `chat_identifier`, `chat_guid`, `chat_name`
- `participants`, `is_group`
- `guid`, `reply_to_guid`, `destination_caller_id`
- `sender`, `sender_name`, `is_from_me`, `text`, `created_at`
- `attachments`, `reactions`

When `watch --reactions --json` sees a tapback event, the message object also
includes `is_reaction`, `reaction_type`, `reaction_emoji`, `is_reaction_add`,
and `reacted_to_guid`.

Routing fields such as `destination_caller_id`, `account_id`,
`account_login`, and `last_addressed_handle` are read-only diagnostics from
Messages. AppleScript does not expose a way for `imsg send` to force a specific
outgoing Apple ID phone number or inline reply target.

## Bulk Send

Sending the same body to many recipients is supported through optional Python
tooling under `scripts/`:

- `scripts/bulk_send.py` — CLI driver. Reads a CSV with a `handle` column,
  normalizes each handle, looks each one up against `chat.db` to bucket by
  service, and paces iMessage and SMS sends separately. Persists to a
  SQLite store at `<recipients dir>/imsg.db` (or `--db <path>`); resume
  state lives in the `sends` table.
- `scripts/web_ui/` — local FastAPI + HTMX web UI on `http://127.0.0.1:8765/`
  that wraps the same engine with persistent contact lists, a one-off
  send form, a chat browser with opt-out badges, an inbound feed, and
  automatic opt-out detection (phrases like "stop", "unsubscribe" flip
  the sender's contact to opted-out and skip future sends). See
  `scripts/web_ui/README.md`.

Both require Full Disk Access for `chat.db` reads and Automation permission
for Messages.app — the same prereqs as `imsg send`. Identical-body bulk SMS
through a personal line is rate-limited by carriers; default paces are 3-6s
for iMessage and 15-30s for SMS / unknown buckets.

```bash
python scripts/bulk_send.py \
  --recipients recipients.csv \
  --message "see you Saturday at 6pm" \
  --confirm
```

Without `--confirm` the run is dry-run only — it still does the pre-flight
bucketing and prints the plan, but skips every `send` call.

## JSON-RPC

`imsg rpc` speaks JSON-RPC 2.0 over stdin/stdout, one JSON object per line. It
is intended for agents and long-running integrations that want a single process
for chats, history, send, and watch.

Read methods:

- `chats.list`
- `messages.history`
- `watch.subscribe`
- `watch.unsubscribe`

Mutating method:

- `send`

See [docs/rpc.md](docs/rpc.md) for request and response shapes.

## Attachments

`--attachments` reports metadata only. It does not copy or upload files.

Attachment metadata includes filename, transfer name, UTI, MIME type, byte
count, sticker flag, missing flag, and resolved original path.

`--convert-attachments` can expose cached, model-compatible receive-side
variants:

- CAF audio -> M4A
- GIF image -> first-frame PNG

Conversion requires `ffmpeg` on `PATH`. Original Messages attachments are left
unchanged. Converted metadata is reported with `converted_path` and
`converted_mime_type`.

`send --file` sends regular files, including audio files, through Messages.app.
Before handing the file to Messages, `imsg` stages it under
`~/Library/Messages/Attachments/imsg/` so Messages can read it reliably.

## Watch Behavior

`imsg watch` starts at the newest message by default and streams messages written
after it starts. Use `--since-rowid <id>` to resume from a stored cursor.

The watcher listens for filesystem events on `chat.db`, `chat.db-wal`, and
`chat.db-shm`, then backs that up with a lightweight poll. The poll keeps
streams alive when macOS drops file events or rotates SQLite sidecar files.

RPC watch defaults to a 500ms debounce to reduce outbound echo races. CLI watch
can be tuned with `--debounce`.

## Permissions Troubleshooting

If reads fail with `unable to open database file`, empty output, or
`authorization denied`:

1. Open System Settings -> Privacy & Security -> Full Disk Access.
2. Add the terminal or parent app that launches `imsg`.
3. If launched from an editor, Node process, gateway, or shell wrapper, grant
   Full Disk Access to that parent app too.
4. Also add the built-in Terminal.app at
   `/System/Applications/Utilities/Terminal.app`; macOS can still consult the
   default terminal grant.
5. Toggle stale Full Disk Access entries off and on after terminal, Homebrew,
   Node, or app updates.
6. Confirm Messages.app is signed in and `~/Library/Messages/chat.db` exists.

For sends and tapbacks, allow the terminal or parent app under Privacy &
Security -> Automation -> Messages.

`imsg` opens `chat.db` read-only. It does not use SQLite `immutable=1` by
default because immutable reads can miss WAL-backed Messages updates.

## Advanced IMCore Features

Default `send`, `chats`, `history`, `watch`, and read-only `rpc` workflows do
not require IMCore injection.

Advanced features such as `read`, `typing`, `launch`, and IMCore bridge status are
opt-in. They require SIP to be disabled and a helper dylib to be injected into
Messages.app:

```bash
make build-dylib
imsg launch
imsg status
```

Important limits:

- `imsg launch` refuses to inject when SIP is enabled.
- `imsg status` is read-only and does not auto-launch or auto-inject.
- macOS 26/Tahoe can block injection through library validation.
- macOS 26/Tahoe can also reject direct IMCore clients through `imagent`
  private-entitlement checks.
- These limits affect advanced IMCore features such as typing indicators, not
  normal send/history/watch usage.

To revert after testing advanced features, re-enable SIP from Recovery mode with
`csrutil enable`.

## Development

```bash
make lint
make test
make build
```

`make test` applies the repository's SQLite.swift patch before running Swift
tests.

The reusable Swift core lives in `Sources/IMsgCore`; the CLI target lives in
`Sources/imsg`.
