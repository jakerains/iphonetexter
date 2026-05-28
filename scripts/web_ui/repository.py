"""Typed wrappers over the SQLite database.

Everything that talks to the DB outside of `db.py` goes through these
classes — no inline SQL in route handlers or the watcher. Each repository
opens its own connection per call so we don't have to thread connection
state through the HTTP request lifecycle.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from db import Database


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Contact:
    id: int
    normalized_handle: str
    kind: str
    region: Optional[str]
    display_name: Optional[str]
    last_known_service: Optional[str]
    opted_out: bool
    opted_out_at: Optional[str]
    opted_out_reason: Optional[str]
    created_at: str
    updated_at: str
    notes: Optional[str] = None
    # Populated only by list_all() — comma-joined names of the lists this
    # contact belongs to. Not a persisted column.
    list_names: Optional[str] = None


@dataclass
class List:
    id: int
    name: str
    kind: str
    notes: Optional[str]
    created_at: str
    member_count: int = 0


@dataclass
class SendRow:
    id: int
    contact_id: Optional[int]
    list_id: Optional[int]
    job_id: Optional[str]
    chat_id: Optional[int]
    target_type: str
    target: str
    service: Optional[str]
    region: Optional[str]
    message_body: str
    attachment_path: Optional[str]
    status: str
    message_rowid: Optional[int]
    guid: Optional[str]
    error: Optional[str]
    ts: str
    contact_handle: Optional[str] = None
    contact_display_name: Optional[str] = None
    list_name: Optional[str] = None


@dataclass
class ReceivedRow:
    id: int
    guid: str
    chat_id: Optional[int]
    contact_id: Optional[int]
    sender_handle: Optional[str]
    text: str
    is_reaction: bool
    received_at: Optional[str]
    ingested_at: str
    message_rowid: Optional[int]
    contact_display_name: Optional[str] = None
    triggered_optout: bool = False


@dataclass
class OptOutRow:
    id: int
    contact_id: int
    received_id: Optional[int]
    matched_phrase: Optional[str]
    processed_at: str
    handle: Optional[str] = None
    display_name: Optional[str] = None


def _row_to_contact(row: sqlite3.Row) -> Contact:
    keys = row.keys()
    return Contact(
        id=row["id"],
        normalized_handle=row["normalized_handle"],
        kind=row["kind"],
        region=row["region"],
        display_name=row["display_name"],
        last_known_service=row["last_known_service"],
        opted_out=bool(row["opted_out"]),
        opted_out_at=row["opted_out_at"],
        opted_out_reason=row["opted_out_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        notes=row["notes"] if "notes" in keys else None,
        list_names=row["list_names"] if "list_names" in keys else None,
    )


class Contacts:
    def __init__(self, db: Database):
        self.db = db

    def upsert(
        self,
        normalized_handle: str,
        kind: str = "unknown",
        region: Optional[str] = None,
        display_name: Optional[str] = None,
        service: Optional[str] = None,
    ) -> Contact:
        now = _now()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM contacts WHERE normalized_handle = ?",
                (normalized_handle,),
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    """
                    INSERT INTO contacts (
                        normalized_handle, kind, region, display_name,
                        last_known_service, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (normalized_handle, kind, region, display_name, service, now, now),
                )
                row = conn.execute(
                    "SELECT * FROM contacts WHERE id = ?", (cur.lastrowid,)
                ).fetchone()
            else:
                conn.execute(
                    """
                    UPDATE contacts
                       SET kind = COALESCE(NULLIF(?, 'unknown'), kind),
                           region = COALESCE(?, region),
                           display_name = COALESCE(?, display_name),
                           last_known_service = COALESCE(?, last_known_service),
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (kind, region, display_name, service, now, existing["id"]),
                )
                row = conn.execute(
                    "SELECT * FROM contacts WHERE id = ?", (existing["id"],)
                ).fetchone()
        return _row_to_contact(row)

    def get(self, contact_id: int) -> Optional[Contact]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
        return _row_to_contact(row) if row else None

    def get_by_handle(self, normalized_handle: str) -> Optional[Contact]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM contacts WHERE normalized_handle = ?",
                (normalized_handle,),
            ).fetchone()
        return _row_to_contact(row) if row else None

    def is_opted_out(self, normalized_handle: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT opted_out FROM contacts WHERE normalized_handle = ?",
                (normalized_handle,),
            ).fetchone()
        return bool(row and row["opted_out"])

    def mark_opted_out(self, contact_id: int, reason: Optional[str]) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE contacts
                   SET opted_out = 1,
                       opted_out_at = ?,
                       opted_out_reason = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (_now(), reason, _now(), contact_id),
            )

    def clear_optout(self, contact_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE contacts
                   SET opted_out = 0,
                       opted_out_at = NULL,
                       opted_out_reason = NULL,
                       updated_at = ?
                 WHERE id = ?
                """,
                (_now(), contact_id),
            )

    def list_opted_out(self) -> list[Contact]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM contacts WHERE opted_out = 1 ORDER BY opted_out_at DESC"
            ).fetchall()
        return [_row_to_contact(r) for r in rows]

    @staticmethod
    def _filter_clause(
        search: Optional[str],
        kind: Optional[str],
        status: Optional[str],
        list_id: Optional[int],
    ) -> tuple[str, list]:
        """Build a shared WHERE clause + params for list_all / count_all."""
        clauses: list[str] = []
        params: list = []
        if search:
            like = f"%{search.strip()}%"
            clauses.append(
                "(c.normalized_handle LIKE ? OR c.display_name LIKE ? OR c.notes LIKE ?)"
            )
            params += [like, like, like]
        if kind:
            clauses.append("c.kind = ?")
            params.append(kind)
        if status == "opted_out":
            clauses.append("c.opted_out = 1")
        elif status == "active":
            clauses.append("c.opted_out = 0")
        if list_id is not None:
            clauses.append(
                "c.id IN (SELECT contact_id FROM list_members WHERE list_id = ?)"
            )
            params.append(list_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def count_all(
        self,
        *,
        search: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        list_id: Optional[int] = None,
    ) -> int:
        where, params = self._filter_clause(search, kind, status, list_id)
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM contacts c{where}", params
            ).fetchone()
        return int(row["n"])

    def list_all(
        self,
        *,
        search: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        list_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Contact]:
        """Filtered, paginated contacts, each annotated with its list names."""
        where, params = self._filter_clause(search, kind, status, list_id)
        sql = f"""
            SELECT c.*,
                   (SELECT group_concat(l.name, ', ')
                      FROM list_members lm
                      JOIN lists l ON l.id = lm.list_id
                     WHERE lm.contact_id = c.id) AS list_names
              FROM contacts c{where}
             ORDER BY (c.display_name IS NULL OR c.display_name = ''),
                      c.display_name COLLATE NOCASE,
                      c.normalized_handle
             LIMIT ? OFFSET ?
        """
        with self.db.connect() as conn:
            rows = conn.execute(sql, [*params, limit, offset]).fetchall()
        return [_row_to_contact(r) for r in rows]

    def bulk_mark_opted_out(self, contact_ids: Iterable[int], reason: Optional[str]) -> int:
        now = _now()
        changed = 0
        with self.db.transaction() as conn:
            for cid in contact_ids:
                cur = conn.execute(
                    """
                    UPDATE contacts
                       SET opted_out = 1, opted_out_at = ?, opted_out_reason = ?,
                           updated_at = ?
                     WHERE id = ? AND opted_out = 0
                    """,
                    (now, reason, now, cid),
                )
                changed += cur.rowcount
        return changed

    def bulk_clear_optout(self, contact_ids: Iterable[int]) -> int:
        now = _now()
        changed = 0
        with self.db.transaction() as conn:
            for cid in contact_ids:
                cur = conn.execute(
                    """
                    UPDATE contacts
                       SET opted_out = 0, opted_out_at = NULL, opted_out_reason = NULL,
                           updated_at = ?
                     WHERE id = ? AND opted_out = 1
                    """,
                    (now, cid),
                )
                changed += cur.rowcount
        return changed

    def update_details(
        self,
        contact_id: int,
        *,
        display_name: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Set name/notes (used by CSV enrichment and inline edits).

        Only non-None arguments overwrite; pass "" to clear a field.
        """
        sets: list[str] = []
        params: list = []
        if display_name is not None:
            sets.append("display_name = ?")
            params.append(display_name or None)
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes or None)
        if not sets:
            return
        sets.append("updated_at = ?")
        params += [_now(), contact_id]
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?", params
            )


class Lists:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> list[List]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT l.*, COUNT(lm.contact_id) AS member_count
                  FROM lists l
                  LEFT JOIN list_members lm ON lm.list_id = l.id
                 GROUP BY l.id
                 ORDER BY l.created_at DESC
                """
            ).fetchall()
        return [
            List(
                id=r["id"],
                name=r["name"],
                kind=r["kind"],
                notes=r["notes"],
                created_at=r["created_at"],
                member_count=r["member_count"],
            )
            for r in rows
        ]

    def named(self) -> list[List]:
        return [lst for lst in self.all() if lst.kind == "named"]

    def get(self, list_id: int) -> Optional[List]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT l.*, COUNT(lm.contact_id) AS member_count
                  FROM lists l
                  LEFT JOIN list_members lm ON lm.list_id = l.id
                 WHERE l.id = ?
                 GROUP BY l.id
                """,
                (list_id,),
            ).fetchone()
        if row is None:
            return None
        return List(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            notes=row["notes"],
            created_at=row["created_at"],
            member_count=row["member_count"],
        )

    def create(self, name: str, kind: str = "named", notes: Optional[str] = None) -> List:
        clean = name.strip()
        if not clean:
            raise ValueError("List name is required.")
        if kind not in {"named", "adhoc"}:
            raise ValueError(f"Invalid list kind: {kind}")
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM lists WHERE name = ?", (clean,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"A list named {clean!r} already exists.")
            cur = conn.execute(
                "INSERT INTO lists (name, kind, notes, created_at) VALUES (?, ?, ?, ?)",
                (clean, kind, notes, _now()),
            )
            list_id = cur.lastrowid
        return self.get(list_id)  # type: ignore[return-value]

    def create_adhoc(self) -> List:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return self.create(name=f"adhoc-{ts}", kind="adhoc")

    def delete(self, list_id: int) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
            return cur.rowcount > 0

    def add_members(self, list_id: int, contact_ids: Iterable[int]) -> int:
        added = 0
        now = _now()
        with self.db.transaction() as conn:
            for cid in contact_ids:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO list_members (list_id, contact_id, added_at)
                    VALUES (?, ?, ?)
                    """,
                    (list_id, cid, now),
                )
                added += cur.rowcount
        return added

    def remove_member(self, list_id: int, contact_id: int) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM list_members WHERE list_id = ? AND contact_id = ?",
                (list_id, contact_id),
            )
        return cur.rowcount > 0

    def members(self, list_id: int) -> list[Contact]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*
                  FROM list_members lm
                  JOIN contacts c ON c.id = lm.contact_id
                 WHERE lm.list_id = ?
                 ORDER BY lm.added_at
                """,
                (list_id,),
            ).fetchall()
        return [_row_to_contact(r) for r in rows]

    def members_active(self, list_id: int) -> list[Contact]:
        return [m for m in self.members(list_id) if not m.opted_out]


class Sends:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        *,
        contact_id: Optional[int],
        list_id: Optional[int],
        job_id: Optional[str],
        chat_id: Optional[int],
        target_type: str,
        target: str,
        service: Optional[str],
        region: Optional[str],
        message_body: str,
        attachment_path: Optional[str],
        status: str,
        message_rowid: Optional[int] = None,
        guid: Optional[str] = None,
        error: Optional[str] = None,
        ts: Optional[str] = None,
    ) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO sends (
                    contact_id, list_id, job_id, chat_id, target_type, target,
                    service, region, message_body, attachment_path, status,
                    message_rowid, guid, error, ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contact_id, list_id, job_id, chat_id, target_type, target,
                    service, region, message_body, attachment_path, status,
                    message_rowid, guid, error, ts or _now(),
                ),
            )
            return cur.lastrowid

    def is_already_done(self, list_id: int, contact_id: int) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM sends
                 WHERE list_id = ? AND contact_id = ?
                   AND status IN ('ok', 'ok_unverified')
                 LIMIT 1
                """,
                (list_id, contact_id),
            ).fetchone()
        return row is not None

    def recent_oneoffs(self, limit: int = 50) -> list[SendRow]:
        return self._recent(limit=limit, where="WHERE list_id IS NULL")

    def recent(self, limit: int = 200) -> list[SendRow]:
        return self._recent(limit=limit, where="")

    def for_job(self, job_id: str) -> list[SendRow]:
        return self._recent(limit=10000, where="WHERE job_id = ?", params=(job_id,))

    def jobs_summary(self) -> list[dict]:
        """Group sends by job_id for the /results bulk-runs section."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.job_id, l.id AS list_id, l.name AS list_name, l.kind AS list_kind,
                       MIN(s.ts) AS started_at, MAX(s.ts) AS finished_at,
                       COUNT(*) AS total,
                       SUM(CASE WHEN s.status IN ('ok','ok_unverified') THEN 1 ELSE 0 END) AS ok_count,
                       SUM(CASE WHEN s.status NOT IN ('ok','ok_unverified') THEN 1 ELSE 0 END) AS error_count
                  FROM sends s
                  LEFT JOIN lists l ON l.id = s.list_id
                 WHERE s.job_id IS NOT NULL
                 GROUP BY s.job_id
                 ORDER BY started_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def _recent(self, *, limit: int, where: str, params: tuple = ()) -> list[SendRow]:
        sql = f"""
            SELECT s.*, c.normalized_handle AS contact_handle,
                   c.display_name AS contact_display_name,
                   l.name AS list_name
              FROM sends s
              LEFT JOIN contacts c ON c.id = s.contact_id
              LEFT JOIN lists l ON l.id = s.list_id
              {where}
             ORDER BY s.ts DESC
             LIMIT ?
        """
        with self.db.connect() as conn:
            rows = conn.execute(sql, (*params, limit)).fetchall()
        return [
            SendRow(
                id=r["id"], contact_id=r["contact_id"], list_id=r["list_id"],
                job_id=r["job_id"], chat_id=r["chat_id"], target_type=r["target_type"],
                target=r["target"], service=r["service"], region=r["region"],
                message_body=r["message_body"], attachment_path=r["attachment_path"],
                status=r["status"], message_rowid=r["message_rowid"], guid=r["guid"],
                error=r["error"], ts=r["ts"], contact_handle=r["contact_handle"],
                contact_display_name=r["contact_display_name"], list_name=r["list_name"],
            )
            for r in rows
        ]


class Received:
    def __init__(self, db: Database):
        self.db = db

    def insert_idempotent(
        self,
        *,
        guid: str,
        chat_id: Optional[int],
        contact_id: Optional[int],
        sender_handle: Optional[str],
        text: str,
        is_reaction: bool,
        received_at: Optional[str],
        message_rowid: Optional[int],
    ) -> Optional[int]:
        """Insert one received row keyed on guid. Returns the new id, or
        None if a row with this guid already existed."""
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM received WHERE guid = ?", (guid,)
            ).fetchone()
            if existing is not None:
                return None
            cur = conn.execute(
                """
                INSERT INTO received (
                    guid, chat_id, contact_id, sender_handle, text,
                    is_reaction, received_at, ingested_at, message_rowid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guid, chat_id, contact_id, sender_handle, text,
                    1 if is_reaction else 0, received_at, _now(), message_rowid,
                ),
            )
            return cur.lastrowid

    def recent(self, limit: int = 100) -> list[ReceivedRow]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*, c.display_name AS contact_display_name,
                       EXISTS (
                         SELECT 1 FROM opt_outs o WHERE o.received_id = r.id
                       ) AS triggered_optout
                  FROM received r
                  LEFT JOIN contacts c ON c.id = r.contact_id
                 WHERE r.is_reaction = 0
                 ORDER BY r.ingested_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            ReceivedRow(
                id=r["id"], guid=r["guid"], chat_id=r["chat_id"],
                contact_id=r["contact_id"], sender_handle=r["sender_handle"],
                text=r["text"], is_reaction=bool(r["is_reaction"]),
                received_at=r["received_at"], ingested_at=r["ingested_at"],
                message_rowid=r["message_rowid"],
                contact_display_name=r["contact_display_name"],
                triggered_optout=bool(r["triggered_optout"]),
            )
            for r in rows
        ]


