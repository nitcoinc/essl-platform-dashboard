from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from essl_hub.config import DeviceConfig
from essl_hub import db as db_module
from essl_hub.hashing import sha256_hex


V7_DEVICE_COLUMNS = (
    "duplicate_window_seconds",
    "door_denied_since",
    "door_params",
    "door_params_read_at",
    "door_params_previous",
    "door_params_previous_for",
    "door_params_request",
    "door_params_requested_at",
    "door_params_result",
    "policy_updated_at",
)

V8_TABLES = (
    "attendance_baselines",
    "attendance_alerts",
    "mail_outbox",
    "fix_report_links",
    "fix_requests",
    "fix_request_lines",
    "punch_corrections",
    "fix_request_events",
)


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "essl.db")
        self.database = db_module.Database(self.path)
        self.devices = (
            DeviceConfig(
                id="uppal-x990",
                site="Uppal",
                model="X990",
                ip="192.168.1.201",
                port=4370,
                protocol="zk-attendance",
                password_env="ESSL_X990_PASSWORD",
            ),
            DeviceConfig(
                id="gachibowli-c3-400",
                site="Gachibowli",
                model="C3-400",
                ip="192.168.1.202",
                port=4370,
                protocol="c3-pullsdk",
                password_env="ESSL_C3_PASSWORD",
            ),
        )
        self.database.initialize(self.devices)

    def _punch(self, **overrides) -> dict:
        base = {
            "fingerprint": "fp-1",
            "device_id": "gachibowli-c3-400",
            "site": "Gachibowli",
            "device_serial": "DEMO00000001",
            "device_user_id": "118",
            "employee_name": "Ravi Kumar",
            "event_timestamp": datetime(2026, 9, 10, 9, 5, 0),
            "raw_state": "0",
            "door": "1",
            "reader": "",
            "raw_direction_byte": 0,
            "trusted_direction": "entry",
            "raw": {"pin": "118"},
        }
        base.update(overrides)
        return base

    def test_initialize_creates_every_table_and_is_idempotent(self) -> None:
        self.database.initialize(self.devices)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
        names = {row["name"] for row in rows}
        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertEqual(
            names,
            {
                "devices",
                "employees",
                "punches",
                "sessions",
                "login_codes",
                "api_keys",
                "settings",
                "frappe_sync_log",
                "branding_assets",
                "employee_device_ids",
                "device_changes",
                *V8_TABLES,
            },
        )

    def test_bulk_insert_batches_and_counts_only_new_rows(self) -> None:
        self.database.insert_punches([self._punch(fingerprint="f5")])
        punches = [self._punch(fingerprint=f"f{i}") for i in range(250)]
        punches.append(self._punch(fingerprint="f7"))  # duplicate inside the call
        statements: list[str] = []
        with self.database.connect() as connection:
            connection.set_trace_callback(statements.append)
            try:
                inserted = self.database.insert_punches(punches)
            finally:
                connection.set_trace_callback(None)
            count = connection.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"]
        self.assertEqual(inserted, 249)
        self.assertEqual(count, 250)
        inserts = [s for s in statements if s.startswith("INSERT INTO punches")]
        self.assertEqual(len(inserts), 3)  # 251 rows / 100 per statement

    def test_device_id_summary_uses_latest_punch_name(self) -> None:
        # "Zed" sorts after "Bala", so MAX(name) would pick the old one.
        self.database.insert_punches([
            self._punch(fingerprint="fp-old", employee_name="Zed Old", event_timestamp=datetime(2026, 9, 1, 9, 0)),
            self._punch(fingerprint="fp-new", employee_name="Bala New", event_timestamp=datetime(2026, 9, 10, 9, 0)),
        ])
        (row,) = self.database.device_id_summary()
        self.assertEqual(row["device_name"], "Bala New")
        self.assertEqual(row["punch_count"], 2)
        self.assertEqual(row["first_seen"], datetime(2026, 9, 1, 9, 0))
        self.assertEqual(row["last_seen"], datetime(2026, 9, 10, 9, 0))

    def test_duplicate_fingerprint_inserts_once(self) -> None:
        inserted_first = self.database.insert_punches([self._punch()])
        inserted_second = self.database.insert_punches([self._punch()])
        self.assertEqual(inserted_first, 1)
        self.assertEqual(inserted_second, 0)
        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_punch_preserves_raw_payload_byte_for_byte(self) -> None:
        self.database.insert_punches([self._punch(raw={"pin": "118", "card_no": "2332136"})])
        with self.database.connect() as connection:
            row = connection.execute("SELECT raw_json FROM punches").fetchone()
        self.assertEqual(row["raw_json"], {"pin": "118", "card_no": "2332136"})

    def test_punch_stores_trusted_direction(self) -> None:
        self.database.insert_punches([self._punch(trusted_direction="exit")])
        with self.database.connect() as connection:
            row = connection.execute("SELECT trusted_direction FROM punches").fetchone()
        self.assertEqual(row["trusted_direction"], "exit")

    def test_punch_with_no_trusted_direction_stores_null(self) -> None:
        self.database.insert_punches([self._punch(trusted_direction=None)])
        with self.database.connect() as connection:
            row = connection.execute("SELECT trusted_direction FROM punches").fetchone()
        self.assertIsNone(row["trusted_direction"])

    def test_unopenable_path_fails_with_a_plain_error(self) -> None:
        broken = db_module.Database(os.path.join(self.path, "nested", "missing.db"))
        with self.assertRaises(ValueError):
            with broken.connect() as connection:
                connection.execute("SELECT 1")

    def test_default_daily_target_is_eight_hours(self) -> None:
        self.assertEqual(self.database.setting("daily_target_minutes"), "480")

    def test_setting_round_trips(self) -> None:
        self.database.set_setting("daily_target_minutes", "420")
        self.assertEqual(self.database.setting("daily_target_minutes"), "420")

    def test_unmapped_device_user_id_appears_with_its_punch_count(self) -> None:
        self.database.insert_punches(
            [self._punch(fingerprint="fp-1"), self._punch(fingerprint="fp-2")]
        )
        unmapped = self.database.unmapped_device_user_ids()
        self.assertEqual(unmapped, [{"device_user_id": "118", "punch_count": 2}])
        self.database.upsert_employee(
            device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com"
        )
        self.assertEqual(self.database.unmapped_device_user_ids(), [])

    def test_initialize_does_not_overwrite_existing_device_connection_columns(self) -> None:
        moved = (
            DeviceConfig(
                id="uppal-x990",
                site="Uppal",
                model="X990",
                ip="192.168.1.99",
                port=5000,
                protocol="zk-attendance",
                password_env="ESSL_X990_PASSWORD",
            ),
            DeviceConfig(
                id="gachibowli-c3-400",
                site="Gachibowli",
                model="C3-400",
                ip="192.168.1.100",
                port=5001,
                protocol="c3-pullsdk",
                password_env="ESSL_C3_PASSWORD",
            ),
        )
        self.database.initialize(moved)
        rows = {row["id"]: row for row in self.database.devices()}
        self.assertEqual(rows["uppal-x990"]["ip"], "192.168.1.201")
        self.assertEqual(rows["uppal-x990"]["port"], 4370)
        self.assertEqual(rows["gachibowli-c3-400"]["ip"], "192.168.1.202")
        self.assertEqual(rows["gachibowli-c3-400"]["port"], 4370)

    def test_new_device_is_inserted_with_devices_ip_port_and_null_connection_updated_at(self) -> None:
        row = self.database.device_by_id("uppal-x990")
        self.assertEqual(row["ip"], "192.168.1.201")
        self.assertEqual(row["port"], 4370)
        self.assertIsNone(row["connection_updated_at"])

    def test_update_device_persists_and_survives_initialize(self) -> None:
        self.database.update_device(
            "uppal-x990", ip="192.168.1.7", port=5000, comm_password="secret"
        )
        row = self.database.device_by_id("uppal-x990")
        self.assertEqual((row["ip"], row["port"], row["comm_password"]), ("192.168.1.7", 5000, "secret"))
        self.assertIsNotNone(row["connection_updated_at"])
        self.database.initialize(self.devices)
        row = self.database.device_by_id("uppal-x990")
        self.assertEqual((row["ip"], row["port"], row["comm_password"]), ("192.168.1.7", 5000, "secret"))

    def test_update_device_with_none_clears_comm_password(self) -> None:
        self.database.update_device(
            "uppal-x990", ip="192.168.1.201", port=4370, comm_password="secret"
        )
        self.database.update_device(
            "uppal-x990", ip="192.168.1.201", port=4370, comm_password=None
        )
        self.assertIsNone(self.database.device_by_id("uppal-x990")["comm_password"])

    def test_device_by_id_returns_none_for_unknown_id(self) -> None:
        self.assertIsNone(self.database.device_by_id("does-not-exist"))

    def test_device_connection_info_returns_only_connection_columns(self) -> None:
        rows = self.database.device_connection_info()
        self.assertEqual(len(rows), len(self.devices))
        for row in rows:
            self.assertEqual(
                set(row.keys()),
                {"id", "site", "model", "protocol", "ip", "port", "comm_password",
                 "connection_updated_at", "duplicate_window_seconds", "door_denied_since",
                 "door_params_request", "door_params_requested_at"},
            )

    def _retire(self, device_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE devices SET retired_at=? WHERE id=?", (db_module._now(), device_id)
            )

    def test_device_connection_info_skips_retired_devices(self) -> None:
        self._retire("gachibowli-c3-400")
        self.assertEqual(
            [row["id"] for row in self.database.device_connection_info()], ["uppal-x990"]
        )

    def test_seed_keeps_edited_site_model_ip_and_retirement(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE devices SET site='Uppal HQ', model='X990-B', ip='10.1.1.1', "
                "retired_at='2026-10-12 09:00:00' WHERE id='uppal-x990'"
            )
        self.database.initialize(self.devices)
        row = self.database.device_by_id("uppal-x990")
        self.assertEqual(
            (row["site"], row["model"], row["ip"]), ("Uppal HQ", "X990-B", "10.1.1.1")
        )
        self.assertIsNotNone(row["retired_at"])

    def test_seed_inserts_missing_static_devices_with_created_at(self) -> None:
        row = self.database.device_by_id("uppal-x990")
        self.assertIsNotNone(row["created_at"])
        self.assertIsNone(row["retired_at"])

    def test_devices_order_by_site_then_created_time(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO devices (id, site, model, ip, port, protocol, created_at) "
                "VALUES ('aaa-c3', 'Uppal', 'C3-400', '10.0.0.3', 4370, 'c3-pullsdk', "
                "'2999-01-01 00:00:00')"
            )
        self.assertEqual(
            [row["id"] for row in self.database.devices()],
            ["gachibowli-c3-400", "uppal-x990", "aaa-c3"],
        )

    def test_min_device_last_success_ignores_retired_devices(self) -> None:
        old = datetime(2026, 1, 1, 9, 0, 0)
        recent = datetime(2026, 10, 12, 9, 0, 0)
        with self.database.transaction() as connection:
            connection.execute("UPDATE devices SET last_success=? WHERE id='uppal-x990'", (recent,))
            connection.execute(
                "UPDATE devices SET last_success=? WHERE id='gachibowli-c3-400'", (old,)
            )
        self.assertEqual(self.database.min_device_last_success(), old)
        self._retire("gachibowli-c3-400")
        self.assertEqual(self.database.min_device_last_success(), recent)

    def test_v3_to_v4_adds_device_columns_keeps_rows_and_reruns_safely(self) -> None:
        self.database.close()
        path = os.path.join(self._tmp.name, "v3.db")
        old = db_module.Database(path)
        self.addCleanup(old.close)
        with mock.patch.object(db_module, "SCHEMA_VERSION", 3), \
                mock.patch.dict(db_module.MIGRATIONS, {}, clear=False):
            db_module.MIGRATIONS.pop(4)
            old.initialize(self.devices[:0])
            with old.transaction() as connection:
                connection.execute(
                    "INSERT INTO devices (id, site, model, ip, port, protocol) "
                    "VALUES ('uppal-x990', 'Uppal', 'X990', '10.2.2.2', 4370, 'zk-attendance')"
                )
        with old.connect() as connection:
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(devices)").fetchall()}
        self.assertNotIn("retired_at", columns)
        old.migrate_schema()
        with old.transaction() as connection:  # simulate a re-run of step 4
            connection.execute("UPDATE settings SET value='3' WHERE key='schema_version'")
            connection.execute("PRAGMA user_version=3")
        old.migrate_schema()
        with old.connect() as connection:
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(devices)").fetchall()}
            version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
        self.assertTrue({"retired_at", "created_at"} <= columns)
        self.assertEqual(version, db_module.SCHEMA_VERSION)
        row = old.device_by_id("uppal-x990")
        self.assertEqual(row["ip"], "10.2.2.2")
        self.assertIsNone(row["retired_at"])

    def test_v4_to_v5_adds_branding_assets_and_reruns_safely(self) -> None:
        self.database.close()
        path = os.path.join(self._tmp.name, "v4.db")
        old = db_module.Database(path)
        self.addCleanup(old.close)
        with mock.patch.object(db_module, "SCHEMA_VERSION", 4), \
                mock.patch.dict(db_module.MIGRATIONS, {}, clear=False):
            db_module.MIGRATIONS.pop(5)
            old.initialize(self.devices[:0])
        with old.connect() as connection:
            tables = {r["name"] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertNotIn("branding_assets", tables)
        old.migrate_schema()
        with old.transaction() as connection:  # simulate a re-run of step 5
            connection.execute("UPDATE settings SET value='4' WHERE key='schema_version'")
            connection.execute("PRAGMA user_version=4")
        old.migrate_schema()
        with old.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
        self.assertEqual(version, db_module.SCHEMA_VERSION)
        with old.connect() as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='branding_assets'").fetchone()
        self.assertIsNotNone(row)

    def test_v5_to_v6_adds_archive_and_extra_ids_and_reruns_safely(self) -> None:
        self.database.close()
        path = os.path.join(self._tmp.name, "v5.db")
        old = db_module.Database(path)
        self.addCleanup(old.close)
        with mock.patch.object(db_module, "SCHEMA_VERSION", 5), \
                mock.patch.dict(db_module.MIGRATIONS, {}, clear=False):
            db_module.MIGRATIONS.pop(6)
            old.initialize(self.devices[:0])
            old.upsert_employee(device_user_id="118", name="Asha", email="asha@x.com")
        with old.connect() as connection:
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(employees)").fetchall()}
        self.assertNotIn("archived_at", columns)
        old.migrate_schema()
        with old.transaction() as connection:  # simulate a re-run of step 6
            connection.execute("UPDATE settings SET value='5' WHERE key='schema_version'")
            connection.execute("PRAGMA user_version=5")
        old.migrate_schema()
        old.migrate_schema()
        with old.connect() as connection:
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(employees)").fetchall()}
            version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
            marker = connection.execute(
                "SELECT value FROM settings WHERE key='schema_version'").fetchone()["value"]
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='employee_device_ids'"
            ).fetchone()
        self.assertIn("archived_at", columns)
        self.assertIsNotNone(table)
        self.assertEqual((version, marker), (db_module.SCHEMA_VERSION, str(db_module.SCHEMA_VERSION)))
        self.assertIsNone(old.employee_by_device_user_id("118")["archived_at"])

    def test_fresh_database_has_v6_schema(self) -> None:
        with self.database.connect() as connection:
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(employees)").fetchall()}
            extra = {r["name"] for r in connection.execute(
                "PRAGMA table_info(employee_device_ids)").fetchall()}
        self.assertIn("archived_at", columns)
        self.assertEqual(extra, {"device_user_id", "employee_id", "created_at"})

    def test_v6_to_v7_adds_policy_door_state_and_audit_and_reruns_safely(self) -> None:
        self.database.close()
        path = os.path.join(self._tmp.name, "v6.db")
        old = db_module.Database(path)
        self.addCleanup(old.close)
        with mock.patch.object(db_module, "SCHEMA_VERSION", 6), \
                mock.patch.dict(db_module.MIGRATIONS, {}, clear=False):
            db_module.MIGRATIONS.pop(7)
            old.initialize(self.devices)
        with old.connect() as connection:
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(devices)").fetchall()}
        self.assertNotIn("duplicate_window_seconds", columns)
        old.migrate_schema()
        with old.transaction() as connection:  # simulate a re-run of step 7
            connection.execute("UPDATE settings SET value='6' WHERE key='schema_version'")
            connection.execute("PRAGMA user_version=6")
        old.migrate_schema()
        old.migrate_schema()
        with old.connect() as connection:
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(devices)").fetchall()}
            version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_changes'"
            ).fetchone()
        self.assertTrue(set(V7_DEVICE_COLUMNS) <= columns)
        self.assertIsNotNone(table)
        self.assertEqual(version, db_module.SCHEMA_VERSION)
        for device in old.devices():
            self.assertEqual(device["duplicate_window_seconds"], 60)
            self.assertIsNone(device["door_denied_since"])
        self.assertEqual(old.device_windows(), {"uppal-x990": 60, "gachibowli-c3-400": 60})

    def test_fresh_database_has_v7_schema(self) -> None:
        with self.database.connect() as connection:
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(devices)").fetchall()}
            changes = {r["name"] for r in connection.execute(
                "PRAGMA table_info(device_changes)").fetchall()}
        self.assertTrue(set(V7_DEVICE_COLUMNS) <= columns)
        self.assertEqual(
            changes,
            {"id", "device_id", "actor_employee_id", "kind", "old_value", "new_value", "created_at"},
        )

    def test_device_policy_window_range(self) -> None:
        for bad in (-1, 301, True, "10"):
            with self.assertRaises(ValueError):
                self.database.set_device_policy(
                    "uppal-x990", window_seconds=bad, wait_for_door_close=True)
        self.assertEqual(self.database.device_windows()["uppal-x990"], 60)
        for good in (0, 300):
            self.assertTrue(self.database.set_device_policy(
                "uppal-x990", window_seconds=good, wait_for_door_close=True))
            self.assertEqual(self.database.device_windows()["uppal-x990"], good)
        self.assertFalse(self.database.set_device_policy(
            "missing", window_seconds=10, wait_for_door_close=True))

    def test_device_policy_door_close_and_audit_row(self) -> None:
        admin = self._employee("1", "admin@x.com")
        c3 = "gachibowli-c3-400"
        self.database.set_device_policy(c3, window_seconds=10, wait_for_door_close=False, actor_id=admin)
        device = self.database.device_by_id(c3)
        since = device["door_denied_since"]
        self.assertIsNotNone(since)
        self.assertIsNotNone(device["policy_updated_at"])
        # Still unchecked: the start of capture does not move.
        with self.database.transaction() as connection:
            connection.execute("UPDATE devices SET door_denied_since=? WHERE id=?",
                               (datetime(2026, 10, 1, 9, 0), c3))
        self.database.set_device_policy(c3, window_seconds=10, wait_for_door_close=False, actor_id=admin)
        self.assertEqual(self.database.device_by_id(c3)["door_denied_since"], datetime(2026, 10, 1, 9, 0))
        self.database.set_device_policy(c3, window_seconds=5, wait_for_door_close=True, actor_id=admin)
        self.assertIsNone(self.database.device_by_id(c3)["door_denied_since"])
        changes = self.database.device_changes(c3)
        self.assertEqual(len(changes), 3)
        self.assertEqual(changes[0]["kind"], "policy")
        self.assertEqual(changes[0]["actor_employee_id"], admin)
        self.assertEqual(changes[0]["actor_name"], "admin@x.com")
        self.assertEqual(changes[0]["old_value"],
                         {"duplicate_window_seconds": 10, "wait_for_door_close": False})
        self.assertEqual(changes[0]["new_value"],
                         {"duplicate_window_seconds": 5, "wait_for_door_close": True})
        self.assertEqual(changes[-1]["old_value"],
                         {"duplicate_window_seconds": 60, "wait_for_door_close": True})

    def test_policy_rolls_back_with_failed_audit_row(self) -> None:
        with mock.patch.object(db_module, "_insert_device_change", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.database.set_device_policy(
                    "uppal-x990", window_seconds=5, wait_for_door_close=True)
        self.assertEqual(self.database.device_windows()["uppal-x990"], 60)

    def test_connection_info_carries_policy_and_request(self) -> None:
        t1 = datetime(2026, 10, 26, 10, 0, 0)
        self.database.file_door_request("gachibowli-c3-400", {"Door1Intertime": 0}, requested_at=t1)
        info = {row["id"]: row for row in self.database.device_connection_info()}
        c3 = info["gachibowli-c3-400"]
        self.assertEqual(c3["duplicate_window_seconds"], 60)
        self.assertIsNone(c3["door_denied_since"])
        self.assertEqual(c3["door_params_request"], {"Door1Intertime": 0})
        self.assertEqual(c3["door_params_requested_at"], t1)

    def test_door_request_replaced_then_guarded_clear_keeps_newer(self) -> None:
        c3 = "gachibowli-c3-400"
        t1 = datetime(2026, 10, 26, 10, 0, 0)
        t2 = datetime(2026, 10, 26, 10, 5, 0)
        self.database.file_door_request(c3, {"Door1Intertime": 0}, requested_at=t1)
        self.database.file_door_request(c3, {"Door1Intertime": 2}, requested_at=t2)
        self.assertFalse(self.database.clear_door_request(c3, t1))
        device = self.database.device_by_id(c3)
        self.assertEqual(device["door_params_request"], {"Door1Intertime": 2})
        self.assertEqual(device["door_params_requested_at"], t2)
        self.assertTrue(self.database.clear_door_request(c3, t2))
        device = self.database.device_by_id(c3)
        self.assertIsNone(device["door_params_request"])
        self.assertIsNone(device["door_params_requested_at"])

    def test_door_request_validates_and_audits_old_values(self) -> None:
        c3 = "gachibowli-c3-400"
        for bad in ({"Door1Intertime": "0"}, {"Door1Intertime": True}, {1: 0}):
            with self.assertRaises(ValueError):
                self.database.file_door_request(c3, bad)
        with self.assertRaises(ValueError):
            self.database.file_door_request(c3, {}, kind="policy")
        self.assertIsNone(self.database.device_by_id(c3)["door_params_request"])
        self.database.record_door_params(
            c3, params={"Door1Intertime": 1, "InterLock": 0}, read_at=datetime(2026, 10, 26, 9, 0))
        self.database.file_door_request(c3, {"Door1Intertime": 0})
        self.database.file_door_request(c3, {}, kind="door_read")
        changes = self.database.device_changes(c3)
        self.assertEqual([c["kind"] for c in changes], ["door_read", "door_write"])
        self.assertEqual(changes[1]["old_value"], {"Door1Intertime": 1})
        self.assertEqual(changes[1]["new_value"], {"Door1Intertime": 0})
        self.assertEqual(changes[0]["new_value"], {})

    def test_dismiss_clears_request_and_result_files_nothing(self) -> None:
        c3 = "gachibowli-c3-400"
        self.database.file_door_request(c3, {"Door1Intertime": 0},
                                         requested_at=datetime(2026, 10, 26, 10, 0))
        self.database.record_door_params(
            c3, params={"Door1Intertime": 1}, read_at=datetime(2026, 10, 26, 10, 1),
            result={"status": "failed", "keys": {"Door1Intertime": "panel kept 1"}})
        self.assertTrue(self.database.dismiss_door_request(c3, actor_id=None))
        device = self.database.device_by_id(c3)
        self.assertIsNone(device["door_params_request"])
        self.assertIsNone(device["door_params_requested_at"])
        self.assertIsNone(device["door_params_result"])
        self.assertEqual(device["door_params"], {"Door1Intertime": 1})
        changes = self.database.device_changes(c3)
        self.assertEqual([c["kind"] for c in changes], ["door_dismiss", "door_write"])
        self.assertEqual(changes[0]["old_value"]["request"], {"Door1Intertime": 0})
        self.assertIsNone(changes[0]["new_value"])

    def test_recording_door_result_leaves_policy_untouched(self) -> None:
        c3 = "gachibowli-c3-400"
        self.database.set_device_policy(c3, window_seconds=10, wait_for_door_close=False)
        before = self.database.device_by_id(c3)
        self.database.record_door_params(
            c3, params={"Door1Intertime": 0}, read_at=datetime(2026, 10, 26, 10, 1),
            result={"status": "applied"})
        after = self.database.device_by_id(c3)
        for column in ("duplicate_window_seconds", "door_denied_since", "policy_updated_at"):
            self.assertEqual(after[column], before[column])
        self.assertEqual(after["door_params"], {"Door1Intertime": 0})
        self.assertEqual(after["door_params_read_at"], datetime(2026, 10, 26, 10, 1))
        self.assertEqual(after["door_params_result"], {"status": "applied"})
        # A plain read (no result) keeps the last result.
        self.database.record_door_params(
            c3, params={"Door1Intertime": 0}, read_at=datetime(2026, 10, 26, 16, 0))
        self.assertEqual(self.database.device_by_id(c3)["door_params_result"], {"status": "applied"})

    def test_previous_snapshot_kept_once_per_request(self) -> None:
        c3 = "gachibowli-c3-400"
        t1 = datetime(2026, 10, 26, 10, 0, 0)
        t2 = datetime(2026, 10, 26, 11, 0, 0)
        read = datetime(2026, 10, 26, 10, 1)
        self.database.record_door_params(
            c3, params={"Door1Intertime": 0}, read_at=read,
            previous={"Door1Intertime": 1}, previous_for=t1)
        self.database.record_door_params(
            c3, params={"Door1Intertime": 0}, read_at=read,
            previous={"Door1Intertime": 0}, previous_for=t1)
        device = self.database.device_by_id(c3)
        self.assertEqual(device["door_params_previous"], {"Door1Intertime": 1})
        self.assertEqual(device["door_params_previous_for"], t1)
        # Reads without a request never touch the snapshot.
        self.database.record_door_params(c3, params={"Door1Intertime": 5}, read_at=read)
        self.assertEqual(self.database.device_by_id(c3)["door_params_previous"], {"Door1Intertime": 1})
        self.database.record_door_params(
            c3, params={"Door1Intertime": 3}, read_at=read,
            previous={"Door1Intertime": 5}, previous_for=t2)
        device = self.database.device_by_id(c3)
        self.assertEqual(device["door_params_previous"], {"Door1Intertime": 5})
        self.assertEqual(device["door_params_previous_for"], t2)

    def test_record_device_change_appends_and_rejects_unknown_kind(self) -> None:
        self.database.record_device_change(
            "uppal-x990", kind="policy", old_value=None, new_value={"x": 1})
        self.assertEqual(len(self.database.device_changes("uppal-x990")), 1)
        with self.assertRaises(ValueError):
            self.database.record_device_change(
                "uppal-x990", kind="other", old_value=None, new_value=None)

    def _employee(self, device_user_id: str, email: str) -> int:
        self.database.upsert_employee(device_user_id=device_user_id, name=email, email=email)
        return int(self.database.employee_by_device_user_id(device_user_id)["id"])

    def test_extra_device_ids_add_list_remove_and_owner(self) -> None:
        asha = self._employee("118", "asha@x.com")
        self.database.add_extra_device_id(asha, "NP0118")
        self.assertEqual(self.database.extra_device_ids(asha), ["NP0118"])
        self.assertEqual(self.database.device_user_id_owner("NP0118")["id"], asha)
        self.assertEqual(self.database.device_user_id_owner("118")["id"], asha)
        self.assertIsNone(self.database.device_user_id_owner("999"))
        self.assertTrue(self.database.remove_extra_device_id(asha, "NP0118"))
        self.assertEqual(self.database.extra_device_ids(asha), [])

    def test_deleting_employee_cascades_extra_ids(self) -> None:
        asha = self._employee("118", "asha@x.com")
        self.database.add_extra_device_id(asha, "NP0118")
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM employees WHERE id = ?", (asha,))
        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM employee_device_ids").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_device_id_unique_across_main_and_extra(self) -> None:
        asha = self._employee("118", "asha@x.com")
        ravi = self._employee("200", "ravi@x.com")
        self.database.add_extra_device_id(asha, "NP0118")
        with self.assertRaises(db_module.IntegrityError):
            self.database.add_extra_device_id(ravi, "200")  # someone's main ID
        with self.assertRaises(db_module.IntegrityError):
            self.database.add_extra_device_id(ravi, "NP0118")  # someone's extra ID
        with self.assertRaises(db_module.IntegrityError):
            self.database.update_employee(ravi, name="Ravi", email="ravi@x.com", device_user_id="NP0118")
        with self.assertRaises(db_module.IntegrityError):
            self.database.upsert_employee(device_user_id="NP0118", name="New", email="new@x.com")
        with self.assertRaises(db_module.IntegrityError):
            self.database.create_first_admin(name="Boss", email="boss@x.com", device_user_id="NP0118")
        # Unrelated upsert of an existing main ID still works.
        self.assertEqual(
            self.database.upsert_employee(device_user_id="118", name="Asha K", email="asha@x.com"), "updated"
        )

    def test_extra_ids_resolve_in_lookups_punches_and_unmapped(self) -> None:
        asha = self._employee("NP0118", "asha@x.com")
        self.database.insert_punches([
            self._punch(fingerprint="a", device_user_id="NP0118"),
            self._punch(fingerprint="b", device_user_id="118", event_timestamp=datetime(2026, 9, 10, 18, 0)),
        ])
        self.assertEqual([r["device_user_id"] for r in self.database.unmapped_device_user_ids()], ["118"])
        self.database.add_extra_device_id(asha, "118")
        self.assertEqual(self.database.unmapped_device_user_ids(), [])
        person = self.database.employee_by_device_user_id("118")
        self.assertEqual((person["id"], person["device_ids"]), (asha, ["NP0118", "118"]))
        self.assertEqual(self.database.employee_by_id(asha)["device_ids"], ["NP0118", "118"])
        self.assertEqual(self.database.employees()[0]["device_ids"], ["NP0118", "118"])
        self.assertEqual(db_module.device_ids_of({"device_user_id": "7"}), ["7"])
        rows = self.database.punches_for_employee_range(
            db_module.device_ids_of(person), datetime(2026, 9, 10), datetime(2026, 9, 11)
        )
        self.assertEqual(len(rows), 2)

    def test_frappe_and_checkins_send_main_id_and_requeue_skipped_extra_punches(self) -> None:
        asha = self._employee("NP0118", "asha@x.com")
        self.database.insert_punches([self._punch(fingerprint="b", device_user_id="118")])
        punch_id = self.database.frappe_pending_punches(since=None, limit=10)[0]["id"]
        self.database.record_frappe_sync(punch_id, status="skipped", error="Employee not found")
        self.assertEqual(self.database.frappe_pending_punches(since=None, limit=10), [])
        self.database.add_extra_device_id(asha, "118")
        pending = self.database.frappe_pending_punches(since=None, limit=10)
        self.assertEqual([(p["id"], p["device_user_id"]) for p in pending], [(punch_id, "NP0118")])
        self.assertEqual(
            [r["device_user_id"] for r in self.database.checkins_since(since=None, limit=10)], ["NP0118"]
        )

    def test_frappe_skips_door_denied_punches(self) -> None:
        self.database.insert_punches([
            self._punch(fingerprint="a", device_user_id="118"),
            self._punch(fingerprint="b", device_user_id="118", raw_state="event 25 · in/out 1 · door denied: interlock",
                        event_timestamp=datetime(2026, 9, 10, 18, 0)),
        ])
        pending = self.database.frappe_pending_punches(since=None, limit=10)
        self.assertEqual([p["event_timestamp"] for p in pending], [datetime(2026, 9, 10, 9, 5, 0)])

    def test_frappe_holds_a_guessed_realtime_direction_until_history_confirms(self) -> None:
        from unittest.mock import patch

        realtime = {"pin": "118", "in_out_state": 15, "source": "realtime_fallback"}
        guess = self._punch(fingerprint="rt", trusted_direction="exit", raw_direction_byte=15, raw=realtime)
        self.database.insert_punches([guess])
        soon = guess["event_timestamp"] + timedelta(hours=1)
        with patch.object(db_module, "_now", return_value=soon):
            self.assertEqual(self.database.frappe_pending_punches(since=None, limit=10), [])
            self.database.fill_punch_directions([
                self._punch(fingerprint="rt", trusted_direction="exit", raw_direction_byte=1),
            ])
            self.assertEqual(len(self.database.frappe_pending_punches(since=None, limit=10)), 1)

    def test_frappe_sends_an_unconfirmed_guess_after_the_hold(self) -> None:
        from unittest.mock import patch

        realtime = {"pin": "118", "in_out_state": 15, "source": "realtime_fallback"}
        guess = self._punch(fingerprint="rt", trusted_direction="exit", raw_direction_byte=15, raw=realtime)
        self.database.insert_punches([guess])
        late = guess["event_timestamp"] + db_module.FRAPPE_GUESS_HOLD + timedelta(minutes=1)
        with patch.object(db_module, "_now", return_value=late):
            self.assertEqual(len(self.database.frappe_pending_punches(since=None, limit=10)), 1)

    def test_main_id_change_requeues_skipped_punches(self) -> None:
        asha = self._employee("asha@x.com", "asha@x.com")
        self.database.insert_punches([self._punch(fingerprint="b", device_user_id="777")])
        punch_id = self.database.frappe_pending_punches(since=None, limit=10)[0]["id"]
        self.database.record_frappe_sync(punch_id, status="skipped", error="Employee not found")
        self.database.update_employee(asha, name="Asha", email="asha@x.com", device_user_id="777")
        pending = self.database.frappe_pending_punches(since=None, limit=10)
        self.assertEqual([(p["id"], p["device_user_id"]) for p in pending], [(punch_id, "777")])

    def test_archive_hides_person_and_restore_brings_them_back(self) -> None:
        asha = self._employee("118", "asha@x.com")
        self.database.add_extra_device_id(asha, "NP0118")
        before = self.database.employee_by_id(asha)
        self.database.archive_employee(asha)
        self.assertIsNone(self.database.employee_by_id(asha))
        self.assertIsNone(self.database.employee_by_email("asha@x.com"))
        self.assertIsNotNone(self.database.employee_by_email("asha@x.com", include_archived=True))
        self.assertEqual(self.database.employees(), [])
        self.assertEqual([e["id"] for e in self.database.employees(include_archived=True)], [asha])
        archived = self.database.archived_employees()
        self.assertEqual(archived[0]["device_ids"], ["118", "NP0118"])
        self.assertIsInstance(archived[0]["archived_at"], datetime)
        # Their IDs stay mapped: punches never resurface as unknown.
        self.database.insert_punches([self._punch(device_user_id="NP0118")])
        self.assertEqual(self.database.unmapped_device_user_ids(), [])
        self.assertEqual(self.database.employee_by_device_user_id("NP0118")["id"], asha)
        self.assertTrue(self.database.restore_employee(asha))
        self.assertFalse(self.database.restore_employee(asha))
        after = self.database.employee_by_id(asha)
        for row in (before, after):
            row.pop("archived_at"), row.pop("updated_at")
        self.assertEqual(before, after)

    def test_archived_with_punches_only_in_range(self) -> None:
        asha = self._employee("118", "asha@x.com")
        self.database.insert_punches([self._punch(event_timestamp=datetime(2026, 9, 10, 9, 0))])
        self.database.archive_employee(asha)
        inside = self.database.archived_employees_with_punches(datetime(2026, 9, 10), datetime(2026, 9, 11))
        outside = self.database.archived_employees_with_punches(datetime(2026, 9, 11), datetime(2026, 9, 12))
        self.assertEqual([e["id"] for e in inside], [asha])
        self.assertEqual(outside, [])
        self.assertIsInstance(self.database.archived_employees()[0]["last_punch"], datetime)

    def test_archive_guards_last_admin_and_direct_reports(self) -> None:
        boss = self._employee("1", "boss@x.com")
        worker = self._employee("2", "worker@x.com")
        self.database.set_employee_role(boss, "admin")
        with self.assertRaisesRegex(db_module.RuleViolation, "last admin"):
            self.database.archive_employee(boss)
        self.database.set_employee_role(worker, "admin")
        self.database.set_employee_manager(worker, boss)
        with self.assertRaisesRegex(db_module.RuleViolation, "1 person reports"):
            self.database.archive_employee(boss)
        self.database.set_employee_manager(worker, None)
        self.database.archive_employee(boss)
        # Archived admins don't count: the remaining admin is now the last.
        self.assertTrue(self.database.has_admin())
        with self.assertRaises(db_module.RuleViolation):
            self.database.set_employee_role(worker, "employee")

    def test_archive_ends_sessions(self) -> None:
        asha = self._employee("118", "asha@x.com")
        self.database.create_session(employee_id=asha, token_hash="t", csrf_token="c",
                                     expires_at=datetime.now() + timedelta(days=1))
        self.assertIsNotNone(self.database.session_employee("t"))
        self.database.archive_employee(asha)
        self.database.restore_employee(asha)
        self.assertIsNone(self.database.session_employee("t"))

    def test_record_poll_commits_punches_and_device_row_together(self) -> None:
        inserted = self.database.record_poll(
            "gachibowli-c3-400",
            punches=[self._punch()],
            status="connected",
            transport="PullSDK",
            serial_number="SN-9",
            firmware="FW-9",
            records_seen=1,
        )
        self.assertEqual(inserted, 1)
        device = self.database.device_by_id("gachibowli-c3-400")
        self.assertEqual(device["status"], "connected")
        self.assertEqual(device["serial_number"], "SN-9")
        self.assertIsNotNone(device["last_attempt"])
        self.assertIsNone(device["last_error"])

    def _traced_statements(self, fn) -> list[str]:
        statements: list[str] = []
        with self.database.connect() as connection:
            connection.set_trace_callback(statements.append)
            try:
                fn()
            finally:
                connection.set_trace_callback(None)
        return statements

    def test_record_poll_skips_punches_already_stored(self) -> None:
        # Devices return their full history each poll; known rows never reach Turso.
        history = [self._punch(fingerprint=f"h{i}") for i in range(5)]
        self.database.insert_punches(history)
        statements = self._traced_statements(
            lambda: self.assertEqual(
                self.database.record_poll(
                    "gachibowli-c3-400", punches=history, status="connected", records_seen=5
                ),
                0,
            )
        )
        self.assertFalse([s for s in statements if s.startswith("INSERT INTO punches")])
        self.assertEqual(self.database.device_by_id("gachibowli-c3-400")["status"], "connected")

    def test_record_poll_commits_large_backlog_in_chunks(self) -> None:
        punches = [self._punch(fingerprint=f"b{i}") for i in range(5)]
        with mock.patch.object(db_module, "PUNCH_TX_ROWS", 2):
            statements = self._traced_statements(
                lambda: self.assertEqual(
                    self.database.record_poll(
                        "gachibowli-c3-400", punches=punches, status="connected", records_seen=5
                    ),
                    5,
                )
            )
        self.assertEqual(statements.count("BEGIN IMMEDIATE"), 3)  # 2 + 2 + (1 + device row)
        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"]
        self.assertEqual(count, 5)
        self.assertEqual(self.database.device_by_id("gachibowli-c3-400")["status"], "connected")

    def test_record_poll_error_sets_status_and_message(self) -> None:
        self.database.record_poll("gachibowli-c3-400", status="error", error="timeout")
        device = self.database.device_by_id("gachibowli-c3-400")
        self.assertEqual(device["status"], "error")
        self.assertEqual(device["last_error"], "timeout")

    def test_record_poll_rolls_back_punches_when_the_device_update_fails(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER fail_update BEFORE UPDATE ON devices "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
        with self.assertRaises(Exception):
            self.database.record_poll(
                "gachibowli-c3-400", punches=[self._punch()], status="connected"
            )
        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_warn_device_drift_logs_warning_for_never_edited_row(self) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE devices SET ip='10.0.0.1' WHERE id='uppal-x990'")
        with self.assertLogs(db_module._logger, level="WARNING") as captured:
            self.database.warn_device_drift(self.devices)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("uppal-x990", captured.records[0].getMessage())

    def test_warn_device_drift_is_silent_after_update_device(self) -> None:
        self.database.update_device(
            "uppal-x990", ip="10.0.0.1", port=4370, comm_password=None
        )
        with mock.patch.object(db_module._logger, "warning") as warning:
            self.database.warn_device_drift(self.devices)
        warning.assert_not_called()

    # synthetic admin identity (R10, R14, R22, R23) -------------------------

    # bootstrap admin --------------------------------------------------------

    def test_ensure_bootstrap_admin_promotes_a_matching_employee(self) -> None:
        self.database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        self.database.ensure_bootstrap_admin("BOSS@example.com")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT role FROM employees WHERE device_user_id='1'"
            ).fetchone()
        self.assertEqual(row["role"], "admin")

    def test_ensure_bootstrap_admin_without_a_match_changes_nothing(self) -> None:
        self.database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        self.database.ensure_bootstrap_admin("ghost@example.com")
        self.assertEqual(self.database.employee_by_email("boss@example.com")["role"], "employee")

    def test_ensure_bootstrap_admin_is_a_noop_when_unset(self) -> None:
        self.database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        self.database.ensure_bootstrap_admin(None)
        self.assertEqual(self.database.employee_by_email("boss@example.com")["role"], "employee")

    def test_ensure_bootstrap_admin_does_not_override_an_existing_admin(self) -> None:
        self.database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        self.database.upsert_employee(device_user_id="2", name="Other", email="other@example.com")
        self.database.ensure_bootstrap_admin("boss@example.com")
        self.database.ensure_bootstrap_admin("other@example.com")
        self.assertEqual(self.database.employee_by_email("boss@example.com")["role"], "admin")
        self.assertEqual(self.database.employee_by_email("other@example.com")["role"], "employee")

    def test_ensure_bootstrap_admin_leaves_a_demoted_admin_demoted(self) -> None:
        self.database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        self.database.upsert_employee(device_user_id="2", name="Other", email="other@example.com")
        self.database.ensure_bootstrap_admin("boss@example.com")
        other = self.database.employee_by_email("other@example.com")
        self.database.set_employee_role(other["id"], "admin")
        boss = self.database.employee_by_email("boss@example.com")
        self.database.set_employee_role(boss["id"], "employee")
        self.database.ensure_bootstrap_admin("boss@example.com")
        self.assertEqual(self.database.employee_by_email("boss@example.com")["role"], "employee")

    def test_has_admin_reflects_the_role_column(self) -> None:
        self.assertFalse(self.database.has_admin())
        self.database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        self.assertFalse(self.database.has_admin())
        boss = self.database.employee_by_email("boss@example.com")
        self.database.set_employee_role(boss["id"], "admin")
        self.assertTrue(self.database.has_admin())

    def test_employee_by_device_user_id_finds_the_row(self) -> None:
        self.database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        row = self.database.employee_by_device_user_id("1")
        self.assertEqual(row["email"], "boss@example.com")
        self.assertIsNone(self.database.employee_by_device_user_id("missing"))

    def test_create_first_admin_inserts_a_new_employee(self) -> None:
        self.database.create_first_admin(
            name="Boss", email="boss@example.com", device_user_id="boss@example.com"
        )
        row = self.database.employee_by_email("boss@example.com")
        self.assertEqual(row["role"], "admin")
        self.assertEqual(row["device_user_id"], "boss@example.com")

    def test_create_first_admin_promotes_an_existing_employee(self) -> None:
        self.database.upsert_employee(device_user_id="7", name="Boss", email="boss@example.com")
        self.database.create_first_admin(
            name="Boss", email="BOSS@example.com", device_user_id="boss@example.com"
        )
        rows = self.database.employees()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "admin")
        self.assertEqual(rows[0]["device_user_id"], "7")

    def test_session_returns_the_employee_with_its_csrf_token(self) -> None:
        self.database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        employee = self.database.employee_by_email("boss@example.com")
        self.database.create_session(
            employee_id=employee["id"],
            token_hash="t1",
            csrf_token="csrf-1",
            expires_at=db_module._now() + timedelta(days=1),
        )
        row = self.database.session_employee("t1")
        self.assertEqual(row["csrf_token"], "csrf-1")
        self.assertEqual(row["email"], "boss@example.com")

    def test_punches_for_employee_range_is_time_ordered(self) -> None:
        base = datetime(2026, 9, 10, 9, 0, 0)
        self.database.insert_punches(
            [
                self._punch(fingerprint="fp-2", event_timestamp=base + timedelta(hours=4)),
                self._punch(fingerprint="fp-1", event_timestamp=base),
            ]
        )
        rows = self.database.punches_for_employee_range(
            "118", base - timedelta(hours=1), base + timedelta(hours=8)
        )
        self.assertEqual([row["event_timestamp"] for row in rows], [base, base + timedelta(hours=4)])

    # SQLite-specific behaviour (U2) ----------------------------------------

    def test_initialize_on_fresh_file_seeds_devices_and_settings(self) -> None:
        self.assertEqual({row["id"] for row in self.database.devices()}, {d.id for d in self.devices})
        self.assertEqual(self.database.setting("daily_target_minutes"), "480")

    def test_create_schema_does_not_seed(self) -> None:
        other = db_module.Database(os.path.join(self._tmp.name, "bare.db"))
        other.create_schema()
        self.assertEqual(other.devices(), [])
        self.assertIsNone(other.setting("daily_target_minutes"))

    def test_datetime_and_string_timestamps_store_canonically(self) -> None:
        moment = datetime(2026, 10, 9, 9, 15, 0)
        self.database.insert_punches(
            [
                self._punch(fingerprint="fp-a", event_timestamp=moment),
                self._punch(fingerprint="fp-b", event_timestamp="2026-10-09 09:15:00"),
            ]
        )
        with self.database.connect() as connection:
            raw = connection.execute(
                "SELECT CAST(event_timestamp AS TEXT) AS t FROM punches ORDER BY fingerprint"
            ).fetchall()
            parsed = connection.execute(
                "SELECT event_timestamp FROM punches ORDER BY fingerprint"
            ).fetchall()
        self.assertEqual([row["t"] for row in raw], ["2026-10-09 09:15:00"] * 2)
        self.assertEqual([row["event_timestamp"] for row in parsed], [moment, moment])

    def test_malformed_timestamp_is_rejected_not_stored(self) -> None:
        inserted = self.database.insert_punches(
            [
                self._punch(fingerprint="fp-bad", event_timestamp="09/10/2026 25:99"),
                self._punch(fingerprint="fp-good"),
            ]
        )
        self.assertEqual(inserted, 1)
        with self.database.connect() as connection:
            rows = connection.execute("SELECT fingerprint FROM punches").fetchall()
        self.assertEqual([row["fingerprint"] for row in rows], ["fp-good"])

    def test_range_query_returns_exactly_one_day(self) -> None:
        self.database.insert_punches(
            [
                self._punch(fingerprint="a", event_timestamp="2026-10-08 23:59:59"),
                self._punch(fingerprint="b", event_timestamp="2026-10-09 00:00:00"),
                self._punch(fingerprint="c", event_timestamp=datetime(2026, 10, 9, 18, 30)),
                self._punch(fingerprint="d", event_timestamp="2026-10-10 00:00:00"),
            ]
        )
        rows = self.database.punches_for_employee_range(
            "118", datetime(2026, 10, 9), datetime(2026, 10, 10)
        )
        self.assertEqual(
            [row["event_timestamp"] for row in rows],
            [datetime(2026, 10, 9), datetime(2026, 10, 9, 18, 30)],
        )

    def test_role_check_allows_manager_and_rejects_unknown(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO employees (device_user_id, name, email, role, created_at, updated_at) "
                "VALUES ('m', 'M', 'm@x.com', 'manager', ?, ?)",
                (db_module._now(), db_module._now()),
            )
            with self.assertRaises(db_module.IntegrityError):
                connection.execute(
                    "INSERT INTO employees (device_user_id, name, email, role, created_at, updated_at) "
                    "VALUES ('o', 'O', 'o@x.com', 'owner', ?, ?)",
                    (db_module._now(), db_module._now()),
                )

    def test_foreign_keys_are_enforced(self) -> None:
        with self.assertRaises(db_module.IntegrityError):
            self.database.insert_punches([self._punch(device_id="no-such-device")])

    def test_manager_id_is_cleared_when_manager_is_deleted(self) -> None:
        self.database.upsert_employee(device_user_id="1", name="Boss", email="boss@x.com")
        self.database.upsert_employee(device_user_id="2", name="Rep", email="rep@x.com")
        boss = self.database.employee_by_device_user_id("1")
        with self.database.connect() as connection:
            connection.execute("UPDATE employees SET manager_id=? WHERE device_user_id='2'", (boss["id"],))
            connection.execute("DELETE FROM employees WHERE id=?", (boss["id"],))
        self.assertIsNone(self.database.employee_by_device_user_id("2")["manager_id"])

    def test_threads_sharing_the_connection_both_write(self) -> None:
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def write(prefix: str) -> None:
            try:
                database = self.database
                barrier.wait()
                for i in range(25):
                    database.insert_punches([self._punch(fingerprint=f"{prefix}-{i}")])
                    database.set_setting(f"{prefix}-key", str(i))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(p,)) for p in ("x", "y")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"]
        self.assertEqual(count, 50)

    def test_roster_duplicate_email_is_a_per_row_rejection(self) -> None:
        from essl_hub.roster import apply_rows

        self.database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        outcomes = apply_rows(self.database, [(1, "Imposter", "boss@example.com", "2")])
        self.assertEqual(outcomes[0].result, "rejected")
        self.assertEqual(len(self.database.employees()), 1)


    # libsql connection layer (U1) -------------------------------------------

    def _two_employees(self) -> tuple[int, int]:
        self.database.upsert_employee(device_user_id="1", name="A", email="a@x.com")
        self.database.upsert_employee(device_user_id="2", name="B", email="b@x.com")
        return (
            self.database.employee_by_device_user_id("1")["id"],
            self.database.employee_by_device_user_id("2")["id"],
        )

    def test_datetime_and_json_round_trip_with_dict_rows(self) -> None:
        moment = datetime(2026, 10, 9, 9, 15, 0)
        self.database.insert_punches([self._punch(event_timestamp=moment, raw={"pin": "118", "n": [1, 2]})])
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT fingerprint, event_timestamp, collected_at, raw_json FROM punches"
            ).fetchone()
        self.assertEqual(list(row), ["fingerprint", "event_timestamp", "collected_at", "raw_json"])
        self.assertEqual(row["event_timestamp"], moment)
        self.assertIsInstance(row["collected_at"], datetime)
        self.assertEqual(row["raw_json"], {"pin": "118", "n": [1, 2]})

    def test_exception_inside_transaction_rolls_back(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.database.transaction() as connection:
                connection.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")
                raise RuntimeError("boom")
        self.assertIsNone(self.database.setting("k"))

    def test_duplicate_email_any_case_raises_integrity_error(self) -> None:
        self.database.upsert_employee(device_user_id="1", name="A", email="a@x.com")
        with self.assertRaises(db_module.IntegrityError):
            self.database.upsert_employee(device_user_id="2", name="B", email="A@X.COM")

    def test_last_admin_cannot_be_demoted_but_one_of_two_can(self) -> None:
        a, b = self._two_employees()
        self.database.set_employee_role(a, "admin")
        with self.assertRaisesRegex(db_module.RuleViolation, "last admin"):
            self.database.set_employee_role(a, "employee")
        self.database.set_employee_role(b, "admin")
        self.database.set_employee_role(a, "employee")
        self.assertEqual(self.database.employee_by_id(a)["role"], "employee")
        with self.assertRaisesRegex(db_module.RuleViolation, "No such employee"):
            self.database.set_employee_role(9999, "hr")

    def test_manager_cycles_are_rejected_and_valid_change_succeeds(self) -> None:
        a, b = self._two_employees()
        self.database.upsert_employee(device_user_id="3", name="C", email="c@x.com")
        c = self.database.employee_by_device_user_id("3")["id"]
        self.database.set_employee_manager(b, a)
        with self.assertRaisesRegex(db_module.RuleViolation, "cycle"):
            self.database.set_employee_manager(a, b)
        self.database.set_employee_manager(c, b)
        with self.assertRaisesRegex(db_module.RuleViolation, "cycle"):
            self.database.set_employee_manager(a, c)
        with self.assertRaisesRegex(db_module.RuleViolation, "own manager"):
            self.database.set_employee_manager(a, a)
        with self.assertRaisesRegex(db_module.RuleViolation, "No such manager"):
            self.database.set_employee_manager(a, 9999)
        self.database.set_employee_manager(c, a)
        self.assertEqual(self.database.employee_by_id(c)["manager_id"], a)
        self.database.set_employee_manager(c, None)
        self.assertIsNone(self.database.employee_by_id(c)["manager_id"])

    def test_orphan_manager_id_insert_is_rejected(self) -> None:
        with self.assertRaises(db_module.IntegrityError):
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO employees (device_user_id, name, email, manager_id, created_at, updated_at) "
                    "VALUES ('9', 'O', 'o@x.com', 4242, ?, ?)",
                    (db_module._now(), db_module._now()),
                )

    def test_migrate_schema_twice_is_a_noop(self) -> None:
        self.database.migrate_schema()
        self.database.migrate_schema()
        with self.database.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
        self.assertEqual(version, db_module.SCHEMA_VERSION)

    def test_settings_schema_version_counts_without_pragma(self) -> None:
        # Files from Turso carry the version only in settings.
        with self.database.transaction() as connection:
            connection.execute("PRAGMA user_version=0")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS backups (id INTEGER PRIMARY KEY)"
            )
        self.database.migrate_schema()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key='schema_version'"
            ).fetchone()
            kept = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='backups'"
            ).fetchone()
        self.assertEqual(row["value"], str(db_module.SCHEMA_VERSION))
        self.assertIsNotNone(kept)  # no re-migration: settings said v3 already

    def test_v2_to_v3_drops_backups_and_rerun_is_noop(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS backups (id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
            )
            connection.execute("DELETE FROM settings WHERE key='schema_version'")
            connection.execute("PRAGMA user_version=2")
        self.database.migrate_schema()
        self.database.migrate_schema()
        with self.database.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='backups'"
            ).fetchone()
        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertIsNone(exists)

    def test_thread_reads_another_threads_committed_write_at_once(self) -> None:
        def write() -> None:
            self.database.set_setting("from-a", "1")

        thread = threading.Thread(target=write)
        thread.start()
        thread.join()
        seen: list[str | None] = []
        reader = threading.Thread(target=lambda: seen.append(self.database.setting("from-a")))
        reader.start()
        reader.join()
        self.assertEqual(seen, ["1"])

    def test_classifier_maps_sample_messages(self) -> None:
        classify = db_module.classify_error
        self.assertIsInstance(classify(ValueError("UNIQUE constraint failed: p.name")), db_module.IntegrityError)
        self.assertIsInstance(classify(ValueError("FOREIGN KEY constraint failed")), db_module.IntegrityError)
        self.assertIsInstance(classify(ValueError("CHECK constraint failed: n>0")), db_module.IntegrityError)
        self.assertIsInstance(
            classify(ValueError("sync error: http dispatch error: error trying to connect")),
            db_module.OfflineError,
        )
        self.assertIsInstance(classify(ValueError("Connection refused (os error 111)")), db_module.OfflineError)
        self.assertIsNone(classify(ValueError("no such table: nope")))


    def test_fill_punch_directions_updates_null_c3_rows_without_inserting(self) -> None:
        self.database.insert_punches([self._punch(trusted_direction=None, raw_direction_byte=15)])
        history = self._punch(trusted_direction="exit", raw_direction_byte=1)
        self.assertEqual(self.database.fill_punch_directions([history]), 1)
        self.assertEqual(self.database.insert_punches([history]), 0)
        with self.database.connect() as connection:
            rows = connection.execute("SELECT trusted_direction, raw_direction_byte FROM punches").fetchall()
        self.assertEqual([(r["trusted_direction"], r["raw_direction_byte"]) for r in rows], [("exit", 1)])
        self.assertEqual(self.database.fill_punch_directions([history]), 0)

    def test_fill_punch_directions_leaves_set_directions_and_x990_rows(self) -> None:
        self.database.insert_punches([
            self._punch(fingerprint="c3-set", trusted_direction="entry"),
            self._punch(fingerprint="x990", device_id="uppal-x990", site="Uppal", trusted_direction=None),
        ])
        updated = self.database.fill_punch_directions([
            self._punch(fingerprint="c3-set", trusted_direction="exit"),
            self._punch(fingerprint="x990", device_id="uppal-x990", trusted_direction="exit"),
            self._punch(fingerprint="unknown", trusted_direction="exit"),
        ])
        self.assertEqual(updated, 0)
        with self.database.connect() as connection:
            rows = {r["fingerprint"]: r["trusted_direction"] for r in connection.execute(
                "SELECT fingerprint, trusted_direction FROM punches").fetchall()}
        self.assertEqual(rows, {"c3-set": "entry", "x990": None})

    def test_fill_punch_directions_overwrites_a_contradicted_realtime_guess(self) -> None:
        # KTD4: realtime 15 was guessed exit; the history byte 0 is authoritative.
        realtime = {"pin": "118", "in_out_state": 15, "source": "realtime_fallback"}
        self.database.insert_punches([
            self._punch(fingerprint="rt", trusted_direction="exit", raw_direction_byte=15, raw=realtime),
            self._punch(fingerprint="rt-ok", trusted_direction="exit", raw_direction_byte=15, raw=realtime),
        ])
        updated = self.database.fill_punch_directions([
            self._punch(fingerprint="rt", trusted_direction="entry", raw_direction_byte=0),
            self._punch(fingerprint="rt-ok", trusted_direction="exit", raw_direction_byte=1),
        ])
        # Both take the real byte; the matching guess is confirmed, not changed.
        self.assertEqual(updated, 2)
        with self.database.connect() as connection:
            rows = {r["fingerprint"]: (r["trusted_direction"], r["raw_direction_byte"]) for r in connection.execute(
                "SELECT fingerprint, trusted_direction, raw_direction_byte FROM punches").fetchall()}
        self.assertEqual(rows, {"rt": ("entry", 0), "rt-ok": ("exit", 1)})
        self.assertEqual(self.database.fill_punch_directions([
            self._punch(fingerprint="rt", trusted_direction="entry", raw_direction_byte=0),
            self._punch(fingerprint="rt-ok", trusted_direction="exit", raw_direction_byte=1),
        ]), 0)

    def test_fill_punch_directions_never_overwrites_a_history_sourced_row(self) -> None:
        self.database.insert_punches([
            self._punch(fingerprint="hist", trusted_direction="exit", raw={"Pin": "118", "InOutState": "1"}),
        ])
        self.assertEqual(self.database.fill_punch_directions([
            self._punch(fingerprint="hist", trusted_direction="entry"),
        ]), 0)
        with self.database.connect() as connection:
            row = connection.execute("SELECT trusted_direction FROM punches").fetchone()
        self.assertEqual(row["trusted_direction"], "exit")


    def test_add_retire_and_reenable_device(self) -> None:
        self.database.initialize(self.devices)
        self.database.add_device(
            device_id="kondapur-c3-400", site="Kondapur", model="C3-400",
            ip="192.168.1.210", port=4370, protocol="c3-pullsdk", comm_password=None,
        )
        added = self.database.device_by_id("kondapur-c3-400")
        self.assertIsNotNone(added["created_at"])
        self.assertIsNotNone(added["connection_updated_at"])
        self.assertEqual(
            [d["id"] for d in self.database.devices_at_address("192.168.1.210", 4370)],
            ["kondapur-c3-400"],
        )
        self.database.set_device_retired("kondapur-c3-400", True)
        self.assertIsNotNone(self.database.device_by_id("kondapur-c3-400")["retired_at"])
        self.assertNotIn(
            "kondapur-c3-400", [d["id"] for d in self.database.device_connection_info()]
        )
        self.database.set_device_retired("kondapur-c3-400", False)
        self.assertIsNone(self.database.device_by_id("kondapur-c3-400")["retired_at"])


