from __future__ import annotations

import gzip
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from essl_hub import backup as bk
from essl_hub.db import SCHEMA_VERSION, Database

NOW = datetime(2026, 10, 14, 10, 0, 0)


class FakeR2:
    """In-memory bucket. `fail_get`/`fail_put` raise to simulate R2 errors."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_get: Exception | None = None
        self.fail_put: Exception | None = None
        self.puts: list[str] = []

    def put_file(self, path: str, key: str) -> None:
        self.put_bytes(key, Path(path).read_bytes())

    def put_bytes(self, key: str, data: bytes) -> None:
        if self.fail_put is not None:
            raise self.fail_put
        self.objects[key] = data
        self.puts.append(key)

    def get_bytes(self, key: str) -> bytes | None:
        if self.fail_get is not None:
            raise self.fail_get
        return self.objects.get(key)

    def download(self, key: str, dest: str) -> None:
        Path(dest).write_bytes(self.objects[key])

    def list(self, prefix: str) -> list[bk.ObjectInfo]:
        return [
            bk.ObjectInfo(k, len(v), bk.key_time(k))
            for k, v in sorted(self.objects.items())
            if k.startswith(prefix)
        ]

    def delete(self, keys: list[str]) -> None:
        for key in keys:
            self.objects.pop(key, None)

    def latest(self) -> dict | None:
        raw = self.objects.get(bk.LATEST_KEY)
        return json.loads(raw) if raw else None


def _insert_punch(path: str, n: int) -> None:
    db = Database(path, must_exist=True)
    try:
        with db.transaction() as c:
            c.execute(
                "INSERT INTO punches (fingerprint, device_id, site, device_user_id, "
                "event_timestamp, raw_json, collected_at) VALUES (?, 'uppal-x990', 's', '1', "
                "'2026-10-14 09:00:00', '{}', '2026-10-14 09:00:00')",
                (f"fp-{n}",),
            )
    finally:
        db.close()


def _seed_secrets(path: str) -> None:
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys=OFF")
    for table in bk.SCRUBBED_TABLES:
        info = raw.execute(f"PRAGMA table_info({table})").fetchall()
        cols = [r[1] for r in info if r[5] == 0]
        values = ["x" if "TEXT" in (r[2] or "").upper() else 1 for r in info if r[5] == 0]
        raw.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            values,
        )
    raw.commit()
    raw.close()


def _setting(path: str, key: str) -> str | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db_path = str(self.dir / "essl.db")
        self.status_path = str(self.dir / "backup-status.json")
        self.r2 = FakeR2()

    def init_db(self) -> str:
        with redirect_stdout(io.StringIO()):
            bk.cmd_init(self.db_path)
        return _setting(self.db_path, "database_id")

    def cycle(self, now: datetime = NOW, **kw) -> str:
        kw.setdefault("export_hour", 2)
        return bk.run_cycle(self.db_path, self.r2, status_path=self.status_path, now=now, **kw)

    def status(self) -> dict:
        return bk.load_status(self.status_path)

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir() if p.name.startswith("."))


# --- U3: database_id guard (written first) -----------------------------------


class GuardTest(Base):
    def test_first_cycle_into_empty_bucket_uploads_and_writes_latest(self) -> None:
        db_id = self.init_db()
        self.assertEqual(self.cycle(), "ok")
        recent = [k for k in self.r2.objects if k.startswith("recent/")]
        self.assertEqual(recent, ["recent/20261014T100000.db.gz"])
        latest = self.r2.latest()
        self.assertEqual(latest["database_id"], db_id)
        self.assertEqual(latest["key"], recent[0])
        status = self.status()
        self.assertIsNone(status["last_error"])
        self.assertFalse(status["blocked"])
        self.assertEqual(status["last_key"], recent[0])
        self.assertEqual(self.leftovers(), [])

    def test_other_database_id_blocks_upload(self) -> None:
        self.init_db()
        self.r2.objects[bk.LATEST_KEY] = json.dumps({"database_id": "someone-else"}).encode()
        self.assertEqual(self.cycle(), "blocked")
        self.assertEqual(list(self.r2.objects), [bk.LATEST_KEY])
        status = self.status()
        self.assertTrue(status["blocked"])
        self.assertIn("restore", status["last_error"])
        self.assertIn("adopt", status["last_error"])

    def test_missing_local_database_id_blocks_naming_adopt(self) -> None:
        self.init_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM settings WHERE key='database_id'")
        conn.commit()
        conn.close()
        self.assertEqual(self.cycle(), "blocked")
        self.assertEqual(self.r2.objects, {})
        self.assertIn("adopt", self.status()["last_error"])

    def test_latest_read_error_skips_upload(self) -> None:
        self.init_db()
        for error in (TimeoutError("read timed out"), RuntimeError("500 Internal Error")):
            self.r2.fail_get = error
            self.assertEqual(self.cycle(), "failed")
            self.assertEqual(self.r2.objects, {})
            self.assertIn("latest.json", self.status()["last_error"])
        self.r2.fail_get = None  # only a 404 (None) permits the first upload
        self.assertEqual(self.cycle(), "ok")


# --- U3: pruning (written first) ----------------------------------------------


class PruneTest(Base):
    def test_retention_windows(self) -> None:
        def key(prefix: str, at: datetime) -> str:
            return f"{prefix}{at.strftime('%Y%m%dT%H%M%S')}.db.gz"

        keep = {
            key("recent/", NOW - timedelta(hours=47)),
            f"nightly/{(NOW - timedelta(days=29)).strftime('%Y%m%d')}.db.gz",
            bk.LATEST_KEY,
            "nightly/unparseable.db.gz",
        }
        drop = {
            key("recent/", NOW - timedelta(hours=49)),
            f"nightly/{(NOW - timedelta(days=31)).strftime('%Y%m%d')}.db.gz",
            "nightly/20260901T020000.db.gz",  # old-format key, 43 days old
        }
        for k in keep | drop:
            self.r2.objects[k] = b"x"
        deleted = bk.prune(self.r2, NOW, keep_hours=48, keep_days=30)
        self.assertEqual(set(deleted), drop)
        self.assertEqual(set(self.r2.objects), keep)

    def test_simulated_three_days_matches_retention(self) -> None:
        self.init_db()
        counter = iter(range(10_000))

        def fake_snapshot(source_path, work_dir):
            packed = Path(work_dir) / "export.db.gz"
            packed.write_bytes(f"snap-{next(counter)}".encode())
            return bk.Snapshot(packed, _setting(self.db_path, "database_id"), 0)

        start = datetime(2026, 10, 11, 0, 0)
        at = start
        with mock.patch.object(bk, "_build_snapshot", side_effect=fake_snapshot):
            while at < start + timedelta(days=3):
                self.assertEqual(self.cycle(at, keep_hours=48, keep_days=30, export_hour=2), "ok")
                at += timedelta(minutes=15)
        last = at - timedelta(minutes=15)
        recent = [o for o in self.r2.list("recent/")]
        self.assertTrue(all(last - o.modified <= timedelta(hours=48) for o in recent))
        self.assertEqual(len(recent), 48 * 4 + 1)
        nightly = sorted(o.key for o in self.r2.list("nightly/"))
        self.assertEqual(
            nightly,
            ["nightly/20261011.db.gz", "nightly/20261012.db.gz", "nightly/20261013.db.gz"],
        )


# --- U3: cycle behaviour --------------------------------------------------------


class CycleTest(Base):
    def test_unchanged_data_skips_upload_but_advances_last_ok(self) -> None:
        self.init_db()
        self.cycle()
        first = self.status()
        puts = len(self.r2.puts)
        later = NOW + timedelta(minutes=15)
        self.assertEqual(self.cycle(later), "skipped")
        self.assertEqual(len(self.r2.puts), puts)
        status = self.status()
        self.assertGreater(status["last_ok_at"], first["last_ok_at"])
        self.assertEqual(status["last_upload_at"], first["last_upload_at"])
        self.assertEqual(status["entries"][-1]["status"], "ok")

    def test_insert_causes_new_upload(self) -> None:
        self.init_db()
        self.cycle()
        _insert_punch(self.db_path, 1)
        self.assertEqual(self.cycle(NOW + timedelta(minutes=15)), "ok")
        self.assertIn("recent/20261014T101500.db.gz", self.r2.objects)
        self.assertEqual(self.r2.latest()["punches"], 1)

    def test_snapshot_is_scrubbed_and_deterministic(self) -> None:
        self.init_db()
        _seed_secrets(self.db_path)
        work1, work2 = self.dir / "w1", self.dir / "w2"
        work1.mkdir()
        work2.mkdir()
        a = bk._build_snapshot(self.db_path, work1)
        b = bk._build_snapshot(self.db_path, work2)
        self.assertEqual(a.path.read_bytes(), b.path.read_bytes())
        out = self.dir / "check.db"
        out.write_bytes(gzip.decompress(a.path.read_bytes()))
        conn = sqlite3.connect(out)
        for table in bk.SCRUBBED_TABLES:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        conn.close()
        src = sqlite3.connect(self.db_path)
        self.assertEqual(src.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        src.close()

    def test_schema_behind_fails_without_migrating(self) -> None:
        self.init_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version=1")
        conn.execute("DELETE FROM settings WHERE key='schema_version'")
        conn.commit()
        conn.close()
        self.assertEqual(self.cycle(), "failed")
        self.assertIn("schema version", self.status()["last_error"])

    def test_nightly_once_per_day_after_hour_and_across_restart(self) -> None:
        self.init_db()
        early = datetime(2026, 10, 14, 1, 0)
        self.cycle(early)
        self.assertEqual(self.r2.list("nightly/"), [])
        _insert_punch(self.db_path, 1)
        self.cycle(datetime(2026, 10, 14, 2, 0))
        self.assertEqual([o.key for o in self.r2.list("nightly/")], ["nightly/20261014.db.gz"])
        nightly_bytes = self.r2.objects["nightly/20261014.db.gz"]
        # Restart: a fresh status file must not cause a second nightly upload.
        os.unlink(self.status_path)
        _insert_punch(self.db_path, 2)
        self.cycle(datetime(2026, 10, 14, 2, 15))
        self.assertEqual(len(self.r2.list("nightly/")), 1)
        self.assertEqual(self.r2.objects["nightly/20261014.db.gz"], nightly_bytes)

    def test_nightly_written_on_sha_skip_when_due(self) -> None:
        self.init_db()
        self.cycle(datetime(2026, 10, 14, 1, 0))
        self.assertEqual(self.r2.list("nightly/"), [])
        self.assertEqual(self.cycle(datetime(2026, 10, 14, 2, 0)), "skipped")
        self.assertEqual([o.key for o in self.r2.list("nightly/")], ["nightly/20261014.db.gz"])
        self.assertEqual(self.r2.objects["nightly/20261014.db.gz"],
                         self.r2.objects["recent/20261014T010000.db.gz"])
        self.assertNotIn("recent/20261014T020000.db.gz", self.r2.objects)
        puts = len(self.r2.puts)
        self.assertEqual(self.cycle(datetime(2026, 10, 14, 2, 15)), "skipped")
        self.assertEqual(len(self.r2.puts), puts)

    def test_missing_database_reports_not_set_up(self) -> None:
        self.assertEqual(self.cycle(), "not_set_up")
        self.assertEqual(self.r2.objects, {})
        self.assertFalse(os.path.exists(self.db_path))
        self.assertIn("init", self.status()["last_error"])

    def test_upload_error_recorded_and_retried(self) -> None:
        self.init_db()
        self.r2.fail_put = RuntimeError("EndpointConnectionError " + "x" * 2000)
        self.assertEqual(self.cycle(), "failed")
        self.assertIsNone(self.r2.latest())
        self.assertLessEqual(len(self.status()["last_error"]), bk.ERROR_MAX_CHARS)
        self.assertTrue(os.path.exists(self.db_path))
        self.r2.fail_put = None
        self.assertEqual(self.cycle(NOW + timedelta(minutes=15)), "ok")
        self.assertIsNotNone(self.r2.latest())
        self.assertIsNone(self.status()["last_error"])

    def test_no_r2_config_records_error(self) -> None:
        self.init_db()
        status = bk.run_cycle(self.db_path, None, status_path=self.status_path, now=NOW)
        self.assertEqual(status, "failed")
        self.assertIn("R2", self.status()["last_error"])
        self.assertIs(self.status()["r2_configured"], False)

    def test_cycle_records_r2_configured_and_interval_for_web(self) -> None:
        self.init_db()
        self.cycle(NOW)
        self.assertIs(self.status()["r2_configured"], True)
        self.assertEqual(self.status()["interval_minutes"], 15)

    def test_status_entries_capped(self) -> None:
        self.init_db()
        for i in range(bk.STATUS_CAP + 5):
            self.cycle(NOW + timedelta(minutes=15 * i))
        self.assertEqual(len(self.status()["entries"]), bk.STATUS_CAP)
        Path(self.status_path).write_text("{not json")
        self.assertEqual(bk.load_status(self.status_path)["entries"], [])

    def test_now_waits_for_running_cycle_lock(self) -> None:
        self.init_db()
        lock = bk.backup_lock(self.db_path)
        lock.__enter__()
        results: list[str] = []
        worker = threading.Thread(target=lambda: results.append(self.cycle()))
        worker.start()
        time.sleep(0.3)
        self.assertEqual(results, [])
        lock.__exit__(None, None, None)
        worker.join(10)
        self.assertEqual(results, ["ok"])
        self.assertEqual(self.leftovers(), [])

    def test_snapshot_during_writes_passes_integrity(self) -> None:
        # The collector is a separate process in production. Two SQLite
        # libraries (libsql and stdlib) in ONE process break each other's
        # POSIX locks, so the writer must run in its own process here too.
        self.init_db()
        script = (
            "import sys, time\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "from tests.test_backup import _insert_punch\n"
            "end = time.time() + 30\n"
            "n = 0\n"
            "while time.time() < end:\n"
            "    _insert_punch(sys.argv[1], n); n += 1\n"
        )
        repo = str(Path(__file__).resolve().parent.parent)
        writer = subprocess.Popen([sys.executable, "-c", script, self.db_path, repo])
        try:
            time.sleep(1.0)
            for i in range(3):
                self.assertEqual(self.cycle(NOW + timedelta(minutes=15 * i)), "ok",
                                 self.status()["last_error"])
            self.assertIsNone(writer.poll(), "writer died")
        finally:
            writer.kill()
            writer.wait()
        self.assertGreater(self.r2.latest()["punches"], 0)


# --- U2: admin commands -------------------------------------------------------


class InitTest(Base):
    def test_init_creates_current_schema_with_id(self) -> None:
        db_id = self.init_db()
        self.assertTrue(db_id)
        self.assertEqual(_setting(self.db_path, "schema_version"), str(SCHEMA_VERSION))

    def test_refuses_existing_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                path = Path(self.db_path + suffix)
                path.write_bytes(b"keep")
                for command in (
                    lambda: bk.cmd_init(self.db_path),
                    lambda: bk.cmd_restore(self.db_path, self.r2),
                    lambda: bk.cmd_import_turso(self.db_path, "libsql://x", "t"),
                ):
                    with self.assertRaises(bk.CommandError):
                        command()
                self.assertEqual(path.read_bytes(), b"keep")
                path.unlink()


class RestoreTest(Base):
    def _snapshots(self) -> tuple[str, str]:
        db_id = self.init_db()
        _seed_secrets(self.db_path)
        self.cycle()
        _insert_punch(self.db_path, 1)
        self.cycle(NOW + timedelta(minutes=15))
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                os.unlink(self.db_path + suffix)
        return db_id, "recent/20261014T100000.db.gz"

    def _restore(self, **kw) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            bk.cmd_restore(self.db_path, self.r2, **kw)
        return out.getvalue()

    def test_restore_newest(self) -> None:
        db_id, _ = self._snapshots()
        self._restore()
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM punches").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        conn.close()
        self.assertEqual(_setting(self.db_path, "database_id"), db_id)
        self.assertEqual(self.leftovers(), [])

    def test_restore_key(self) -> None:
        _, older = self._snapshots()
        self._restore(key=older)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM punches").fetchone()[0], 0)
        conn.close()

    def test_corrupt_download_leaves_nothing(self) -> None:
        self.r2.objects["recent/20261014T100000.db.gz"] = b"not gzip"
        with self.assertRaises(bk.CommandError):
            self._restore()
        self.assertFalse(os.path.exists(self.db_path))
        self.assertEqual(self.leftovers(), [])
        self.r2.objects["recent/20261014T100000.db.gz"] = gzip.compress(b"garbage" * 1000)
        with self.assertRaises(bk.CommandError):
            self._restore()
        self.assertFalse(os.path.exists(self.db_path))
        self.assertEqual(self.leftovers(), [])

    def test_newer_schema_refused(self) -> None:
        raw = self.dir / "new.db"
        conn = sqlite3.connect(raw)
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        self.r2.objects["recent/20261014T100000.db.gz"] = gzip.compress(raw.read_bytes())
        raw.unlink()
        with self.assertRaisesRegex(bk.CommandError, "newer"):
            self._restore()
        self.assertFalse(os.path.exists(self.db_path))

    def test_snapshot_without_id_mentions_adopt(self) -> None:
        self.init_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM settings WHERE key='database_id'")
        conn.commit()
        conn.close()
        work = self.dir / "w"
        work.mkdir()
        snap = bk._build_snapshot(self.db_path, work)
        self.r2.objects["nightly/20260901T020000.db.gz"] = snap.path.read_bytes()
        import shutil
        shutil.rmtree(work)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                os.unlink(self.db_path + suffix)
        out = self._restore(key="nightly/20260901T020000.db.gz")
        self.assertIn("adopt", out)

    def test_restore_mode_0600_temp(self) -> None:
        self._snapshots()
        seen: list[int] = []
        real = bk._verify_restored

        def spy(path: str) -> str | None:
            seen.append(os.stat(path).st_mode & 0o777)
            return real(path)

        with mock.patch.object(bk, "_verify_restored", side_effect=spy):
            self._restore()
        self.assertEqual(seen, [0o600])


class DrillTest(Base):
    def test_drill_restore_gets_new_id_and_never_uploads(self) -> None:
        db_id = self.init_db()
        self.cycle()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                os.unlink(self.db_path + suffix)
        with redirect_stdout(io.StringIO()):
            bk.cmd_restore(self.db_path, self.r2, drill=True)
        new_id = _setting(self.db_path, "database_id")
        self.assertTrue(new_id)
        self.assertNotEqual(new_id, db_id)
        before = dict(self.r2.objects)
        self.assertEqual(self.cycle(NOW + timedelta(minutes=15)), "blocked")
        self.assertEqual(self.r2.objects, before)


class HasBackupsTest(Base):
    def test_exit_codes(self) -> None:
        with mock.patch.object(bk, "make_client", return_value=self.r2):
            self.assertEqual(bk.main(["has-backups"]), 3)
            self.r2.objects[bk.LATEST_KEY] = b'{"database_id": "x"}'
            self.assertEqual(bk.main(["has-backups"]), 0)
            self.r2.fail_get = OSError("down")
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(bk.main(["has-backups"]), 1)


class AdoptTest(Base):
    def test_adopt_writes_null_key(self) -> None:
        self.init_db()
        self.cycle()
        with redirect_stdout(io.StringIO()):
            bk.cmd_adopt(self.db_path, self.r2, confirm=lambda: True)
        self.assertIsNone(self.r2.latest()["key"])

    def test_adopt_needs_confirmation(self) -> None:
        db_id = self.init_db()
        self.r2.objects[bk.LATEST_KEY] = json.dumps({"database_id": "other"}).encode()
        with redirect_stdout(io.StringIO()), self.assertRaises(bk.CommandError):
            bk.cmd_adopt(self.db_path, self.r2, confirm=lambda: False)
        self.assertEqual(self.r2.latest()["database_id"], "other")
        with redirect_stdout(io.StringIO()):
            bk.cmd_adopt(self.db_path, self.r2, confirm=lambda: True)
        self.assertEqual(self.r2.latest()["database_id"], db_id)
        self.assertEqual(self.cycle(), "ok")

    def test_adopt_writes_id_when_missing(self) -> None:
        self.init_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM settings WHERE key='database_id'")
        conn.commit()
        conn.close()
        with redirect_stdout(io.StringIO()):
            bk.cmd_adopt(self.db_path, self.r2, confirm=lambda: True)
        new_id = _setting(self.db_path, "database_id")
        self.assertTrue(new_id)
        self.assertEqual(self.r2.latest()["database_id"], new_id)


class FakeReplica:
    """Mimics libsql's embedded replica: rows stay in -wal after close."""

    def __init__(self, path: str, rows: int) -> None:
        self.path = path
        seed = Database(path + ".seed")
        seed.migrate_schema()
        seed.close()
        for suffix in ("-wal", "-shm"):
            if os.path.exists(path + ".seed" + suffix):
                os.unlink(path + ".seed" + suffix)
        os.replace(path + ".seed", path)
        Path(path + "-info").write_text("{}")
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=0")
        self.conn.execute(
            "INSERT INTO devices (id, site, model, ip, port, protocol) "
            "VALUES ('x990', 's', 'm', '1.2.3.4', 4370, 'tcp')"
        )
        for n in range(rows):
            self.conn.execute(
                "INSERT INTO punches (fingerprint, device_id, site, raw_json, collected_at) "
                "VALUES (?, 'x990', 's', '{}', '2026-10-14')",
                (f"fp-{n}",),
            )
        self.synced = False

    def sync(self) -> None:
        self.synced = True

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def close(self) -> None:
        self.conn.close()


