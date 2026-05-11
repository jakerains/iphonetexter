---
name: imsg
description: Use for local iMessage/SMS archive reads, chat history, watch, and explicitly requested sends.
---

# imsg

Use this for Messages.app history, chat lookup, streaming, and sends. Reading is local DB access; sending uses Messages automation and must be explicitly requested.

## Sources

- DB: `~/Library/Messages/chat.db`
- Repo: `~/Projects/imsg`
- CLI: `imsg`
- JSON output is NDJSON; pipe to `jq -s` for arrays.

## Read Workflow

Check DB access:

```bash
sqlite3 ~/Library/Messages/chat.db 'pragma quick_check;'
```

List chats:

```bash
imsg chats --json | jq -s
```

Read a chat:

```bash
imsg history --chat-id ID --json | jq -s
```

Use `--attachments` when attachment metadata matters. Use `--start`/`--end` with absolute timestamps for date-scoped questions.

## Sends

Only send, react, mark read, or show typing when the user explicitly asks. Prefer dry wording in the final confirmation: recipient, service, and what was sent.

Common send command:

```bash
imsg send --to "+15551234567" --text "message" --service auto
```

Normalize a free-form handle before lookup or send:

```bash
imsg normalize --to "(415) 555-1212" --json
```

## Bulk Send

For same-body broadcasts to many recipients, use the Python wrapper instead of looping `imsg send`. It buckets recipients by service from `chat.db`, paces iMessage and SMS separately, and persists results to a SQLite store with resume support.

```bash
python scripts/bulk_send.py --recipients recipients.csv --message "text" --confirm
```

Without `--confirm` the run is a dry-run only. A localhost web UI at `scripts/web_ui/` adds persistent contact lists, an inbound watcher, automatic opt-out flagging on replies like "stop", and one-off send.

## Verification

For repo edits:

```bash
make test
make build
```

For live read proof:

```bash
imsg chats --limit 3 --json | jq -s
```
