#!/usr/bin/env python3
"""Scan existing Messages history for opt-out replies and flag those contacts.

The live inbound watcher only sees messages that arrive *after* it starts, so
anyone who replied "STOP" / "unsubscribe" / etc. *before* you imported them
would never be flagged. This backfill closes that gap: it walks every chat via
the `imsg` RPC (`chats.list` + `messages.history`, which decode message text
including `attributedBody` blobs), runs each inbound reply through the same
``OptOutMatcher`` the watcher uses, and marks any match as opted out.

Idempotent: contacts already flagged are left alone. Safe to re-run.

Usage:
    python scripts/web_ui/scan_optouts.py [--chat-limit N] [--per-chat-limit N]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Optional

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR.parent))

from db import Database  # noqa: E402
from import_from_messages import classify  # noqa: E402
from optout import OptOutMatcher, load_matcher  # noqa: E402
from repository import Repo  # noqa: E402

DB_PATH = BASE_DIR / "state" / "imsg.db"
OPTOUT_PHRASES_PATH = BASE_DIR / "state" / "optout_phrases.txt"

# Defaults sized to be thorough without runaway cost on huge histories.
DEFAULT_CHAT_LIMIT = 1000
DEFAULT_PER_CHAT_LIMIT = 2000


def scan_history(
    call: Callable[[str, Optional[dict]], dict],
    repo: Repo,
    matcher: OptOutMatcher,
    *,
    region: str = "US",
    chat_limit: int = DEFAULT_CHAT_LIMIT,
    per_chat_limit: int = DEFAULT_PER_CHAT_LIMIT,
) -> dict:
    """Scan all chats for opt-out replies and flag matching contacts.

    ``call`` is a synchronous ``call(method, params) -> dict`` against the imsg
    RPC. Returns a summary dict.
    """
    chats = (call("chats.list", {"limit": chat_limit}) or {}).get("chats", [])
    chats_scanned = 0
    messages_scanned = 0
    flagged = 0
    already_flagged = 0
    matched_handles: list[str] = []
    seen_contacts: set[int] = set()

    for chat in chats:
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        chats_scanned += 1
        result = call("messages.history", {"chat_id": chat_id, "limit": per_chat_limit}) or {}
        for msg in result.get("messages", []):
            if msg.get("is_from_me") or msg.get("is_reaction"):
                continue
            text = msg.get("text") or ""
            sender = msg.get("sender") or ""
            if not text or not sender:
                continue
            messages_scanned += 1
            match = matcher.scan(text)
            if not match:
                continue
            normalized, kind = classify(sender, region)
            if not normalized:
                continue
            contact = repo.contacts.upsert(normalized_handle=normalized, kind=kind)
            if contact.id in seen_contacts:
                continue
            seen_contacts.add(contact.id)
            if contact.opted_out:
                already_flagged += 1
                continue
            repo.contacts.mark_opted_out(contact.id, match)
            flagged += 1
            matched_handles.append(normalized)

    return {
        "chats_scanned": chats_scanned,
        "messages_scanned": messages_scanned,
        "flagged": flagged,
        "already_flagged": already_flagged,
        "handles": matched_handles,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="US", help="Default region for phone normalization.")
    parser.add_argument("--chat-limit", type=int, default=DEFAULT_CHAT_LIMIT,
                        help="Max chats to scan.")
    parser.add_argument("--per-chat-limit", type=int, default=DEFAULT_PER_CHAT_LIMIT,
                        help="Max messages to read per chat.")
    args = parser.parse_args()

    # Standalone: spawn our own short-lived `imsg rpc` subprocess.
    from bulk_send import RpcClient  # noqa: E402

    binary = os.environ.get("IMSG_BIN", "imsg")

    repo = Repo(Database(DB_PATH))
    matcher = load_matcher(OPTOUT_PHRASES_PATH)

    with RpcClient(binary) as rpc:
        summary = scan_history(
            rpc.call,
            repo,
            matcher,
            region=args.region,
            chat_limit=args.chat_limit,
            per_chat_limit=args.per_chat_limit,
        )

    print(f"Scanned {summary['messages_scanned']} inbound message(s) across "
          f"{summary['chats_scanned']} chat(s).")
    print(f"  newly flagged opt-outs: {summary['flagged']}")
    print(f"  already flagged:        {summary['already_flagged']}")
    for handle in summary["handles"]:
        print(f"    flagged {handle}")


if __name__ == "__main__":
    main()