class ImportTursoTest(Base):
    def test_import_keeps_wal_rows_and_drops_sidecars(self) -> None:
        replicas: list[FakeReplica] = []

        def connect(path, sync_url, auth_token):
            replica = FakeReplica(path, rows=25)
            replicas.append(replica)
            return replica

        with redirect_stdout(io.StringIO()):
            bk.cmd_import_turso(self.db_path, "libsql://x.turso.io", "t", connect=connect)
        self.assertTrue(replicas[0].synced)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM punches").fetchone()[0], 25)
        conn.close()
        self.assertTrue(_setting(self.db_path, "database_id"))
        names = sorted(os.listdir(self.dir))
        self.assertNotIn("essl.db-info", names)
        self.assertEqual([n for n in names if n.startswith(".")], [])

    def test_count_mismatch_refused(self) -> None:
        def connect(path, sync_url, auth_token):
            replica = FakeReplica(path, rows=3)
            real_execute = replica.execute

            def execute(sql, params=()):
                if "COUNT" in sql:
                    return sqlite3.connect(":memory:").execute("SELECT 99")
                return real_execute(sql, params)

            replica.execute = execute
            return replica

        with self.assertRaisesRegex(bk.CommandError, "punch"):
            bk.cmd_import_turso(self.db_path, "libsql://x.turso.io", "t", connect=connect)
        self.assertFalse(os.path.exists(self.db_path))
        self.assertEqual(self.leftovers(), [])


class CliTest(Base):
    def test_status_and_list_run(self) -> None:
        self.init_db()
        self.cycle()
        env = {"ESSL_DB_PATH": self.db_path}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(bk, "make_client", return_value=self.r2):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(bk.main(["list"]), 0)
                self.assertEqual(bk.main(["status"]), 0)
        text = out.getvalue()
        self.assertIn("recent/20261014T100000.db.gz", text)
        self.assertIn("database_id", text)

    def test_command_error_exit_code(self) -> None:
        Path(self.db_path).write_bytes(b"x")
        with mock.patch.dict(os.environ, {"ESSL_DB_PATH": self.db_path}):
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(bk.main(["init"]), 1)


if __name__ == "__main__":
    unittest.main()
