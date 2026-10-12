from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime

import libsql

from essl_hub import db as db_module
from essl_hub import frappe_sync
from essl_hub.config import DeviceConfig

DEVICES = (
    DeviceConfig("uppal-x990", "Uppal", "X990", "10.0.0.1", 4370, "zk-attendance", "X"),
)


def punch(fp, user, ts, direction):
    return {
        "fingerprint": fp, "device_id": "uppal-x990", "site": "Uppal",
        "device_user_id": user, "event_timestamp": ts, "trusted_direction": direction,
    }


class StubTransport:
    def __init__(self):
        self.calls = []
        self.responses = []  # list of HttpResult | Exception | callable

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, json.loads(body)))
        if not self.responses:
            n = len(self.calls)
            return frappe_sync.HttpResult(200, json.dumps({"message": {"name": f"EMP-CKIN-{n}"}}))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def frappe_error(status, message):
    return frappe_sync.HttpResult(
        status,
        json.dumps({"exception": f"frappe.exceptions.ValidationError: {message}",
                    "_server_messages": json.dumps([json.dumps({"message": message})])}),
    )


class FrappeSyncTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "essl.db")
        self.database = db_module.Database(self.path)
        self.database.initialize(DEVICES)
        self.transport = StubTransport()
        self.config = frappe_sync.FrappeConfig(
            url="https://hr.example.com", api_key="k", api_secret="s", batch_size=2,
        )

    def sync(self, **kw):
        cfg = frappe_sync.FrappeConfig(**{**self.config.__dict__, **kw})
        return frappe_sync.FrappeSync(self.database, cfg, transport=self.transport,
                                      clock=lambda: datetime(2026, 1, 1))

    def add(self, *punches):
        self.database.record_poll("uppal-x990", punches=list(punches), status="connected",
                                  transport="tcp", serial_number="S", firmware="F", records_seen=len(punches))

    def test_mapping_and_request_shape(self):
        self.add(punch("a", "101", "2026-03-02 09:00:00", "entry"),
                 punch("b", "101", "2026-03-02 18:00:00", "exit"),
                 punch("c", "102", "2026-03-02 10:00:00", None))
        report = self.sync().run_once()
        self.assertEqual(report.synced, 3)
        method, url, headers, body = self.transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith(
            "/api/method/hrms.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field"))
        self.assertEqual(headers["Authorization"], "token k:s")
        self.assertEqual(body, {"employee_field_value": "101", "timestamp": "2026-03-02 09:00:00",
                                "device_id": "uppal-x990", "employee_fieldname": "attendance_device_id",
                                "log_type": "IN"})
        bodies = [c[3] for c in self.transport.calls]
        self.assertNotIn("log_type", bodies[1])  # unknown direction omitted
        self.assertEqual(bodies[2]["log_type"], "OUT")

    def test_idempotent(self):
        self.add(punch("a", "101", "2026-03-02 09:00:00", "entry"))
        self.sync().run_once()
        self.sync().run_once()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.database.frappe_sync_counts(), {"synced": 1})

    def test_employee_not_found_skipped_not_retried(self):
        self.add(punch("a", "999", "2026-03-02 09:00:00", "entry"))
        self.transport.responses = [frappe_error(417, "No Employee found for the given employee field value. 'attendance_device_id': 999")]
        report = self.sync().run_once()
        self.assertEqual(report.skipped, 1)
        self.sync().run_once()
        self.assertEqual(len(self.transport.calls), 1)
        with self.database.connect() as c:
            row = c.execute("SELECT status, error FROM frappe_sync_log").fetchone()
        status, error = row["status"], row["error"]
        self.assertEqual(status, "skipped")
        self.assertIn("No Employee found", error)

    def test_duplicate_counts_as_synced(self):
        self.add(punch("a", "101", "2026-03-02 09:00:00", "entry"))
        self.transport.responses = [frappe_error(417, "This employee already has a log with the same timestamp.")]
        self.assertEqual(self.sync().run_once().synced, 1)

    def test_network_error_retries_next_cycle(self):
        self.add(punch("a", "101", "2026-03-02 09:00:00", "entry"))
        self.transport.responses = [OSError("connection refused")]
        report = self.sync().run_once()
        self.assertIn("network", report.retry_error)
        self.assertEqual(self.database.frappe_sync_counts(), {})
        status = json.loads(self.database.setting(frappe_sync.STATUS_SETTING))
        self.assertFalse(status["ok"])
        self.assertEqual(self.sync().run_once().synced, 1)

    def test_server_error_retries(self):
        self.add(punch("a", "101", "2026-03-02 09:00:00", "entry"))
        self.transport.responses = [frappe_sync.HttpResult(502, "bad gateway")]
        self.assertIsNotNone(self.sync().run_once().retry_error)
        self.assertEqual(self.database.frappe_sync_counts(), {})

    def test_batches_drain_all(self):
        self.add(*[punch(str(i), "101", f"2026-03-02 09:0{i}:00", "entry") for i in range(5)])
        self.assertEqual(self.sync().run_once().synced, 5)

    def test_since_filter(self):
        self.add(punch("a", "101", "2026-01-01 09:00:00", "entry"),
                 punch("b", "101", "2026-03-02 09:00:00", "entry"))
        self.assertEqual(self.sync(since=datetime(2026, 3, 1)).run_once().synced, 1)

    def test_shift_type_update(self):
        self.add(punch("a", "101", "2026-03-02 09:00:00", "entry"))
        report = self.sync(shift_types=("General",)).run_once()
        self.assertEqual(report.shift_types_updated, ["General"])
        method, url, _, body = self.transport.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/api/resource/Shift%20Type/General"))
        self.assertIn("last_sync_of_checkin", body)
        # Same poll time: no second PUT.
        self.sync(shift_types=("General",)).run_once()
        self.assertEqual(self.transport.calls[-1][0], "PUT")
        self.assertEqual(sum(1 for c in self.transport.calls if c[0] == "PUT"), 1)

    def test_shift_type_not_updated_while_retrying(self):
        self.add(punch("a", "101", "2026-03-02 09:00:00", "entry"))
        self.transport.responses = [OSError("down")]
        report = self.sync(shift_types=("General",)).run_once()
        self.assertEqual(report.shift_types_updated, [])
        self.assertFalse(any(c[0] == "PUT" for c in self.transport.calls))

    def test_config_from_env(self):
        self.assertIsNone(frappe_sync.config_from_env({}))
        cfg = frappe_sync.config_from_env({"FRAPPE_URL": "https://x/", "FRAPPE_API_KEY": "a",
                                           "FRAPPE_API_SECRET": "b", "FRAPPE_SHIFT_TYPES": "A, B"})
        self.assertEqual(cfg.url, "https://x")
        self.assertEqual(cfg.shift_types, ("A", "B"))
        self.assertEqual(cfg.interval_seconds, 60)

    def test_scheduler_interval(self):
        now = [0.0]
        sched = frappe_sync.SyncScheduler(self.sync(interval_seconds=60), clock=lambda: now[0])
        self.assertIsNotNone(sched.maybe_run())
        now[0] = 30
        self.assertIsNone(sched.maybe_run())
        now[0] = 61
        self.assertIsNotNone(sched.maybe_run())

    # KTD13: with Turso unreachable the push logs and skips, writing nothing.
    def test_offline_run_logs_and_skips_without_writes(self):
        self.add(punch("a", "101", "2026-03-02 09:00:00", "entry"))
        self.database.offline = True
        with self.assertLogs(frappe_sync._logger, level="WARNING"):
            report = self.sync().run_once()
        self.assertEqual(report.synced, 0)
        self.assertIn("unreachable", report.retry_error)
        self.assertEqual(self.transport.calls, [])
        self.database.offline = False
        self.assertIsNone(self.database.setting(frappe_sync.STATUS_SETTING))
        self.assertEqual(self.sync().run_once().synced, 1)

    def test_offline_error_mid_run_stops_without_crashing_the_scheduler(self):
        self.add(punch("a", "101", "2026-03-02 09:00:00", "entry"))

        def offline(*args, **kwargs):
            raise db_module.OfflineError("Turso is unreachable")

        self.database.record_frappe_sync = offline
        scheduler = frappe_sync.SyncScheduler(self.sync())
        with self.assertLogs(frappe_sync._logger, level="WARNING"):
            report = scheduler.maybe_run()
        self.assertIn("unreachable", report.retry_error)


class MigrationTest(unittest.TestCase):
    def test_upgrade_from_v1(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "essl.db")
        conn = libsql.connect(path)
        for statement in db_module.SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        database = db_module.Database(path)
        database.initialize(DEVICES)
        with database.connect() as c:
            version = c.execute("PRAGMA user_version").fetchone()["user_version"]
            table = c.execute("SELECT name FROM sqlite_master WHERE name='frappe_sync_log'").fetchone()
        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertIsNotNone(table)
        self.assertEqual(database.frappe_sync_counts(), {})


if __name__ == "__main__":
    unittest.main()

