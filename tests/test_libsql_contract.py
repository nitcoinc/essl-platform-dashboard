"""libsql contract spike (plan U8, docs/plans/2026-10-10-001-...).

Pins the libsql behaviour the local database relies on, so a libsql upgrade
or swap fails here instead of in production.

Observed with libsql 0.1.11 (bundled SQLite 3.42.0, paramstyle ``qmark``),
Linux x86_64, 2026-10-10:

Rows / types (KTD5)
- Rows are plain ``tuple``s. No ``row_factory``/``detect_types``.
- ``cursor.description`` is a 7-tuple per column; ``[0]`` is the column
  name, the other six are ``None`` (no declared type exposed).
- TIMESTAMP columns come back as the stored ``str``; JSON as ``str``.
- ``cursor.lastrowid`` and ``cursor.rowcount`` work for INSERT/UPDATE.
- ``executemany`` exists on Connection and Cursor; ``executescript`` too.

Transactions (KTD7)
- Default ``isolation_level`` is ``'DEFERRED'``: a DML statement opens an
  implicit transaction (``in_transaction`` True) until commit/rollback.
- ``isolation_level`` is read-only after construction (AttributeError);
  pass ``isolation_level=None`` to ``connect()`` for autocommit.
- With ``isolation_level=None``: statements autocommit; explicit ``BEGIN``
  and ``BEGIN IMMEDIATE`` work; ``conn.commit()`` commits an explicit BEGIN;
  ``ROLLBACK`` statement undoes it.
- ``PRAGMA user_version`` set/get works locally.

Errors (KTD6)
- UNIQUE / FOREIGN KEY (with ``PRAGMA foreign_keys=ON``) / CHECK violations
  raise builtin ``ValueError`` -- NOT ``libsql.Error`` (which exists but is
  not raised). Messages are SQLite's text:
  ``'UNIQUE constraint failed: p.name'``, ``'FOREIGN KEY constraint failed'``,
  ``'CHECK constraint failed: n>0'``. Classification must be by message
  (``'constraint failed'``), not by type.
- "database is locked" is also ``ValueError``; it maps to BusyError.

Backup (KTD14)
- After a libsql connection writes and closes the file, stdlib
  ``sqlite3.Connection.backup`` reads it and ``integrity_check`` is ``ok``.

Threads (KTD1)
- One Connection used from several threads works (no thread-affinity
  error despite the ``_check_same_thread`` kwarg). Callers must still
  serialise with a lock.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest

import libsql

class LocalContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "c.db")
        self.conn = libsql.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute(
            "CREATE TABLE p (id INTEGER PRIMARY KEY, name TEXT UNIQUE,"
            " n INT CHECK (n > 0), ts TIMESTAMP, j TEXT)"
        )
        self.conn.execute("CREATE TABLE ch (id INTEGER PRIMARY KEY, pid INT REFERENCES p(id))")
        self.conn.commit()

    def test_row_shape_and_description(self) -> None:
        cur = self.conn.execute(
            "INSERT INTO p (name, n, ts, j) VALUES (?, ?, ?, ?)",
            ("a", 1, "2026-01-01 10:00:00", '{"x": 1}'),
        )
        self.assertEqual(cur.lastrowid, 1)
        self.assertEqual(cur.rowcount, 1)
        self.conn.commit()
        cur = self.conn.execute("SELECT id, name, n, ts, j FROM p")
        row = cur.fetchone()
        self.assertIsInstance(row, tuple)
        self.assertEqual(row, (1, "a", 1, "2026-01-01 10:00:00", '{"x": 1}'))
        self.assertEqual([d[0] for d in cur.description], ["id", "name", "n", "ts", "j"])
        self.assertTrue(all(len(d) == 7 for d in cur.description))
        self.assertFalse(hasattr(self.conn, "row_factory"))

    def test_executemany_and_update_rowcount(self) -> None:
        cur = self.conn.cursor()
        cur.executemany("INSERT INTO p (name, n) VALUES (?, ?)", [("x", 1), ("y", 2)])
        self.conn.commit()
        cur = self.conn.execute("UPDATE p SET n = n + 1 WHERE n > 0")
        self.assertEqual(cur.rowcount, 2)
        cur = self.conn.execute("UPDATE p SET n = 5 WHERE name = 'nope'")
        self.assertEqual(cur.rowcount, 0)
        self.conn.commit()

    def test_default_isolation_is_implicit_deferred(self) -> None:
        self.assertEqual(self.conn.isolation_level, "DEFERRED")
        self.conn.execute("INSERT INTO p (name, n) VALUES ('a', 1)")
        self.assertTrue(self.conn.in_transaction)
        other = libsql.connect(self.path)
        self.addCleanup(other.close)
        self.assertEqual(other.execute("SELECT count(*) FROM p").fetchone(), (0,))
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM p").fetchone(), (0,))
        with self.assertRaises(AttributeError):
            self.conn.isolation_level = None  # type: ignore[misc]

    def test_autocommit_with_explicit_begin(self) -> None:
        conn = libsql.connect(self.path, isolation_level=None)
        self.addCleanup(conn.close)
        other = libsql.connect(self.path)
        self.addCleanup(other.close)
        self.assertIsNone(conn.isolation_level)
        conn.execute("INSERT INTO p (name, n) VALUES ('a', 1)")
        self.assertFalse(conn.in_transaction)
        self.assertEqual(other.execute("SELECT count(*) FROM p").fetchone(), (1,))
        for begin in ("BEGIN", "BEGIN IMMEDIATE"):
            conn.execute(begin)
            conn.execute("INSERT INTO p (name, n) VALUES ('tmp', 1)")
            self.assertTrue(conn.in_transaction)
            conn.execute("ROLLBACK")
            self.assertFalse(conn.in_transaction)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO p (name, n) VALUES ('b', 1)")
        conn.commit()
        self.assertEqual(other.execute("SELECT count(*) FROM p").fetchone(), (2,))

    def test_user_version_local(self) -> None:
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone(), (0,))
        self.conn.execute("PRAGMA user_version = 3")
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone(), (3,))

    def _violation(self, sql: str) -> Exception:
        with self.assertRaises(Exception) as ctx:
            self.conn.execute(sql)
        self.conn.rollback()
        return ctx.exception

    def test_constraint_violations_are_valueerror_with_sqlite_message(self) -> None:
        self.conn.execute("INSERT INTO p (name, n) VALUES ('a', 1)")
        self.conn.commit()
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone(), (1,))
        cases = {
            "INSERT INTO p (name, n) VALUES ('a', 2)": "UNIQUE constraint failed: p.name",
            "INSERT INTO ch (pid) VALUES (99)": "FOREIGN KEY constraint failed",
            "INSERT INTO p (name, n) VALUES ('b', 0)": "CHECK constraint failed: n > 0",
        }
        for sql, msg in cases.items():
            exc = self._violation(sql)
            self.assertIs(type(exc), ValueError, sql)
            self.assertNotIsInstance(exc, libsql.Error)
            self.assertIn("constraint failed", str(exc))
            self.assertEqual(str(exc).split(":")[0], msg.split(":")[0])

    def test_connection_shared_across_threads(self) -> None:
        errors: list[BaseException] = []
        lock = threading.Lock()

        def work(i: int) -> None:
            try:
                with lock:
                    self.conn.execute("INSERT INTO p (name, n) VALUES (?, 1)", (f"t{i}",))
                    self.conn.commit()
            except BaseException as e:  # pragma: no cover - recorded for assert
                errors.append(e)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM p").fetchone(), (4,))

    def test_stdlib_backup_reads_closed_libsql_file(self) -> None:
        self.conn.execute("INSERT INTO p (name, n) VALUES ('a', 1)")
        self.conn.commit()
        self.conn.close()
        src = sqlite3.connect(self.path)
        dst = sqlite3.connect(":memory:")
        try:
            src.backup(dst)
            self.assertEqual(dst.execute("SELECT name FROM p").fetchall(), [("a",)])
            self.assertEqual(dst.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        finally:
            src.close()
            dst.close()
