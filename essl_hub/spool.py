"""Local punch spool for the collector (KTD9).

A plain stdlib ``sqlite3`` file with no sync. Any failed punch write lands here
as one batch and is replayed oldest-first on later cycles. Punch inserts are
fingerprint-keyed, so replaying a half-finished batch is safe. A batch that
fails ``MAX_ATTEMPTS`` replays while Turso is healthy moves to ``dead``: kept,
never dropped, and visible in the collector status file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime

MAX_ATTEMPTS = 20

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS spool (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        punches TEXT NOT NULL,
        created_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dead (
        id INTEGER PRIMARY KEY,
        device_id TEXT NOT NULL,
        punches TEXT NOT NULL,
        created_at TEXT NOT NULL,
        attempts INTEGER NOT NULL,
        last_error TEXT,
        dead_at TEXT NOT NULL
    )
    """,
)


@dataclass(frozen=True)
class SpoolEntry:
    id: int
    device_id: str
    punches: list[dict]
    created_at: str
    attempts: int


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


class Spool:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        for statement in _SCHEMA:
            self._conn.execute(statement)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append(self, device_id: str, punches: list[dict]) -> int:
        payload = json.dumps(list(punches), ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO spool (device_id, punches, created_at) VALUES (?, ?, ?)",
                (device_id, payload, _now()),
            )
            return cursor.lastrowid

    def pending(self) -> list[SpoolEntry]:
        """Spooled batches, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, device_id, punches, created_at, attempts FROM spool ORDER BY id"
            ).fetchall()
        return [SpoolEntry(r[0], r[1], json.loads(r[2]), r[3], r[4]) for r in rows]

    def ack(self, entry_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM spool WHERE id=?", (entry_id,))

    def fail(self, entry_id: int, error: str) -> bool:
        """Count one failed replay; True when the batch moved to ``dead``."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE spool SET attempts=attempts+1, last_error=? WHERE id=?",
                    (error[:500], entry_id),
                )
                moved = self._conn.execute(
                    "INSERT INTO dead (id, device_id, punches, created_at, attempts, last_error, dead_at) "
                    "SELECT id, device_id, punches, created_at, attempts, last_error, ? "
                    "FROM spool WHERE id=? AND attempts>=?",
                    (_now(), entry_id, MAX_ATTEMPTS),
                ).rowcount
                if moved:
                    self._conn.execute("DELETE FROM spool WHERE id=?", (entry_id,))
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return bool(moved)

    def depth(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0]

    def oldest_at(self) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT MIN(created_at) FROM spool").fetchone()
        return row[0]

    def dead_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM dead").fetchone()[0]
