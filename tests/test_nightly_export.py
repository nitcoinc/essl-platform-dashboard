import gzip
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from essl_hub import nightly_export as ne
from essl_hub.db import Database

CFG = ne.R2Config("acct", "bucket", "key", "secret")


class ExportTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db_path = str(self.dir / "export-replica.db")
        self.status_path = str(self.dir / "export-status.json")
        self.db = Database(self.db_path, sync_url="")
        self.db.migrate_schema()
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO devices (id, site, model, ip, port, protocol) "
                "VALUES ('d1', 's', 'm', '1.2.3.4', 4370, 'tcp')"
            )
        self._seed_secrets()
        self.uploads: list[tuple[str, str, bytes]] = []

    def _seed_secrets(self) -> None:
        # Insert minimal rows bypassing FKs so the scrub has something to delete.
        raw = sqlite3.connect(self.db_path)
        raw.execute("PRAGMA foreign_keys=OFF")
        for table in ne.SCRUBBED_TABLES:
            info = raw.execute(f"PRAGMA table_info({table})").fetchall()
            cols = [r[1] for r in info if r[5] == 0]  # non-PK columns
            values = ["x" if "TEXT" in (r[2] or "").upper() else 1 for r in info if r[5] == 0]
            raw.execute(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                values,
            )
        raw.commit()
        raw.close()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def uploader(self, path: str, key: str, cfg) -> None:
        self.uploads.append((path, key, Path(path).read_bytes()))

    def tmp_dir_empty(self) -> bool:
        return not (self.dir / ne.TEMP_DIR_NAME).exists()


class RunExportTest(ExportTestBase):
    def test_happy_path_uploads_scrubbed_gzip(self) -> None:
        ok = ne.run_export(self.db, CFG, status_path=self.status_path, uploader=self.uploader)
        self.assertTrue(ok)
        self.assertEqual(len(self.uploads), 1)
        _, key, blob = self.uploads[0]
        self.assertRegex(key, r"^nightly/\d{8}T\d{6}\.db\.gz$")
        out = self.dir / "check.db"
        out.write_bytes(gzip.decompress(blob))
        conn = sqlite3.connect(out)
        for table in ne.SCRUBBED_TABLES:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0], 1)
        conn.close()
        status = ne.load_export_status(self.status_path)
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["status"], "ok")
        self.assertEqual(status[0]["size"], len(blob))
        self.assertEqual(status[0]["key"], key)
        self.assertTrue(self.tmp_dir_empty())
        # Source keeps its secret rows (only the copy is scrubbed).
        src = sqlite3.connect(self.db_path)
        self.assertEqual(src.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        src.close()

    def test_integrity_failure_records_failed_and_no_put(self) -> None:
        real_connect = sqlite3.connect

        class FakeCursor:
            def __init__(self, cur):
                self._cur = cur

            def fetchone(self):
                return ("*** corrupt",)

        class Wrapper:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *a):
                if "integrity_check" in sql:
                    return FakeCursor(None)
                return self._conn.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def fake_connect(target, *a, **kw):
            conn = real_connect(target, *a, **kw)
            return Wrapper(conn) if str(target).endswith("export.db") and "isolation_level" in kw else conn

        with mock.patch.object(ne.sqlite3, "connect", side_effect=fake_connect):
            ok = ne.run_export(self.db, CFG, status_path=self.status_path, uploader=self.uploader)
        self.assertFalse(ok)
        self.assertEqual(self.uploads, [])
        status = ne.load_export_status(self.status_path)
        self.assertEqual(status[-1]["status"], "failed")
        self.assertIn("integrity_check", status[-1]["error"])
        self.assertTrue(self.tmp_dir_empty())

    def test_upload_error_records_failed_and_cleans_up(self) -> None:
        def boom(path, key, cfg):
            raise RuntimeError("botocore EndpointConnectionError " + "x" * 2000)

        ok = ne.run_export(self.db, CFG, status_path=self.status_path, uploader=boom)
        self.assertFalse(ok)
        entry = ne.load_export_status(self.status_path)[-1]
        self.assertEqual(entry["status"], "failed")
        self.assertLessEqual(len(entry["error"]), ne.ERROR_MAX_CHARS)
        self.assertTrue(self.tmp_dir_empty())

    def test_sync_failure_records_failed_then_next_night_retries(self) -> None:
        self.db.last_sync_error = "Turso is unreachable"
        with mock.patch.object(self.db, "sync", return_value=False):
            ok = ne.run_export(self.db, CFG, status_path=self.status_path, uploader=self.uploader)
        self.assertFalse(ok)
        entry = ne.load_export_status(self.status_path)[-1]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("Turso is unreachable", entry["error"])
        self.assertEqual(self.uploads, [])
        ok = ne.run_export(self.db, CFG, status_path=self.status_path, uploader=self.uploader)
        self.assertTrue(ok)
        self.assertEqual([e["status"] for e in ne.load_export_status(self.status_path)], ["failed", "ok"])

    def test_settings_schema_version_satisfies_check_without_pragma(self) -> None:
        # A Turso-created database has user_version 0; settings carries v3.
        with self.db.connect() as c:
            c.execute("PRAGMA user_version=0")
        ok = ne.run_export(self.db, CFG, status_path=self.status_path, uploader=self.uploader)
        self.assertTrue(ok, ne.load_export_status(self.status_path)[-1])

    def test_schema_behind_skips_without_migrating(self) -> None:
        with self.db.connect() as c:
            c.execute("PRAGMA user_version=1")
            c.execute("DELETE FROM settings WHERE key='schema_version'")
        ok = ne.run_export(self.db, CFG, status_path=self.status_path, uploader=self.uploader)
        self.assertFalse(ok)
        self.assertIn("schema version", ne.load_export_status(self.status_path)[-1]["error"])
        src = sqlite3.connect(self.db_path)
        self.assertEqual(src.execute("PRAGMA user_version").fetchone()[0], 1)
        src.close()

    def test_missing_r2_config_fails(self) -> None:
        self.assertFalse(ne.run_export(self.db, None, status_path=self.status_path))


