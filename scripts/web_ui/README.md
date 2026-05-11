# imsg local web UI

Local browser UI for `imsg`. Single-user, localhost-only by default.
SQLite under the hood for contacts, lists, sends, received messages, and
opt-outs. A long-lived watcher subprocess captures every inbound message
and flags opt-outs automatically.

## Setup

```bash
pip install -r scripts/web_ui/requirements.txt
```

`imsg` must be on `PATH` (or set `IMSG_BIN` to its full path) and the
terminal must have Full Disk Access + Automation permission for
Messages.app — same prereqs as the bare CLI. The Full Disk Access grant
is also what lets the watcher see inbound messages.

## Run

```bash
python scripts/web_ui/server.py
```

Open `http://127.0.0.1:8765/`.

## Environment overrides

- `IMSG_BIN` — path to the `imsg` binary (default `imsg`).
- `IMSG_WEB_HOST` — bind address (default `127.0.0.1`; do not change unless
  you understand the risks of exposing this UI on a network).
- `IMSG_WEB_PORT` — port (default `8765`).

## Pages

- `/` Compose — start a bulk job. Pick an existing list or upload a CSV
  (CSV silently creates an ad-hoc list). The "Send for real" checkbox is
  unchecked by default — without it the run is dry-run only.
- `/send` — one-off send. Recipient can be a phone, email, contact name,
  or an existing chat id. Refused with a banner while a bulk job is
  active. Refuses to send to opted-out contacts.
- `/jobs/<id>` — live job page with SSE progress and a cancel button.
- `/chats` — recent chats from `chat.db`. Opt-out rows are highlighted
  red. Each row links to `/send?chat_id=<id>` for a one-click reply.
- `/chats/<chat_id>` — message history for one chat.
- `/lists` — manage named contact lists. Create new lists, browse members,
  remove members, delete lists.
- `/lists/<id>` — list detail. Import a CSV to add contacts (existing
  contacts get linked, new ones get created). Send to the list via the
  Compose page.
- `/inbound` — recent received messages captured by the watcher (last
  100). Reaction tapbacks are excluded. Rows that triggered an opt-out
  are highlighted red.
- `/optouts` — opted-out contacts plus a log of recent opt-out events
  with the matched phrase. Per-contact "clear" button to undo.
- `/templates` — saved message bodies; load into Compose / Send forms.
- `/results` — recent one-off sends and grouped bulk runs (last 50
  one-offs from the `sends` table; bulk runs grouped by `job_id`). Click
  a job id to drill into its per-recipient rows.

## Inbound watcher

A background asyncio task spawns `imsg watch --json --since-rowid <last>`
on server startup and consumes its NDJSON stdout. For each message:

1. Persist to `received` (idempotent on `guid`).
2. Skip if it's outbound (`is_from_me`) or a reaction tapback.
3. Look up / upsert the sender as a contact (handle normalized through
   `imsg normalize`).
4. Scan the text against the opt-out phrase list.
5. On match: insert an `opt_outs` row and flip the contact's
   `opted_out = 1`. Future bulk and one-off sends auto-skip them.

`watch_state.last_message_rowid` is persisted after every message so a
restart resumes from the right place. The watcher reconnects with
exponential backoff (1s → 30s) if `imsg watch` exits.

## Opt-out phrases

Defaults baked into `optout.py`: `stop`, `stop sending`, `stop messaging`,
`unsubscribe`, `remove me`, `take me off`, `take me off your list`,
`opt out`, `opt-out`, `no more`, `quit`, `cancel`, `do not text`,
`don't text me`. Whole-token regex match, case-insensitive, multi-word
phrases collapse internal whitespace.

To override, drop a `state/optout_phrases.txt` file with one phrase per
line (lines starting with `#` are comments). The override **replaces**
the defaults entirely — be deliberate about what you include. Restart
the server to reload.

## Persistent state

- `scripts/web_ui/state/imsg.db` — SQLite database (contacts, lists,
  sends, received, opt-outs, watch cursor). WAL mode is enabled so the
  watcher's writes don't block reads.
- `scripts/web_ui/state/templates.json` — saved message templates.
- `scripts/web_ui/state/optout_phrases.txt` — optional phrase override.
- `scripts/web_ui/state/oneoff/` — files uploaded as one-off attachments
  (kept after send for retry / debugging).
- `scripts/web_ui/state/jobs/<timestamp>/` — per-job artifacts:
  `recipients.csv` (the input the engine read), `message.txt`. Send
  results live in the SQLite `sends` table.

The whole `state/` tree is gitignored.

## Concurrency model

- One bulk job at a time. The Send page refuses one-offs while a job is
  active, because both end up driving Messages.app via AppleScript and
  `MessageSender.send()` has no internal lock.
- Browse pages (chats / history) and the watcher's writes happen
  concurrently with everything else; SQLite WAL mode handles the
  read/write overlap.
- The watcher's normalization + DB writes run on a worker thread so they
  never block the FastAPI event loop.

## CLI bulk-send (standalone)

`scripts/bulk_send.py` can be run directly. It writes into the same
SQLite store the web UI uses (default DB path is
`<recipients_dir>/imsg.db`; override with `--db`):

```bash
python scripts/bulk_send.py \
  --recipients recipients.csv \
  --message "see you Saturday" \
  --db scripts/web_ui/state/imsg.db \
  --confirm
```

Without `--confirm` the run is dry-run only — does the bucketing
preflight and prints the plan, but skips every `send` call.