class OptOuts:
    def __init__(self, db: Database):
        self.db = db

    def record(self, contact_id: int, received_id: Optional[int], matched_phrase: str) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO opt_outs (contact_id, received_id, matched_phrase, processed_at)
                VALUES (?, ?, ?, ?)
                """,
                (contact_id, received_id, matched_phrase, _now()),
            )
            return cur.lastrowid

    def list_recent(self, limit: int = 100) -> list[OptOutRow]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT o.*, c.normalized_handle AS handle, c.display_name AS display_name
                  FROM opt_outs o
                  JOIN contacts c ON c.id = o.contact_id
                 ORDER BY o.processed_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            OptOutRow(
                id=r["id"], contact_id=r["contact_id"], received_id=r["received_id"],
                matched_phrase=r["matched_phrase"], processed_at=r["processed_at"],
                handle=r["handle"], display_name=r["display_name"],
            )
            for r in rows
        ]


class WatchState:
    def __init__(self, db: Database):
        self.db = db

    def get_cursor(self) -> Optional[int]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT last_message_rowid FROM watch_state WHERE id = 1"
            ).fetchone()
        return row["last_message_rowid"] if row and row["last_message_rowid"] is not None else None

    def set_cursor(self, rowid: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE watch_state
                   SET last_message_rowid = MAX(COALESCE(last_message_rowid, 0), ?),
                       last_seen_at = ?
                 WHERE id = 1
                """,
                (rowid, _now()),
            )


class Repo:
    """Convenience aggregate so server.py can hold one object."""

    def __init__(self, db: Database):
        self.db = db
        self.contacts = Contacts(db)
        self.lists = Lists(db)
        self.sends = Sends(db)
        self.received = Received(db)
        self.optouts = OptOuts(db)
        self.watch_state = WatchState(db)