class OpenExportDatabaseTest(unittest.TestCase):
    def test_replica_is_read_only(self) -> None:
        env = {"TURSO_DATABASE_URL": "libsql://x.turso.io", "TURSO_EXPORT_AUTH_TOKEN": "t"}
        with mock.patch.dict(os.environ, env):
            database = ne.open_export_database()
        self.assertTrue(database.read_only)


class StatusFileTest(unittest.TestCase):
    def test_capped_and_tolerant(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            self.assertEqual(ne.load_export_status(path), [])
            for i in range(35):
                ne.append_status(path, {"n": i})
            entries = ne.load_export_status(path)
            self.assertEqual(len(entries), ne.STATUS_CAP)
            self.assertEqual(entries[0]["n"], 5)
            Path(path).write_text("{not json")
            self.assertEqual(ne.load_export_status(path), [])


class NextRunTest(unittest.TestCase):
    def test_before_hour_same_day(self) -> None:
        self.assertEqual(ne.next_run(datetime(2026, 10, 10, 1, 30), 2), datetime(2026, 10, 10, 2, 0))

    def test_after_hour_crosses_midnight(self) -> None:
        self.assertEqual(ne.next_run(datetime(2026, 10, 10, 23, 59), 2), datetime(2026, 10, 11, 2, 0))
        self.assertEqual(ne.next_run(datetime(2026, 12, 31, 2, 0), 2), datetime(2027, 1, 1, 2, 0))

    def test_aware_utc_converted_to_ist(self) -> None:
        # 20:45 UTC = 02:15 IST next day -> next run 02:00 the day after.
        now = datetime(2026, 10, 10, 20, 45, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(ne.next_run(now, 2), datetime(2026, 10, 12, 2, 0))
        now = datetime(2026, 10, 10, 20, 15, tzinfo=ZoneInfo("UTC"))  # 01:45 IST
        self.assertEqual(ne.next_run(now, 2), datetime(2026, 10, 11, 2, 0))


class ExportOnceFreshReplicaTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "export-replica.db")
        for suffix in ("", "-info", "-wal"):
            Path(self.path + suffix).write_text("stale")

    def _run(self, replica: bool) -> None:
        database = mock.Mock(path=self.path, replica=replica)
        with mock.patch.object(ne, "open_export_database", return_value=database), \
                mock.patch.object(ne, "run_export", return_value=True), \
                mock.patch.object(ne, "r2_config", return_value=None):
            self.assertTrue(ne._export_once())

    def test_replica_files_removed_before_each_run(self) -> None:
        self._run(replica=True)
        self.assertEqual(os.listdir(self._tmp.name), [])

    def test_local_mode_never_deletes_the_database(self) -> None:
        self._run(replica=False)
        self.assertTrue(os.path.exists(self.path))


class MainOnceTest(unittest.TestCase):
    def test_once_exit_codes(self) -> None:
        with mock.patch.object(ne, "_export_once", return_value=True):
            self.assertEqual(ne.main(["--once"]), 0)
        with mock.patch.object(ne, "_export_once", return_value=False):
            self.assertEqual(ne.main(["--once"]), 1)


if __name__ == "__main__":
    unittest.main()