DAY = date(2026, 10, 26)


class MissedPunchSchemaTest(unittest.TestCase):
    """Schema v8: alerts, outbox, report links, fix requests (plan U1)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "essl.db")
        self.database = db_module.Database(self.path)
        self.addCleanup(self.database.close)
        self.database.initialize(())
        self.asha = self._person("101", "Asha", "employee")
        self.hari = self._person("102", "Hari", "hr")
        self.hema = self._person("103", "Hema", "hr")
        self.adam = self._person("104", "Adam", "admin")

    def _person(self, uid: str, name: str, role: str) -> int:
        self.database.upsert_employee(device_user_id=uid, name=name, email=f"{name.lower()}@x.com")
        employee_id = self.database.employee_by_device_user_id(uid)["id"]
        self.database.set_employee_role(employee_id, role)
        return employee_id

    def _count(self, table: str) -> int:
        with self.database.connect() as connection:
            return connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def _file(self, *lines, employee=None, day=DAY, source="link"):
        return self.database.file_fix_lines(
            employee or self.asha, day, list(lines), source=source, punch_set_hash="h1")

    @staticmethod
    def _add_in(hour: int = 13, minute: int = 40, note=None) -> dict:
        return {"kind": "add_in", "event_time": datetime(2026, 10, 26, hour, minute), "note": note}

    # --- migration and types ----------------------------------------------

    def test_v7_to_v8_creates_tables_keeps_data_and_reruns_safely(self) -> None:
        path = os.path.join(self._tmp.name, "v7.db")
        old = db_module.Database(path)
        self.addCleanup(old.close)
        device = DeviceConfig(
            id="uppal-x990", site="Uppal", model="X990", ip="10.0.0.1", port=4370,
            protocol="zk-attendance", password_env="ESSL_X990_PASSWORD",
        )
        with mock.patch.object(db_module, "SCHEMA_VERSION", 7), \
                mock.patch.dict(db_module.MIGRATIONS, {}, clear=False):
            db_module.MIGRATIONS.pop(8)
            old.initialize((device,))
            old.insert_punches([{
                "fingerprint": "fp-v7", "device_id": "uppal-x990", "site": "Uppal",
                "device_user_id": "7", "event_timestamp": datetime(2026, 10, 20, 9, 0),
            }])
            old.set_setting("daily_target_minutes", "450")
        with old.connect() as connection:
            names = {r["name"] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertFalse(set(V8_TABLES) & names)
        old.migrate_schema()
        with old.transaction() as connection:  # simulate a re-run of step 8
            connection.execute("UPDATE settings SET value='7' WHERE key='schema_version'")
            connection.execute("PRAGMA user_version=7")
        old.migrate_schema()
        old.migrate_schema()
        with old.connect() as connection:
            names = {r["name"] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
            fingerprints = [r["fingerprint"] for r in connection.execute(
                "SELECT fingerprint FROM punches").fetchall()]
        self.assertTrue(set(V8_TABLES) <= names)
        self.assertEqual(version, 8)
        self.assertEqual(fingerprints, ["fp-v7"])
        self.assertEqual(old.setting("daily_target_minutes"), "450")
        self.assertEqual(old.device_windows(), {"uppal-x990": 60})

    def test_work_day_round_trips_as_a_date_and_datetimes_still_bind_as_timestamps(self) -> None:
        self.assertTrue(self.database.record_alert(
            employee_id=self.asha, work_day=DAY, kind="live", mode="shadow", status="recorded"))
        with self.database.connect() as connection:
            raw = connection.execute(
                "SELECT CAST(work_day AS TEXT) AS day_text, created_at FROM attendance_alerts"
            ).fetchone()
            row = connection.execute("SELECT work_day FROM attendance_alerts").fetchone()
        self.assertEqual(raw["day_text"], "2026-10-26")
        self.assertIsInstance(raw["created_at"], datetime)
        self.assertEqual(type(row["work_day"]), date)
        self.assertEqual(row["work_day"], DAY)
        self.assertEqual(db_module._bind([DAY, datetime(2026, 10, 26, 9, 5)]),
                         ("2026-10-26", "2026-10-26 09:05:00"))

    # --- baselines -------------------------------------------------------------

    def test_baseline_snapshots_latest_and_prune(self) -> None:
        self.assertIsNone(self.database.latest_baseline_at())
        self.assertEqual(self.database.latest_baselines(), {})
        for week in range(6):
            snapshot = self.database.record_baselines([
                {"employee_id": self.asha, "punched_days": 20 + week, "clean_days": 12,
                 "metrics": {"limit_minutes": 90 + week}},
                {"employee_id": self.hari, "punched_days": 3, "clean_days": 1, "metrics": None},
            ])
            self.assertEqual(snapshot, week + 1)
        latest = self.database.latest_baselines()
        self.assertEqual(latest[self.asha]["metrics"], {"limit_minutes": 95})
        self.assertEqual(latest[self.asha]["punched_days"], 25)
        self.assertIsNone(latest[self.hari]["metrics"])
        self.assertIsInstance(self.database.latest_baseline_at(), datetime)
        self.assertEqual(self.database.prune_baselines(), 4)
        with self.database.connect() as connection:
            kept = [r["snapshot_id"] for r in connection.execute(
                "SELECT DISTINCT snapshot_id FROM attendance_baselines ORDER BY snapshot_id")]
        self.assertEqual(kept, [3, 4, 5, 6])

    # --- alerts and outbox ----------------------------------------------------

    def test_second_alert_for_same_day_and_kind_reports_already_recorded(self) -> None:
        mail = {"recipients": ["asha@x.com"], "subject": "S", "text_body": "T", "html_body": "<p>T</p>"}
        self.assertTrue(self.database.record_alert(
            employee_id=self.asha, work_day=DAY, kind="live", mode="live", status="queued",
            site="Uppal", detail={"out_minutes": 120}, mail=mail))
        self.assertFalse(self.database.record_alert(
            employee_id=self.asha, work_day=DAY, kind="live", mode="live", status="queued", mail=mail))
        self.assertEqual(self._count("mail_outbox"), 1)
        # Another kind, or another day, is a separate alert.
        self.assertTrue(self.database.record_alert(
            employee_id=self.asha, work_day=DAY, kind="morning", mode="shadow", status="recorded"))
        self.assertTrue(self.database.record_alert(
            employee_id=self.asha, work_day=DAY + timedelta(days=1), kind="live", mode="shadow",
            status="breaker", site="Uppal"))
        alerts = self.database.alerts_for_day(DAY)
        self.assertEqual([(a["kind"], a["employee_name"]) for a in alerts],
                         [("live", "Asha"), ("morning", "Asha")])
        self.assertEqual(alerts[0]["detail"], {"out_minutes": 120})
        self.assertIsNotNone(alerts[0]["outbox_id"])
        counts = {(r["site"], r["kind"], r["status"]): r["n"]
                  for r in self.database.alert_counts(DAY)}
        self.assertEqual(counts, {("Uppal", "live", "queued"): 1, (None, "morning", "recorded"): 1,
                                  ("Uppal", "live", "breaker"): 1})
        with self.assertRaises(ValueError):
            self.database.record_alert(
                employee_id=self.asha, work_day=DAY, kind="weekly", mode="live", status="queued")

    def test_outbox_retries_with_backoff_then_fails_and_nulls_bodies(self) -> None:
        start = datetime(2026, 10, 26, 10, 0)
        with mock.patch.object(db_module, "_now", return_value=start):
            self.database.record_alert(
                employee_id=self.asha, work_day=DAY, kind="live", mode="live", status="queued",
                mail={"recipients": ["Asha@x.com", "asha@X.com", " "], "subject": "S",
                      "text_body": "token-body", "html_body": "<p>token-body</p>"})
            [row] = self.database.due_mail()
            self.assertEqual(row["recipients"], ["Asha@x.com"])
            self.assertEqual(self.database.mark_mail_failed(row["id"], "boom"), "pending")
            self.assertEqual(self.database.due_mail(), [])
        with mock.patch.object(db_module, "_now", return_value=start + timedelta(minutes=5)):
            self.assertEqual(len(self.database.due_mail()), 1)
            self.assertEqual(self.database.mark_mail_failed(row["id"], "boom"), "pending")
        with mock.patch.object(db_module, "_now", return_value=start + timedelta(minutes=14)):
            self.assertEqual(self.database.due_mail(), [])
        with mock.patch.object(db_module, "_now", return_value=start + timedelta(minutes=15)):
            self.assertEqual(self.database.mark_mail_failed(row["id"], "x" * 900), "failed")
            self.assertEqual(self.database.due_mail(), [])
        with self.database.connect() as connection:
            stored = connection.execute("SELECT * FROM mail_outbox").fetchone()
        self.assertEqual((stored["status"], stored["attempts"]), ("failed", 3))
        self.assertIsNone(stored["text_body"])
        self.assertIsNone(stored["html_body"])
        self.assertEqual(len(stored["last_error"]), 500)
        self.assertEqual(self.database.alerts_for_day(DAY)[0]["status"], "failed")
        self.assertEqual(self.database.mark_mail_failed(row["id"], "again"), "failed")
        self.assertIsNone(self.database.mark_mail_failed(9999, "none"))

    def test_sent_mail_nulls_bodies_and_marks_the_alert_sent(self) -> None:
        self.database.record_alert(
            employee_id=self.asha, work_day=DAY, kind="morning", mode="live", status="queued",
            mail={"recipients": ["asha@x.com"], "subject": "S", "text_body": "T"})
        hr_mail = self.database.enqueue_mail(["hari@x.com"], "Fix request", "text", "<p>html</p>")
        due = self.database.due_mail()
        self.assertEqual(len(due), 2)
        for row in due:
            self.database.mark_mail_sent(row["id"])
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM mail_outbox ORDER BY id").fetchall()
        self.assertEqual({r["status"] for r in rows}, {"sent"})
        self.assertTrue(all(r["text_body"] is None and r["html_body"] is None for r in rows))
        self.assertTrue(all(isinstance(r["sent_at"], datetime) for r in rows))
        self.assertEqual(self.database.alerts_for_day(DAY)[0]["status"], "sent")
        self.assertEqual(rows[1]["id"], hr_mail)
        with self.assertRaises(ValueError):
            self.database.enqueue_mail([], "S", "T")

    def test_prune_alerts_and_outbox_by_age(self) -> None:
        now = datetime(2026, 10, 26, 12, 0)
        with mock.patch.object(db_module, "_now", return_value=now - timedelta(days=40)):
            self.database.enqueue_mail(["a@x.com"], "old", "t")
        with mock.patch.object(db_module, "_now", return_value=now):
            self.database.enqueue_mail(["a@x.com"], "new", "t")
            for day in (DAY - timedelta(days=91), DAY - timedelta(days=90), DAY):
                self.database.record_alert(
                    employee_id=self.asha, work_day=day, kind="live", mode="shadow", status="recorded")
            self.assertEqual(self.database.prune_mail(30), 1)
            self.assertEqual(self.database.prune_alerts(90), 1)
        self.assertEqual(self._count("mail_outbox"), 1)
        self.assertEqual(self._count("attendance_alerts"), 2)

    # --- report links ----------------------------------------------------------

    def test_report_link_is_found_only_by_hash_and_not_after_expiry(self) -> None:
        now = datetime(2026, 10, 27, 9, 0)
        expires = datetime(2026, 11, 2, 6, 0)
        with mock.patch.object(db_module, "_now", return_value=now):
            token = self.database.create_report_link(self.asha, DAY, expires_at=expires)
            link = self.database.report_link(token)
            self.assertEqual((link["employee_id"], link["work_day"]), (self.asha, DAY))
            self.assertIsNone(self.database.report_link(sha256_hex(token)))
            self.assertIsNone(self.database.report_link(token + "x"))
            self.assertIsNone(self.database.report_link(""))
        with self.database.connect() as connection:
            stored = connection.execute("SELECT token_hash FROM fix_report_links").fetchone()
        self.assertEqual(stored["token_hash"], sha256_hex(token))
        self.assertNotEqual(stored["token_hash"], token)
        with mock.patch.object(db_module, "_now", return_value=expires):
            self.assertIsNone(self.database.report_link(token))
            self.assertEqual(self.database.prune_report_links(), 1)
        self.assertEqual(self._count("fix_report_links"), 0)

    # --- fix requests -----------------------------------------------------------

    def test_second_report_adds_lines_to_the_one_pending_request(self) -> None:
        first = self._file(self._add_in(note="Forgot to swipe"))
        self.assertTrue(first["first_lines"])
        second = self._file({"kind": "ignore", "target_fingerprint": "fp-dup"}, source="session")
        self.assertEqual(second, {"request_id": first["request_id"], "first_lines": False})
        request = self.database.fix_request(first["request_id"])
        self.assertEqual(request["status"], "pending")
        self.assertEqual(request["work_day"], DAY)
        self.assertEqual(request["employee_name"], "Asha")
        self.assertEqual([(l["kind"], l["direction"]) for l in request["lines"]],
                         [("add_in", "entry"), ("ignore", None)])
        self.assertEqual(request["lines"][0]["event_time"], datetime(2026, 10, 26, 13, 40))
        self.assertEqual(self.database.pending_fix_request(self.asha, DAY)["id"], first["request_id"])
        self.assertEqual(self.database.pending_fix_request_count(), 1)
        self.assertEqual([e["kind"] for e in self.database.fix_request_events(first["request_id"])],
                         ["filed", "lines_added"])
        # The partial unique index refuses a second pending row outright.
        with self.assertRaises(db_module.IntegrityError):
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO fix_requests (employee_id, work_day, status, source, created_at) "
                    "VALUES (?, ?, 'pending', 'link', ?)", (self.asha, DAY, db_module._now()))

    def test_eleventh_line_is_refused_and_nothing_is_written(self) -> None:
        with self.assertRaises(db_module.FixLineLimit):
            self._file(*[self._add_in(9, minute) for minute in range(11)])
        self.assertEqual(self._count("fix_requests"), 0)
        request_id = self._file(*[self._add_in(9, minute) for minute in range(10)])["request_id"]
        with self.assertRaises(db_module.FixLineLimit):
            self._file(self._add_in(10, 0))
        self.assertEqual(len(self.database.fix_request(request_id)["lines"]), 10)

    def test_malformed_lines_are_refused(self) -> None:
        bad = (
            {"kind": "add_in"},
            {"kind": "add_out", "event_time": datetime(2026, 10, 26, 18), "direction": "entry"},
            {"kind": "flip"},
            {"kind": "flip", "target_fingerprint": "fp", "direction": "sideways"},
            {"kind": "teleport", "target_fingerprint": "fp"},
            {**self._add_in(), "note": "x" * 501},
        )
        for line in bad:
            with self.subTest(line=line), self.assertRaises(ValueError):
                self._file(line)
        with self.assertRaises(ValueError):
            self._file()
        self.assertEqual(self._count("fix_requests"), 0)

    def test_close_day_then_reopen_with_lines(self) -> None:
        closed = self.database.close_fix_day(self.asha, DAY, "closed_correct", source="link")
        self.assertEqual(self.database.latest_fix_request(self.asha, DAY)["status"], "closed_correct")
        self.assertEqual(self.database.close_fix_day(self.asha, DAY, "closed_left", source="session"), closed)
        self.assertEqual(self.database.latest_fix_request(self.asha, DAY)["status"], "closed_left")
        filed = self._file(self._add_in())
        self.assertEqual(filed, {"request_id": closed, "first_lines": True})
        request = self.database.fix_request(closed)
        self.assertEqual(request["status"], "pending")
        self.assertIsNone(request["decided_at"])
        with self.assertRaises(db_module.RuleViolation):
            self.database.close_fix_day(self.asha, DAY, "closed_correct", source="link")
        self.assertEqual([e["kind"] for e in self.database.fix_request_events(closed)],
                         ["closed", "closed", "reopened", "lines_added"])

    def test_report_on_a_decided_day_starts_a_linked_request(self) -> None:
        first = self._file(self._add_in())["request_id"]
        line = self.database.fix_request(first)["lines"][0]["id"]
        self.database.decide_fix_lines(
            first, [{"line_id": line, "status": "rejected"}], actor_id=self.hari, note="Badge log says 13:55")
        self.assertEqual(self.database.fix_request(first)["status"], "rejected")
        second = self._file(self._add_in(13, 55))
        self.assertNotEqual(second["request_id"], first)
        self.assertTrue(second["first_lines"])
        self.assertEqual(self.database.fix_request(second["request_id"])["previous_request_id"], first)
        days = self.database.fix_request_days([self.asha, self.hari], DAY, DAY)
        self.assertEqual(days, {(self.asha, DAY): ["rejected", "pending"]})

    # --- deciding -----------------------------------------------------------------

    def test_approving_writes_corrections_audit_and_status(self) -> None:
        request_id = self._file(
            self._add_in(),
            {"kind": "flip", "target_fingerprint": "fp-out", "direction": "entry"},
            {"kind": "ignore", "target_fingerprint": "fp-dup"},
        )["request_id"]
        add, flip, ignore = (l["id"] for l in self.database.fix_request(request_id)["lines"])
        result = self.database.decide_fix_lines(request_id, [
            {"line_id": add, "status": "approved", "event_time": datetime(2026, 10, 26, 13, 35)},
            {"line_id": flip, "status": "approved"},
            {"line_id": ignore, "status": "approved"},
        ], actor_id=self.hari)
        self.assertEqual(result, {"status": "approved", "decided": [add, flip, ignore], "skipped": []})
        request = self.database.fix_request(request_id)
        self.assertEqual((request["status"], request["decided_by_name"]), ("approved", "Hari"))
        self.assertEqual({l["status"] for l in request["lines"]}, {"approved"})
        self.assertEqual(request["lines"][0]["hr_event_time"], datetime(2026, 10, 26, 13, 35))
        corrections = self.database.corrections_for(
            [self.asha], datetime(2026, 10, 26, 6), datetime(2026, 10, 27, 6))
        self.assertEqual(
            [(c["kind"], c["fingerprint"], c["event_timestamp"], c["direction"], c["work_day"])
             for c in corrections],
            [("add", None, datetime(2026, 10, 26, 13, 35), "entry", DAY),
             ("set_direction", "fp-out", None, "entry", DAY),
             ("ignore", "fp-dup", None, None, DAY)],
        )
        events = self.database.fix_request_events(request_id)
        approved = [e for e in events if e["kind"] == "line_approved"]
        self.assertEqual(len(approved), 3)
        self.assertEqual(approved[0]["actor_name"], "Hari")
        self.assertEqual(approved[0]["old_value"]["event_time"], "2026-10-26 13:40:00")
        self.assertEqual(approved[0]["new_value"]["correction"]["event_timestamp"], "2026-10-26 13:35:00")
        self.assertEqual(events[-1]["kind"], "decided")
        # Another person's or another window's corrections are not returned.
        self.assertEqual(self.database.corrections_for([self.hari], datetime(2026, 10, 26, 6),
                                                        datetime(2026, 10, 27, 6)), [])
        self.assertEqual(self.database.corrections_for([self.asha], datetime(2026, 10, 28, 6),
                                                        datetime(2026, 10, 29, 6)), [])
        self.assertEqual(self.database.pending_fix_request_count(), 0)

    def test_flip_without_direction_takes_the_opposite_of_the_stored_one(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO devices (id, site, model, ip, port, protocol) "
                "VALUES ('c3', 'Gachibowli', 'C3-400', '10.0.0.2', 4370, 'c3-pullsdk')")
        self.database.insert_punches([{
            "fingerprint": "fp-c3", "device_id": "c3", "site": "Gachibowli", "device_user_id": "101",
            "event_timestamp": datetime(2026, 10, 26, 13, 40), "trusted_direction": "exit",
        }])
        request_id = self._file({"kind": "flip", "target_fingerprint": "fp-c3"})["request_id"]
        line = self.database.fix_request(request_id)["lines"][0]["id"]
        self.database.decide_fix_lines(request_id, [{"line_id": line, "status": "approved"}],
                                       actor_id=self.hari)
        [correction] = self.database.corrections_for(
            [self.asha], datetime(2026, 10, 26, 6), datetime(2026, 10, 27, 6))
        self.assertEqual((correction["kind"], correction["direction"]), ("set_direction", "entry"))

    def test_mixed_decisions_give_partial_and_rejection_needs_a_note(self) -> None:
        request_id = self._file(self._add_in(), self._add_in(18, 0))["request_id"]
        first, second = (l["id"] for l in self.database.fix_request(request_id)["lines"])
        decisions = [{"line_id": first, "status": "approved"}, {"line_id": second, "status": "rejected"}]
        with self.assertRaises(ValueError):
            self.database.decide_fix_lines(request_id, decisions, actor_id=self.hari, note="  ")
        result = self.database.decide_fix_lines(request_id, decisions, actor_id=self.hari,
                                                note="Second swipe was a visitor")
        self.assertEqual(result["status"], "partial")
        request = self.database.fix_request(request_id)
        self.assertEqual(request["decision_note"], "Second swipe was a visitor")
        self.assertEqual(self._count("punch_corrections"), 1)

    def test_lines_left_pending_keep_the_request_pending(self) -> None:
        request_id = self._file(self._add_in(), self._add_in(18, 0))["request_id"]
        first, _ = (l["id"] for l in self.database.fix_request(request_id)["lines"])
        result = self.database.decide_fix_lines(
            request_id, [{"line_id": first, "status": "approved"}], actor_id=self.hari)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.database.fix_request(request_id)["status"], "pending")

    def test_nobody_decides_their_own_request(self) -> None:
        request_id = self._file(self._add_in(), employee=self.hari)["request_id"]
        line = self.database.fix_request(request_id)["lines"][0]["id"]
        with self.assertRaises(db_module.RuleViolation):
            self.database.decide_fix_lines(request_id, [{"line_id": line, "status": "approved"}],
                                           actor_id=self.hari)
        with self.assertRaises(db_module.RuleViolation):
            self.database.decide_fix_lines(9999, [], actor_id=self.hema)
        other = self._file(self._add_in())["request_id"]
        with self.assertRaises(ValueError):
            self.database.decide_fix_lines(other, [{"line_id": line, "status": "approved"}],
                                           actor_id=self.hema)

    def test_a_failure_mid_decision_leaves_nothing_written(self) -> None:
        request_id = self._file(self._add_in(), self._add_in(18, 0))["request_id"]
        first, second = (l["id"] for l in self.database.fix_request(request_id)["lines"])
        real = db_module._insert_fix_event
        calls = []

        def fail_on_second_line(connection, request_id, line_id, *args):
            calls.append(line_id)
            if line_id == second:
                raise RuntimeError("disk full")
            return real(connection, request_id, line_id, *args)

        with mock.patch.object(db_module, "_insert_fix_event", side_effect=fail_on_second_line):
            with self.assertRaises(RuntimeError):
                self.database.decide_fix_lines(request_id, [
                    {"line_id": first, "status": "approved"},
                    {"line_id": second, "status": "approved"},
                ], actor_id=self.hari)
        self.assertEqual(calls, [first, second])
        request = self.database.fix_request(request_id)
        self.assertEqual(request["status"], "pending")
        self.assertEqual({l["status"] for l in request["lines"]}, {"pending"})
        self.assertEqual(self._count("punch_corrections"), 0)
        self.assertEqual([e["kind"] for e in self.database.fix_request_events(request_id)], ["filed"])

    def test_second_decider_skips_a_line_already_decided(self) -> None:
        request_id = self._file(self._add_in())["request_id"]
        line = self.database.fix_request(request_id)["lines"][0]["id"]
        self.database.decide_fix_lines(request_id, [{"line_id": line, "status": "approved"}],
                                       actor_id=self.hari)
        again = self.database.decide_fix_lines(
            request_id, [{"line_id": line, "status": "rejected"}], actor_id=self.hema, note="No")
        self.assertEqual(again["decided"], [])
        self.assertEqual(again["skipped"], [{"line_id": line, "status": "approved",
                                             "decided_by": self.hari, "decided_by_name": "Hari"}])
        self.assertEqual(again["status"], "approved")
        self.assertEqual(self._count("punch_corrections"), 1)
        self.assertIsNone(self.database.fix_request(request_id)["decision_note"])

    def test_review_list_pending_first_then_recent_decisions(self) -> None:
        decided = self._file(self._add_in(), day=DAY - timedelta(days=1))["request_id"]
        line = self.database.fix_request(decided)["lines"][0]["id"]
        self.database.decide_fix_lines(decided, [{"line_id": line, "status": "approved"}],
                                       actor_id=self.hari)
        pending = self._file(self._add_in(), self._add_in(18, 0))["request_id"]
        self.database.close_fix_day(self.hari, DAY, "closed_left", source="session")
        rows = self.database.fix_requests_for_review()
        self.assertEqual([(r["id"], r["status"], r["line_count"]) for r in rows],
                         [(pending, "pending", 2), (decided, "approved", 1)])
        self.assertEqual(rows[0]["employee_name"], "Asha")
        self.assertEqual(rows[1]["decided_by_name"], "Hari")

    def test_active_hr_and_admin_lists_skip_archived_people(self) -> None:
        self.assertEqual([p["name"] for p in self.database.active_hr_users()], ["Hari", "Hema"])
        self.database.archive_employee(self.hema)
        self.assertEqual([p["name"] for p in self.database.active_hr_users()], ["Hari"])
        self.assertEqual([p["name"] for p in self.database.active_admins()], ["Adam"])
        self.assertEqual(self.database.active_admins()[0]["device_ids"], ["104"])

    def test_has_active_hr_other_than_skips_the_person_and_archived_hr(self) -> None:
        self.assertTrue(self.database.has_active_hr_other_than(self.hari))
        self.database.archive_employee(self.hema)
        self.assertFalse(self.database.has_active_hr_other_than(self.hari))
        self.assertTrue(self.database.has_active_hr_other_than(self.asha))

    # --- correction overlay (U2) ------------------------------------------------

    def _overlay_day(self) -> None:
        """Asha (main 101, extra NP0101) on a C3: In 09:00, Out 13:00, a wrong
        Out 13:40 under her extra ID, a duplicate Out 18:00:20, Out 18:00."""
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO devices (id, site, model, ip, port, protocol) "
                "VALUES ('c3', 'Gachibowli', 'C3-400', '10.0.0.2', 4370, 'c3-pullsdk')")
        self.database.add_extra_device_id(self.asha, "NP0101")
        swipes = (("fp-in", "101", (9, 0, 0), "entry"), ("fp-out", "101", (13, 0, 0), "exit"),
                  ("fp-wrong", "NP0101", (13, 40, 0), "exit"), ("fp-last", "101", (18, 0, 0), "exit"),
                  ("fp-dup", "101", (18, 0, 20), "exit"))
        self.database.insert_punches([{
            "fingerprint": fp, "device_id": "c3", "site": "Gachibowli", "device_user_id": uid,
            "event_timestamp": datetime(2026, 10, 26, *hms), "trusted_direction": direction,
            "raw_state": "event 0",
        } for fp, uid, hms, direction in swipes])

    def _approve(self, *lines) -> None:
        request_id = self._file(*lines)["request_id"]
        self.database.decide_fix_lines(
            request_id, [{"line_id": l["id"], "status": "approved"}
                         for l in self.database.fix_request(request_id)["lines"]],
            actor_id=self.hari)

    def test_overlay_merges_into_both_read_paths_and_punches_stay_raw(self) -> None:
        self._overlay_day()
        start, end = datetime(2026, 10, 26, 6), datetime(2026, 10, 27, 6)
        self._approve({"kind": "flip", "target_fingerprint": "fp-wrong", "direction": "entry"},
                      {"kind": "ignore", "target_fingerprint": "fp-dup"},
                      {"kind": "add_in", "event_time": datetime(2026, 10, 26, 19, 0)},
                      {"kind": "ignore", "target_fingerprint": "fp-gone"})
        one = self.database.punches_for_employee_range(["101", "NP0101"], start, end)
        many = self.database.punches_for_device_users_range(["101", "NP0101", "999"], start, end)
        self.assertEqual(
            [(r["fingerprint"], r["device_user_id"], r["trusted_direction"], r["correction"]) for r in one],
            [("fp-in", "101", "entry", None), ("fp-out", "101", "exit", None),
             ("fp-wrong", "NP0101", "entry", "direction_fixed"), ("fp-last", "101", "exit", None),
             ("fp-dup", "101", "exit", "ignored"), ("correction:3", "101", "entry", "added")])
        self.assertEqual(one[0]["raw_state"], "event 0")
        self.assertEqual({r["employee_id"] for r in one}, {self.asha})
        added = one[-1]
        self.assertEqual((added["device_id"], added["site"], added["raw_state"]), (None, None, None))
        # The many-person path holds the same rows, by device user ID then time.
        self.assertEqual(sorted(r["fingerprint"] for r in many), sorted(r["fingerprint"] for r in one))
        self.assertEqual([r["device_user_id"] for r in many], ["101"] * 5 + ["NP0101"])
        with self.database.connect() as connection:
            stored = connection.execute(
                "SELECT fingerprint, trusted_direction FROM punches ORDER BY event_timestamp").fetchall()
        self.assertEqual([(r["fingerprint"], r["trusted_direction"]) for r in stored], [
            ("fp-in", "entry"), ("fp-out", "exit"), ("fp-wrong", "exit"), ("fp-last", "exit"),
            ("fp-dup", "exit")])
        # Another person's read never sees Asha's fixes.
        self.assertEqual(self.database.punches_for_employee_range("102", start, end), [])

    def test_added_punch_reads_back_on_a_day_without_device_punches(self) -> None:
        self._approve({"kind": "add_in", "event_time": datetime(2026, 10, 26, 9, 0)})
        rows = self.database.punches_for_employee_range(
            "101", datetime(2026, 10, 26, 6), datetime(2026, 10, 27, 6))
        self.assertEqual([(r["device_user_id"], r["correction"]) for r in rows], [("101", "added")])
        # Outside the window it does not.
        self.assertEqual(self.database.punches_for_employee_range(
            "101", datetime(2026, 10, 27, 6), datetime(2026, 10, 28, 6)), [])

    def test_frappe_checkins_and_id_issues_read_raw_punches_only(self) -> None:
        self._overlay_day()
        self._approve({"kind": "flip", "target_fingerprint": "fp-wrong", "direction": "entry"},
                      {"kind": "ignore", "target_fingerprint": "fp-dup"},
                      {"kind": "add_in", "event_time": datetime(2026, 10, 26, 13, 40, 30)})
        pending = self.database.frappe_pending_punches(since=None, limit=50)
        checkins = self.database.checkins_since(since=None, limit=50)
        for rows in (pending, checkins):
            self.assertEqual(len(rows), 5)
            self.assertNotIn(datetime(2026, 10, 26, 13, 40, 30), [r["event_timestamp"] for r in rows])
            # Raw directions: the flipped punch still goes out as an exit.
            self.assertEqual(rows[2]["trusted_direction"], "exit")
        self.assertEqual({r["device_user_id"]: r["punch_count"] for r in self.database.device_id_summary()},
                         {"101": 4, "NP0101": 1})

    def test_raw_punches_carry_raw_json_and_skip_the_overlay(self) -> None:
        self._overlay_day()
        self._approve({"kind": "flip", "target_fingerprint": "fp-wrong", "direction": "entry"},
                      {"kind": "add_in", "event_time": datetime(2026, 10, 26, 19, 0)})
        rows = self.database.raw_punches_for_device_users_range(
            ["101", "NP0101"], datetime(2026, 10, 26, 6), datetime(2026, 10, 27, 6))
        self.assertEqual(len(rows), 5)
        self.assertNotIn("correction", rows[0])
        wrong = next(r for r in rows if r["fingerprint"] == "fp-wrong")
        self.assertEqual(wrong["trusted_direction"], "exit")
        self.assertIsInstance(wrong["raw_json"], dict)
        self.assertIn("raw_direction_byte", wrong)
        self.assertEqual(self.database.raw_punches_for_device_users_range(
            [], datetime(2026, 10, 26, 6), datetime(2026, 10, 27, 6)), [])


class SharedLocalFileTest(unittest.TestCase):
    """Local-first U1 / KTD5, KTD6: two processes share one WAL file."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = tmp.name
        self.path = os.path.join(tmp.name, "essl.db")

    def _open(self, **kwargs) -> db_module.Database:
        database = db_module.Database(self.path, **kwargs)
        self.addCleanup(database.close)
        return database

    def test_open_sets_wal_normal_sync_and_busy_timeout(self) -> None:
        with self._open().connect() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()["journal_mode"], "wal")
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()["synchronous"], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()["timeout"], 5000)

    def test_reader_sees_writer_rows_at_once(self) -> None:
        self._open().initialize((DeviceConfig(
            id="d1", site="Uppal", model="X990", ip="10.0.0.1", port=4370,
            protocol="zk-attendance", password_env="ESSL_X990_PASSWORD",
        ),))
        collector = self._open(must_exist=True)
        web = self._open(must_exist=True)
        collector.insert_punches([{
            "device_id": "d1", "device_user_id": "7", "event_timestamp": datetime(2026, 10, 14, 9, 0),
            "fingerprint": "fp-shared", "site": "Uppal", "device_serial": "SN-1",
            "employee_name": "Ravi",
        }])
        with web.connect() as connection:
            row = connection.execute("SELECT fingerprint FROM punches").fetchone()
        self.assertEqual(row["fingerprint"], "fp-shared")

    def test_lock_held_past_timeout_raises_busy_error(self) -> None:
        self._open().migrate_schema()
        holder = self._open()
        waiter = self._open(must_exist=True)
        with mock.patch.object(db_module, "BUSY_TIMEOUT_MS", 100):
            with waiter.connect():
                pass
        with holder.transaction():
            with self.assertRaises(db_module.BusyError):
                with waiter.transaction() as connection:
                    connection.execute("UPDATE settings SET value = value")

    def test_classify_error_maps_locked_to_busy(self) -> None:
        self.assertIsInstance(db_module.classify_error(ValueError("database is locked")), db_module.BusyError)

    def test_must_exist_raises_and_creates_nothing(self) -> None:
        database = self._open(must_exist=True)
        with self.assertRaises(db_module.DatabaseMissing):
            database.migrate_schema()
        self.assertFalse(database.exists())
        self.assertEqual(os.listdir(self.folder), [])

    def test_existing_file_opens_and_migrates_with_must_exist(self) -> None:
        self._open().migrate_schema()
        database = self._open(must_exist=True)
        database.initialize(())
        self.assertEqual(database.setting("schema_version"), str(db_module.SCHEMA_VERSION))


if __name__ == "__main__":
    unittest.main()
