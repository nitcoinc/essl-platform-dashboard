"""libsql contract spike (plan U8, docs/plans/2026-10-10-001-...).

Pins the libsql behaviour the Turso migration relies on, so a libsql upgrade
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
- Network failures are ALSO ``ValueError`` (``'sync error: http dispatch
  error: ...'``), so type alone cannot separate integrity from offline.

Replica mode, unreachable primary (KTD10, KTD11)
- ``libsql.connect(path, sync_url=..., auth_token=...)`` performs network
  I/O at construction and RAISES ``ValueError('sync error: ...')`` when the
  primary is unreachable -- also with ``offline=True``, and also when the
  local file already exists with data. No file is created on failure.
  So reads of an existing replica are NOT possible through a replica-mode
  connection while offline.
- Refused port / DNS failure: raises in ~0s. Black-holed address
  (10.255.255.1): blocks ~134s (OS TCP timeout; ``timeout=`` arg ignored)
  and HOLDS THE GIL -- other Python threads freeze for the whole duration.
- Opening the same file as a plain local libsql file (no ``sync_url``)
  reads it fine. (Not verified against a genuine synced replica with its
  ``-info`` metadata; needs the Turso group.)

Backup (KTD14)
- After a libsql connection writes and closes the file, stdlib
  ``sqlite3.Connection.backup`` reads it and ``integrity_check`` is ``ok``.
  (Genuine replica file: Turso group / U3.)

Threads (KTD1)
- One Connection used from several threads works (no thread-affinity
  error despite the ``_check_same_thread`` kwarg). Callers must still
  serialise with a lock.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import uuid

import libsql

UNREACHABLE = "http://127.0.0.1:9"


def _run_isolated(code: str, timeout: float = 30) -> str:
    """Run libsql code in a subprocess so a hang (GIL held) can't wedge pytest."""
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()


class OfflineContractTest(unittest.TestCase):
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

    def _replica_connect(self, path: str, extra: str = "") -> str:
        return _run_isolated(f"""
            import libsql
            try:
                libsql.connect({path!r}, sync_url={UNREACHABLE!r}, auth_token="x"{extra})
                print("OK")
            except Exception as e:
                print(type(e).__name__, "|", e)
        """)

    def test_replica_connect_raises_when_primary_unreachable(self) -> None:
        fresh = os.path.join(self._tmp.name, "fresh.db")
        out = self._replica_connect(fresh)
        self.assertTrue(out.startswith("ValueError | sync error"), out)
        self.assertFalse(os.path.exists(fresh))

    def test_replica_connect_raises_even_with_existing_file_and_offline_flag(self) -> None:
        self.conn.execute("INSERT INTO p (name, n) VALUES ('a', 1)")
        self.conn.commit()
        self.conn.close()
        for extra in ("", ", offline=True"):
            out = self._replica_connect(self.path, extra)
            self.assertTrue(out.startswith("ValueError | sync error"), out)
        # Same file opened locally (no sync_url) is still readable.
        local = libsql.connect(self.path)
        self.addCleanup(local.close)
        self.assertEqual(local.execute("SELECT name FROM p").fetchall(), [("a",)])


TURSO_URL = os.environ.get("TURSO_TEST_URL")
TURSO_TOKEN = os.environ.get("TURSO_TEST_TOKEN")
TURSO_RO_TOKEN = os.environ.get("TURSO_TEST_RO_TOKEN")


