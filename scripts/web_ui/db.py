"""SQLite connection + schema bootstrap for the imsg web UI.

The DB lives at `scripts/web_ui/state/imsg.db` and is shared by every
component: the FastAPI request handlers, the bulk-send hooks, and the
inbound watcher. WAL mode lets the watcher write concurrently with HTTP
reads.

Schema versioning is intentionally simple: a single `schema_meta.version`
row. Future migrations append new SQL inside `_migrate()` keyed by the
target version.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

CURRENT_VERSION = 2
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._bootstrap()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we manage transactions ourselves
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _bootstrap(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock, self._connect() as conn:
            conn.executescript(sql)
            row = conn.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (CURRENT_VERSION,))
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT version FROM schema_meta").fetchone()
        version = row["version"] if row else 0

        # v2: free-text `notes` on contacts (for CSV-imported names/info).
        if version < 2:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(contacts)")}
            if "notes" not in cols:
                conn.execute("ALTER TABLE contacts ADD COLUMN notes TEXT")

        if version != CURRENT_VERSION:
            conn.execute("UPDATE schema_meta SET version = ?", (CURRENT_VERSION,))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Borrow a connection. The caller is responsible for transactions
        (use `with conn:` for a BEGIN/COMMIT/ROLLBACK envelope) but the
        connection itself stays open for reuse across statements."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Convenience for simple write paths: opens a connection,
        begins a transaction, commits on success, rolls back on exception."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
