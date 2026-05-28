#!/usr/bin/env python3
"""Import every handle from the macOS Messages database into the app's contacts.

Reads ``~/Library/Messages/chat.db`` (read-only) and upserts each distinct
phone/email handle into the web UI's ``contacts`` table. Idempotent: existing
contacts are matched by normalized handle and left intact, so it's safe to
re-run as your Messages history grows.

Usage:
    python scripts/web_ui/import_from_messages.py [--region US] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Make sibling modules (bulk_send, db, repository) importable.
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR.parent))

from bulk_send import normalize_handle  # noqa: E402
from db import Database  # noqa: E402
from repository import Repo  # noqa: E402

DB_PATH = BASE_DIR / "state" / "imsg.db"
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
IMSG_BIN = os.environ.get("IMSG_BIN", "imsg")


def fetch_handles() -> list[str]:
    """Return every distinct handle string from the Messages database."""
    if not CHAT_DB.exists():
        sys.exit(f"Messages database not found at {CHAT_DB}")
    uri = f"file:{CHAT_DB}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT id FROM handle WHERE id IS NOT NULL AND id != '' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def classify(handle: str, region: str) -> tuple[str, str]:
    """Return (normalized_handle, kind) for a raw Messages handle.

    Phones (E.164) and emails are recognized directly; anything else (e.g.
    SMS short codes) is run through ``imsg normalize`` for a verdict.
    """
    if "@" in handle:
        return handle.strip().lower(), "email"
    if handle.startswith("+") and handle[1:].isdigit():
        return handle, "phone"
    normalized, valid, kind = normalize_handle(IMSG_BIN, handle, region)
    return normalized, (kind if valid else "unknown")


def import_contacts(repo: Repo, region: str = "US", dry_run: bool = False) -> dict:
    """Import every Messages handle into ``repo``'s contacts table.

    Idempotent: existing contacts are matched by normalized handle. Returns a
    summary dict (found / created / updated / skipped / by_kind).
    """
    handles = fetch_handles()
    created = updated = skipped = 0
    by_kind: dict[str, int] = {"phone": 0, "email": 0, "unknown": 0}

    for raw in handles:
        normalized, kind = classify(raw, region)
        if not normalized:
            skipped += 1
            continue
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if dry_run:
            continue
        before = repo.contacts.get_by_handle(normalized)
        repo.contacts.upsert(normalized_handle=normalized, kind=kind)
        if before is None:
            created += 1
        else:
            updated += 1

    return {
        "found": len(handles),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "by_kind": by_kind,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="US", help="Default region for phone normalization.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported, write nothing.")
    args = parser.parse_args()

    repo = Repo(Database(DB_PATH))
    summary = import_contacts(repo, region=args.region, dry_run=args.dry_run)

    print(f"Found {summary['found']} distinct handle(s) in Messages.")
    print(f"By kind: {summary['by_kind']}")
    if args.dry_run:
        print(f"Dry run — nothing written. {summary['skipped']} skipped.")
    else:
        print(f"Imported into {DB_PATH}")
        print(f"  new contacts:     {summary['created']}")
        print(f"  already existed:  {summary['updated']}")
        print(f"  skipped:          {summary['skipped']}")


if __name__ == "__main__":
    main()