@unittest.skipUnless(TURSO_URL and TURSO_TOKEN, "TURSO_TEST_URL/TURSO_TEST_TOKEN not set")
class TursoContractTest(unittest.TestCase):
    """Runs against a scratch Turso database. Creates and drops its own table."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.table = f"u8_{uuid.uuid4().hex[:8]}"
        self.conn = self._connect("rw.db", TURSO_TOKEN)
        self.conn.execute(f"CREATE TABLE {self.table}_p (id INTEGER PRIMARY KEY, v TEXT)")
        self.conn.execute(
            f"CREATE TABLE {self.table}_c (id INTEGER PRIMARY KEY,"
            f" pid INT REFERENCES {self.table}_p(id))"
        )
        self.conn.commit()
        self.addCleanup(self._drop)

    def _connect(self, name: str, token: str | None, **kw):
        return libsql.connect(
            os.path.join(self._tmp.name, name), sync_url=TURSO_URL, auth_token=token or "", **kw
        )

    def _drop(self) -> None:
        self.conn.execute(f"DROP TABLE IF EXISTS {self.table}_c")
        self.conn.execute(f"DROP TABLE IF EXISTS {self.table}_p")
        self.conn.commit()
        self.conn.close()

    def test_read_your_writes_on_one_connection(self) -> None:
        self.conn.execute(f"INSERT INTO {self.table}_p (v) VALUES ('a')")
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(f"SELECT v FROM {self.table}_p").fetchall(), [("a",)]
        )

    def test_reads_inside_transaction_see_primary(self) -> None:
        other = self._connect("other.db", TURSO_TOKEN)
        self.addCleanup(other.close)
        other.sync()
        self.conn.execute(f"INSERT INTO {self.table}_p (v) VALUES ('fresh')")
        self.conn.commit()
        # `other` has not synced since the insert; KTD7 assumes reads may be
        # stale until the first write. Record what actually happens.
        other.execute("BEGIN")
        other.execute(f"UPDATE {self.table}_p SET v = v WHERE 0")
        seen = other.execute(f"SELECT count(*) FROM {self.table}_p").fetchone()
        other.execute("ROLLBACK")
        self.assertIn(seen, [(0,), (1,)])
        print(f"\n[U8] read inside tx after first write saw {seen[0]} row(s)")

    def test_user_version_write_rejected(self) -> None:
        # Turso refuses PRAGMA writes over Hrana; Database keeps the schema
        # version in settings.schema_version instead (reads still work).
        before = self.conn.execute("PRAGMA user_version").fetchone()[0]
        with self.assertRaises(ValueError) as ctx:
            self.conn.execute(f"PRAGMA user_version = {before + 1}")
        self.assertIn("not allowed", str(ctx.exception))

    def test_foreign_keys_enforced_on_forwarded_write(self) -> None:
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.assertRaises(ValueError) as ctx:
            self.conn.execute(f"INSERT INTO {self.table}_c (pid) VALUES (999)")
            self.conn.commit()
        self.assertIn("FOREIGN KEY constraint failed", str(ctx.exception))

    @unittest.skipUnless(TURSO_RO_TOKEN, "TURSO_TEST_RO_TOKEN not set")
    def test_read_only_token_rejects_writes(self) -> None:
        ro = self._connect("ro.db", TURSO_RO_TOKEN)
        self.addCleanup(ro.close)
        ro.sync()
        with self.assertRaises(ValueError):
            ro.execute(f"INSERT INTO {self.table}_p (v) VALUES ('no')")
            ro.commit()

    @unittest.skipUnless(TURSO_RO_TOKEN, "TURSO_TEST_RO_TOKEN not set")
    def test_read_only_token_blocks_foreign_keys_pragma(self) -> None:
        # A replica forwards PRAGMA foreign_keys to the primary as a write, so
        # Database(read_only=True) (nightly export) must skip it.
        ro = self._connect("rofk.db", TURSO_RO_TOKEN)
        self.addCleanup(ro.close)
        with self.assertRaises(ValueError):
            ro.execute("PRAGMA foreign_keys=ON")
            ro.sync()
        from essl_hub.db import Database

        db = Database(
            os.path.join(self._tmp.name, "export.db"),
            sync_url=TURSO_URL,
            auth_token=TURSO_RO_TOKEN,
            read_only=True,
        )
        self.addCleanup(db.close)
        self.assertTrue(db.sync(), db.last_sync_error)


    def test_rebuild_replica_bootstraps_fresh_copy(self) -> None:
        from essl_hub.db import Database

        path = os.path.join(self._tmp.name, "rebuild.db")
        db = Database(path, sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
        self.addCleanup(db.close)
        self.assertTrue(db.sync(), db.last_sync_error)
        db.rebuild_replica()
        self.assertIsNotNone(db.last_rebuild_at)
        with db.connect() as connection:
            tables = connection.execute(
                "SELECT count(*) AS n FROM sqlite_master WHERE name = ?", (f"{self.table}_p",)
            ).fetchone()["n"]
        self.assertEqual(tables, 1)
        self.assertTrue(db.sync(), db.last_sync_error)


if __name__ == "__main__":
    unittest.main()
