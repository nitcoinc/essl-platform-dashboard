from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

from essl_hub.config import DeviceConfig
from essl_hub import db as db_module


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
                {"id", "ip", "port", "comm_password", "connection_updated_at"},
            )

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

    def test_record_poll_skips_punches_the_replica_already_has(self) -> None:
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
        # Turso replicas cannot write PRAGMA user_version; settings carries it.
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
        self.assertEqual(version, 3)
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

    def test_sync_is_a_noop_in_local_mode(self) -> None:
        self.assertTrue(self.database.sync())
        self.assertIsNone(self.database.start_sync_loop(1))
        self.assertIsNone(self.database.last_sync_error)

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


class ReplicaModeTest(unittest.TestCase):
    """Replica-mode branches with the network faked out: no Turso needed."""

    URL = "libsql://example.turso.io"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "replica.db")

    def _replica(self) -> db_module.Database:
        database = db_module.Database(self.path, sync_url=self.URL, auth_token="t")
        self.addCleanup(database.close)
        return database

    def test_degraded_mode_reads_existing_file_and_refuses_writes(self) -> None:
        local = db_module.Database(self.path, sync_url="")
        local.initialize(())
        local.set_setting("k", "v")
        local.close()
        database = self._replica()
        with mock.patch.object(db_module, "primary_reachable", return_value=False), \
                mock.patch.object(db_module.libsql, "connect", wraps=db_module.libsql.connect) as connect:
            self.assertEqual(database.setting("k"), "v")
            self.assertTrue(database.offline)
            self.assertNotIn("sync_url", connect.call_args.kwargs)
            with self.assertRaises(db_module.OfflineError):
                database.set_setting("k", "w")
            with self.assertRaises(db_module.OfflineError):
                with database.connect() as connection:
                    connection.execute("UPDATE settings SET value='w'")
            self.assertFalse(database.reconnect())
            self.assertFalse(database.sync())
        self.assertEqual(database.setting("k"), "v")
        self.assertIsNotNone(database.last_sync_error)

    def test_offline_without_replica_file_is_unavailable(self) -> None:
        database = self._replica()
        with mock.patch.object(db_module, "primary_reachable", return_value=False):
            with self.assertRaises(db_module.OfflineError):
                database.setting("k")
        self.assertFalse(os.path.exists(self.path))

    def test_failed_sync_records_error_and_does_not_raise(self) -> None:
        raw = mock.MagicMock()
        raw.sync.side_effect = ValueError("sync error: http dispatch error: boom")
        database = self._replica()
        with mock.patch.object(db_module, "primary_reachable", return_value=True), \
                mock.patch.object(db_module.libsql, "connect", return_value=raw):
            self.assertFalse(database.sync())
        self.assertIn("boom", database.last_sync_error)
        self.assertIsNone(database.last_sync_ok_at)
        raw.sync.side_effect = None
        with mock.patch.object(db_module, "primary_reachable", return_value=True):
            self.assertTrue(database.sync())
        self.assertIsNone(database.last_sync_error)
        self.assertIsNotNone(database.last_sync_ok_at)

    def _stale_replica_world(self):
        """Primary file with 2 punches; replica file frozen with 1. The fake
        replica connect bootstraps by copying the primary when the file is gone."""
        import shutil

        primary_path = os.path.join(self._tmp.name, "primary.db")
        primary = db_module.Database(primary_path, sync_url="")
        primary.initialize((DeviceConfig(
            id="gachibowli-c3-400", site="Gachibowli", model="C3-400", ip="192.168.1.202",
            port=4370, protocol="zk-pull", password_env="ESSL_C3_PASSWORD",
        ),))
        punch = DatabaseTest._punch
        primary.insert_punches([punch(self, fingerprint="a")])
        primary.close()
        shutil.copy(primary_path, self.path)
        primary = db_module.Database(primary_path, sync_url="")
        primary.insert_punches([punch(self, fingerprint="b")])
        primary.close()
        real_connect = db_module.libsql.connect

        class Raw:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def sync(self):  # frozen: never pulls anything
                return None

        def fake_connect(target, **kwargs):
            if target == self.URL:
                return real_connect(primary_path)
            if "sync_url" in kwargs:
                if not os.path.exists(target):
                    shutil.copy(primary_path, target)
                return Raw(real_connect(target, isolation_level=None))
            return real_connect(target, **kwargs)

        return fake_connect

    def _count(self, database) -> int:
        with database.connect() as connection:
            return connection.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"]

    def test_replica_behind_compares_both_watermarks(self) -> None:
        behind = db_module.replica_behind
        self.assertFalse(behind(None, (5, "x")))
        self.assertFalse(behind((5, "b"), None))
        self.assertFalse(behind((5, "b"), (5, "b")))
        self.assertFalse(behind((6, "c"), (5, "b")))  # ahead is fine
        self.assertTrue(behind((4, "b"), (5, "b")))
        self.assertTrue(behind((5, "a"), (5, "b")))
        self.assertTrue(behind((0, ""), (1, None)))

    def test_stale_replica_is_rebuilt_after_consecutive_behind_checks(self) -> None:
        database = self._replica()
        with mock.patch.object(db_module, "primary_reachable", return_value=True), \
                mock.patch.object(db_module.libsql, "connect", side_effect=self._stale_replica_world()):
            self.assertTrue(database.sync())  # first check only records the primary
            self.assertEqual(self._count(database), 1)
            self.assertTrue(database.sync())  # behind once: wait, could be a race
            self.assertIsNone(database.last_rebuild_at)
            self.assertTrue(database.sync())  # behind twice: rebuild
            self.assertIsNotNone(database.last_rebuild_at)
            self.assertEqual(self._count(database), 2)
            self.assertTrue(database.sync())  # now caught up, no second rebuild
        self.assertEqual(database._behind_checks, 0)

    def test_unreachable_probe_returns_false_quickly(self) -> None:
        self.assertFalse(db_module.primary_reachable("http://127.0.0.1:9", timeout=1))


class TursoConfigTest(unittest.TestCase):
    def test_url_without_token_fails_naming_the_variable(self) -> None:
        env = {"TURSO_DATABASE_URL": "libsql://x.turso.io", "TURSO_AUTH_TOKEN": ""}
        with mock.patch.dict(os.environ, env):
            with self.assertRaisesRegex(RuntimeError, "TURSO_AUTH_TOKEN"):
                db_module.Database("unused.db")

    def test_unset_url_is_local_mode(self) -> None:
        with mock.patch.dict(os.environ, {"TURSO_DATABASE_URL": ""}):
            self.assertFalse(db_module.Database("unused.db").replica)


if __name__ == "__main__":
    unittest.main()
