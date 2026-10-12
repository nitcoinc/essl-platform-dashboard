from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from essl_hub import db as db_module
from essl_hub.config import DeviceConfig

_SPEC = importlib.util.spec_from_file_location(
    "migrate_postgres",
    Path(__file__).resolve().parent.parent / "scripts" / "migrate_postgres.py",
)
migrate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migrate)


def _punch(pid: int, fingerprint: str, collected: datetime) -> dict[str, object]:
    return {
        "id": pid, "fingerprint": fingerprint, "device_id": "uppal-x990", "site": "Uppal",
        "device_serial": "SN1", "device_user_id": "42", "employee_name": "Asha",
        "event_timestamp": datetime(2026, 9, 1, 9, 5, 7, 123456), "raw_state": "0",
        "door": None, "reader": None, "raw_direction_byte": 1, "trusted_direction": "in",
        "raw_json": {"uid": 42, "nested": {"a": [1, 2]}}, "collected_at": collected,
    }


class FakeReader:
    def __init__(self, tables: dict[str, list[dict[str, object]]]):
        self.tables = tables

    def rows(self, table, since=None):
        rows = self.tables.get(table, [])
        if since is not None:
            rows = [r for r in rows if r["collected_at"] >= since]
        return [dict(r) for r in rows]


def _source() -> FakeReader:
    return FakeReader({
        "devices": [{
            "id": "uppal-x990", "site": "Uppal", "model": "X990", "ip": "192.168.1.201",
            "port": 4370, "protocol": "zk-attendance", "status": "connected",
            "transport": "tcp", "serial_number": "SN1", "firmware": "6.60",
            "last_attempt": datetime(2026, 9, 1, 10, 0, 0),
            "last_success": datetime(2026, 9, 1, 10, 0, 1), "last_error": None,
            "records_seen": 7, "comm_password": "123",
            "connection_updated_at": datetime(2026, 8, 30, 18, 30, 0),
        }],
        "settings": [{"key": "daily_target_minutes", "value": "510"}],
        "employees": [{
            "id": 17, "device_user_id": "42", "name": "Asha", "email": "asha@example.com",
            "role": "hr", "created_at": datetime(2026, 1, 2, 3, 4, 5),
            "updated_at": datetime(2026, 1, 2, 3, 4, 5),
        }],
        "punches": [
            _punch(100, "fp-a", datetime(2026, 9, 1, 9, 6, 0)),
            _punch(205, "fp-b", datetime(2026, 9, 2, 9, 6, 0)),
        ],
        "api_keys": [{
            "id": 3, "prefix": "pfx", "key_hash": "h", "label": "bi",
            "created_at": datetime(2026, 2, 1, 0, 0, 0), "revoked_at": None,
            "last_used_at": None,
        }],
    })


SEED = (DeviceConfig(
    id="uppal-x990", site="Uppal", model="X990", ip="192.168.1.99", port=4370,
    protocol="zk-attendance", password_env="ESSL_X990_PASSWORD",
),)


class MigrateTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "essl.db")
        self.database = db_module.Database(self.path, sync_url="")
        self.addCleanup(self.database.close)

    def _verify(self, source, tables=tuple(migrate.TABLE_COLUMNS)):
        return migrate.verify(source, self.database, tables, out=lambda _l: None)

    def _raw(self, sql):
        with self.database.connect() as c:
            return c.execute(sql).fetchall()

    def test_full_copy_preserves_rows_and_ids(self):
        source = _source()
        migrate.migrate_full(source, self.database)
        self.assertEqual(self._verify(source), [])
        self.assertEqual([r["id"] for r in self._raw("SELECT id FROM punches ORDER BY id")], [100, 205])
        self.assertEqual(self._raw("SELECT id FROM employees")[0]["id"], 17)
        self.assertEqual(self._raw("SELECT id FROM api_keys")[0]["id"], 3)
        employee = self._raw("SELECT role, manager_id FROM employees")[0]
        self.assertEqual((employee["role"], employee["manager_id"]), ("hr", None))
        raw = self._raw("SELECT CAST(raw_json AS TEXT) AS j FROM punches WHERE id=100")[0]["j"]
        self.assertEqual(json.loads(raw), {"uid": 42, "nested": {"a": [1, 2]}})

    def test_source_values_win_over_seed(self):
        self.database.create_schema()
        self.database.seed_defaults(SEED)
        migrate.migrate_full(_source(), self.database)
        device = self._raw("SELECT ip, comm_password FROM devices")[0]
        self.assertEqual((device["ip"], device["comm_password"]), ("192.168.1.201", "123"))
        self.assertEqual(self.database.setting("daily_target_minutes"), "510")
        self.assertEqual(self._verify(_source()), [])

    def test_field_diff_reports_mismatch_and_main_exits_nonzero(self):
        source = _source()
        migrate.migrate_full(source, self.database)
        with self.database.transaction() as c:
            c.execute("UPDATE devices SET port = 4371")
        self.assertEqual(len(self._verify(source)), 1)
        self.assertEqual(len(self._verify(source, ("devices",))), 1)
        self.assertEqual(self._verify(source, ("punches",)), [])

    def test_refuses_non_empty_target(self):
        self.database.create_schema()
        with self.database.transaction() as c:
            c.execute(
                "INSERT INTO employees (device_user_id, name, email, created_at, updated_at) "
                "VALUES ('9', 'X', 'x@example.com', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        with self.assertRaises(migrate.MigrationError):
            migrate.migrate_full(_source(), self.database)
        self.assertEqual(self._raw("SELECT COUNT(*) AS n FROM devices")[0]["n"], 0)
        self.assertEqual(self._raw("SELECT COUNT(*) AS n FROM employees")[0]["n"], 1)
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            code = migrate.main(["--source", "x", "--target", self.path], reader_factory=lambda _u: _source())
        self.assertEqual(code, 2)

    def test_delta_skips_duplicates_and_adds_new(self):
        source = _source()
        migrate.migrate_full(source, self.database)
        source.tables["punches"].append(_punch(300, "fp-c", datetime(2026, 9, 3, 8, 0, 0)))
        added = migrate.migrate_punches_since(source, self.database, datetime(2026, 9, 2, 0, 0, 0))
        self.assertEqual(added, 1)
        fps = [r["fingerprint"] for r in self._raw("SELECT fingerprint FROM punches ORDER BY id")]
        self.assertEqual(fps, ["fp-a", "fp-b", "fp-c"])
        self.assertEqual(self._verify(source, ("punches",)), [])

    def test_delta_rerun_is_idempotent_across_batches(self):
        source = _source()
        migrate.migrate_full(source, self.database)
        for i in range(5):
            source.tables["punches"].append(_punch(300 + i, f"fp-n{i}", datetime(2026, 9, 3, 8, i, 0)))
        since = datetime(2026, 9, 2, 0, 0, 0)
        self.assertEqual(migrate.migrate_punches_since(source, self.database, since, batch_rows=2), 5)
        self.assertEqual(migrate.migrate_punches_since(source, self.database, since, batch_rows=2), 0)
        self.assertEqual(self._raw("SELECT COUNT(*) AS n FROM punches")[0]["n"], 7)

    def _main(self, argv, source):
        out = io.StringIO()
        with redirect_stdout(out):
            code = migrate.main(argv, reader_factory=lambda _u: source)
        return code, out.getvalue()

    def test_main_full_builds_v3_file_and_prints_upload_command(self):
        self.database.close()
        target = os.path.join(os.path.dirname(self.path), "upload.db")
        code, out = self._main(["--source", "x", "--target", target], _source())
        self.assertEqual(code, 0)
        self.assertIn("punches: source=2 target=2", out)
        self.assertIn(f"turso db create essl --from-file {Path(target).resolve()}", out)
        check = db_module.Database(target, sync_url="")
        self.addCleanup(check.close)
        with check.connect() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()["user_version"], 3)
            self.assertEqual(c.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"], 1)

    def test_main_full_empty_source_exits_zero(self):
        target = os.path.join(os.path.dirname(self.path), "empty.db")
        code, out = self._main(["--source", "x", "--target", target], FakeReader({}))
        self.assertEqual(code, 0)
        for table in migrate.TABLE_COLUMNS:
            self.assertIn(f"{table}: source=0 target=0", out)

    def test_delta_without_turso_env_refuses(self):
        import contextlib
        from unittest import mock
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}), \
                contextlib.redirect_stderr(err):
            code, _ = self._main(
                ["--source", "x", "--punches-since", "2026-09-02 00:00:00"], _source()
            )
        self.assertEqual(code, 2)
        self.assertIn("TURSO_DATABASE_URL", err.getvalue())

    def test_timestamps_keep_ist_wall_clock_in_canonical_format(self):
        migrate.migrate_full(_source(), self.database)
        row = self._raw(
            "SELECT CAST(event_timestamp AS TEXT) AS e, CAST(collected_at AS TEXT) AS c "
            "FROM punches WHERE id=100"
        )[0]
        self.assertEqual(row["e"], "2026-09-01 09:05:07")
        self.assertEqual(row["c"], "2026-09-01 09:06:00")
        device = self._raw("SELECT CAST(connection_updated_at AS TEXT) AS t FROM devices")[0]
        self.assertEqual(device["t"], "2026-08-30 18:30:00")


if __name__ == "__main__":
    unittest.main()
