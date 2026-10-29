from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import essl_hub.collector_main as collector_main
from essl_hub import config
from essl_hub.collector_main import (
    collect_once,
    merge_device_configs,
    refresh_devices,
)
from essl_hub.collectors import CollectionResult
from essl_hub.config import DEVICES
from essl_hub.db import OfflineError
from essl_hub.spool import Spool


def _seeded_rows() -> list[dict]:
    """What device_connection_info returns right after seeding config.DEVICES."""
    return [
        {"id": d.id, "site": d.site, "model": d.model, "protocol": d.protocol,
         "ip": d.ip, "port": d.port, "comm_password": None, "connection_updated_at": None,
         "duplicate_window_seconds": 60, "door_denied_since": None,
         "door_params_request": None, "door_params_requested_at": None}
        for d in DEVICES
    ]


class FakeDatabase:
    def __init__(self, connection_info: list[dict] | None = None) -> None:
        self.writes: list[dict] = []
        self.successes: list[dict] = []
        self.failures: list[tuple[str, str]] = []
        self.inserted_punches: list[dict] = []
        self.connection_info = list(_seeded_rows() if connection_info is None else connection_info)
        self.drift_checked: list | None = None
        self.offline = False
        self.initialized = 0
        self.direction_fills: list[list[dict]] = []
        self.door_records: list[dict] = []
        self.door_clears: list[tuple] = []

    def initialize(self, devices) -> None:
        self.initialized += 1

    def insert_punches(self, punches) -> int:
        punches = list(punches)
        self.inserted_punches.extend(punches)
        return len(punches)

    def fill_punch_directions(self, punches) -> int:
        punches = list(punches)
        self.direction_fills.append(punches)
        return len(punches)

    def device_connection_info(self) -> list[dict]:
        return list(self.connection_info)

    def warn_device_drift(self, devices) -> None:
        self.drift_checked = list(devices)

    def record_door_params(self, device_id, *, params, read_at, result=None,
                           previous=None, previous_for=None) -> None:
        self.door_records.append({"device_id": device_id, "params": params, "read_at": read_at,
                                  "result": result, "previous": previous,
                                  "previous_for": previous_for})

    def door_request_at(self, device_id):
        row = next((r for r in self.connection_info if r["id"] == device_id), {})
        return collector_main._as_datetime(row.get("door_params_requested_at"))

    def clear_door_request(self, device_id, requested_at) -> bool:
        self.door_clears.append((device_id, requested_at))
        return True

    def record_poll(self, device_id, *, punches=(), status, **kwargs) -> int:
        punches = list(punches)
        self.writes.append({"device_id": device_id, "status": status, **kwargs})
        self.inserted_punches.extend(punches)
        if status == "connected":
            self.successes.append({"device_id": device_id, **kwargs})
        else:
            self.failures.append((device_id, kwargs.get("error", "")))
        return len(punches)


class RunForeverTest(unittest.TestCase):
    """R5/R16 regression cover for the polling loop itself, not just the pure
    helpers. time.sleep is patched to end the otherwise-infinite loop."""

    def _row(self, ip: str) -> dict:
        device = DEVICES[0]
        return {"id": device.id, "ip": ip, "port": device.port, "comm_password": None}

    def test_a_changed_ip_is_picked_up_without_a_restart(self) -> None:
        database = FakeDatabase([self._row("10.0.0.1")])
        seen: list[str] = []

        def fake_collector(device):
            seen.append(device.ip)
            return CollectionResult([], "TCP realtime", "SN-1", "FW-1")

        sleeps = {"n": 0}

        def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] == 1:
                database.connection_info = [self._row("10.0.0.2")]
            else:
                raise StopIteration

        with patch("essl_hub.collector_main.time.sleep", fake_sleep):
            with self.assertRaises(StopIteration):
                collector_main.run_forever(
                    database,
                    interval_seconds=0,
                    device_id=DEVICES[0].id,
                    collector=fake_collector,
                    spool=Spool(":memory:"),
                )
        self.assertEqual(seen, ["10.0.0.1", "10.0.0.2"])

    def test_a_database_failure_during_a_cycle_does_not_end_the_loop(self) -> None:
        database = FakeDatabase()
        calls = {"n": 0}
        original = database.record_poll

        def flaky_record_poll(device_id, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is down")
            return original(device_id, **kwargs)

        database.record_poll = flaky_record_poll
        collected: list[str] = []

        def fake_collector(device):
            collected.append(device.id)
            return CollectionResult([], "TCP realtime", "SN-1", "FW-1")

        sleeps = {"n": 0}

        def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                raise StopIteration

        with patch("essl_hub.collector_main.time.sleep", fake_sleep):
            with self.assertLogs(collector_main._logger, level="WARNING"):
                with self.assertRaises(StopIteration):
                    collector_main.run_forever(
                        database,
                        interval_seconds=0,
                        device_id=DEVICES[0].id,
                        collector=fake_collector,
                        spool=Spool(":memory:"),
                    )
        # the failed first cycle did not end the loop: the second cycle wrote
        self.assertEqual(collected, [DEVICES[0].id, DEVICES[0].id])
        self.assertEqual(len(database.writes), 1)


class CollectOnceTest(unittest.TestCase):
    def test_normalized_event_is_stored_with_its_raw_direction_byte(self) -> None:
        database = FakeDatabase()
        event = {
            "fingerprint": "fp-1",
            "device_id": DEVICES[1].id,
            "site": DEVICES[1].site,
            "device_user_id": "118",
            "raw_direction_byte": 0,
            "trusted_direction": "entry",
            "raw": {"pin": "118"},
        }
        collector = lambda device: CollectionResult([event], "TCP realtime", "SN-1", "FW-1")
        collect_once(database, DEVICES[1], collector=collector)
        self.assertEqual(database.inserted_punches, [event])
        self.assertEqual(database.successes[0]["device_id"], DEVICES[1].id)
        self.assertEqual(database.successes[0]["records_seen"], 1)

    def test_failed_cycle_records_the_device_error_and_does_not_raise(self) -> None:
        database = FakeDatabase()

        def failing_collector(device):
            raise ConnectionError("controller rejected the PullSDK connection")

        collect_once(database, DEVICES[1], collector=failing_collector)
        self.assertEqual(len(database.failures), 1)
        self.assertIn("rejected", database.failures[0][1])
        self.assertEqual(database.successes, [])

    def test_two_cycles_pass_the_same_event_to_insert_punches_both_times(self) -> None:
        # collect_once always hands its events to Database.record_poll; the
        # dedup itself is SQLite's ON CONFLICT DO NOTHING, proven against a
        # real database in test_db.py's duplicate-fingerprint test.
        database = FakeDatabase()
        event = {"fingerprint": "fp-1", "device_id": DEVICES[1].id}
        collector = lambda device: CollectionResult([event], "TCP realtime", "SN-1", "FW-1")
        collect_once(database, DEVICES[1], collector=collector)
        collect_once(database, DEVICES[1], collector=collector)
        self.assertEqual(database.inserted_punches, [event, event])

    def test_cycle_with_no_events_leaves_device_connected_not_errored(self) -> None:
        database = FakeDatabase()
        collector = lambda device: CollectionResult([], "TCP realtime", "SN-1", "FW-1")
        collect_once(database, DEVICES[1], collector=collector)
        self.assertEqual(database.failures, [])
        self.assertEqual(database.successes[0]["records_seen"], 0)

    def test_collector_main_does_not_import_flask_or_the_web_app(self) -> None:
        import essl_hub.collector_main as collector_main

        with open(collector_main.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertNotIn("flask", source.lower())
        self.assertNotIn("essl_hub.web", source)
        self.assertNotIn("from .web", source)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class WriteReductionTest(unittest.TestCase):
    """KTD11/U4: idle polls write nothing; changes, edits and heartbeats do."""

    def setUp(self) -> None:
        self.clock = _Clock()
        self.state = collector_main.PollState(heartbeat_seconds=300, clock=self.clock)
        self.database = FakeDatabase()
        self.device = DEVICES[1]

    def _poll(self, collector) -> None:
        collect_once(self.database, self.device, collector=collector, state=self.state)

    @staticmethod
    def _idle(device):
        return CollectionResult([], "TCP realtime", "SN-1", "FW-1")

    def test_idle_polls_after_the_first_write_issue_no_writes(self) -> None:
        self._poll(self._idle)
        self.assertEqual(len(self.database.writes), 1)
        for _ in range(3):
            self.clock.now += 60
            self._poll(self._idle)
        self.assertEqual(len(self.database.writes), 1)
        self.assertNotIn("syncing", [w["status"] for w in self.database.writes])

    def test_idle_polls_against_a_real_database_issue_zero_updates(self) -> None:
        import tempfile

        from essl_hub.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            database = Database(f"{tmp}/essl.db")
            database.initialize(DEVICES)
            self._poll_real(database)
            statements: list[str] = []
            database._connection().set_trace_callback(statements.append)
            for _ in range(3):
                self.clock.now += 60
                self._poll_real(database)
        self.assertFalse([s for s in statements if "UPDATE" in s.upper()])
        self.assertFalse([s for s in statements if "BEGIN" in s.upper()])

    def _poll_real(self, database) -> None:
        collect_once(database, self.device, collector=self._idle, state=self.state)

    def test_full_history_polls_with_nothing_new_issue_zero_updates(self) -> None:
        """Devices return their whole log every poll; already-stored punches
        must not count as a change (no needless writes)."""
        import tempfile

        from essl_hub.db import Database

        device = DEVICES[0]
        history = lambda d: CollectionResult([_punch("fp-old", device)], "TCP realtime", "SN-1", "FW-1")  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(f"{tmp}/essl.db")
            database.initialize(DEVICES)
            collect_once(database, device, collector=history, state=self.state)
            statements: list[str] = []
            database._connection().set_trace_callback(statements.append)
            for _ in range(3):
                self.clock.now += 60
                collect_once(database, device, collector=history, state=self.state)
            self.assertFalse([s for s in statements if "UPDATE" in s.upper() or "INSERT" in s.upper()])
            fresh = lambda d: CollectionResult(  # noqa: E731
                [_punch("fp-old", device), _punch("fp-new", device)], "TCP realtime", "SN-1", "FW-1"
            )
            self.clock.now += 60
            collect_once(database, device, collector=fresh, state=self.state)
            self.assertTrue([s for s in statements if "INSERT" in s.upper()])

    def test_heartbeat_interval_forces_one_write(self) -> None:
        self._poll(self._idle)
        self.clock.now += 299
        self._poll(self._idle)
        self.assertEqual(len(self.database.writes), 1)
        self.clock.now += 1
        self._poll(self._idle)
        self.assertEqual(len(self.database.writes), 2)
        self.clock.now += 10
        self._poll(self._idle)
        self.assertEqual(len(self.database.writes), 2)

    def test_status_change_writes_immediately(self) -> None:
        self._poll(self._idle)

        def failing(device):
            raise ConnectionError("timed out")

        self.clock.now += 1
        self._poll(failing)
        self.assertEqual(len(self.database.writes), 2)
        self.assertEqual(self.database.writes[-1]["status"], "error")
        self.clock.now += 1
        self._poll(failing)
        self.assertEqual(len(self.database.writes), 2)
        self.clock.now += 1
        self._poll(self._idle)
        self.assertEqual(self.database.writes[-1]["status"], "connected")

    def test_firmware_change_writes(self) -> None:
        self._poll(self._idle)
        self._poll(lambda d: CollectionResult([], "TCP realtime", "SN-1", "FW-2"))
        self.assertEqual(len(self.database.writes), 2)

    def test_new_punches_write(self) -> None:
        self._poll(self._idle)
        event = {"fingerprint": "fp-9", "device_id": self.device.id}
        self._poll(lambda d: CollectionResult([event], "TCP realtime", "SN-1", "FW-1"))
        self.assertEqual(len(self.database.writes), 2)
        self.assertEqual(self.database.inserted_punches, [event])

    def test_applied_connection_edit_forces_a_write(self) -> None:
        row = {
            "id": self.device.id,
            "ip": self.device.ip,
            "port": self.device.port,
            "comm_password": None,
            "connection_updated_at": None,
        }
        self.database.connection_info = [row]
        refresh_devices(self.database, [], state=self.state)
        self._poll(self._idle)
        self._poll(self._idle)
        self.assertEqual(len(self.database.writes), 1)
        self.database.connection_info = [{**row, "connection_updated_at": "2026-10-09 10:00:00"}]
        refresh_devices(self.database, [], state=self.state)
        self._poll(self._idle)
        self.assertEqual(len(self.database.writes), 2)
        self._poll(self._idle)
        self.assertEqual(len(self.database.writes), 2)

    def test_applied_edit_updates_last_attempt_in_the_database(self) -> None:
        import tempfile

        from essl_hub.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            database = Database(f"{tmp}/essl.db")
            database.initialize(DEVICES)
            refresh_devices(database, [], state=self.state)
            self._poll_real(database)
            database.update_device(
                self.device.id, ip=self.device.ip, port=self.device.port, comm_password=None
            )
            refresh_devices(database, [], state=self.state)
            self._poll_real(database)
            row = database.device_by_id(self.device.id)
        self.assertGreaterEqual(row["last_attempt"], row["connection_updated_at"])

    def test_failed_write_without_a_spool_propagates(self) -> None:
        def broken(device_id, **kwargs):
            raise RuntimeError("boom")

        self.database.record_poll = broken
        with self.assertRaises(RuntimeError):
            self._poll(lambda d: CollectionResult([{"fingerprint": "fp-1"}], "TCP realtime", "SN-1", "FW-1"))


class MergeDeviceConfigsTest(unittest.TestCase):
    def test_matching_row_overrides_ip_port_and_password(self) -> None:
        rows = [
            {
                "id": DEVICES[0].id,
                "ip": "10.0.0.9",
                "port": 5000,
                "comm_password": "s3cret",
            }
        ]
        merged = merge_device_configs(DEVICES, rows)
        self.assertEqual(merged[0].ip, "10.0.0.9")
        self.assertEqual(merged[0].port, 5000)
        self.assertEqual(merged[0].password, "s3cret")

    def test_a_static_device_with_no_row_is_not_polled(self) -> None:
        rows = [{"id": DEVICES[0].id, "ip": "10.0.0.9", "port": 5000, "comm_password": None}]
        merged = merge_device_configs(DEVICES, rows)
        self.assertEqual([d.id for d in merged], [DEVICES[0].id])

    def test_a_db_only_third_device_yields_three_configs(self) -> None:
        third = {
            "id": "kondapur-c3-400", "site": "Kondapur", "model": "C3-400",
            "protocol": "c3-pullsdk", "ip": "192.168.1.203", "port": 4370,
            "comm_password": None,
        }
        merged = merge_device_configs(DEVICES, _seeded_rows() + [third])
        self.assertEqual(len(merged), 3)
        added = merged[2]
        self.assertEqual(
            (added.id, added.site, added.protocol, added.ip),
            ("kondapur-c3-400", "Kondapur", "c3-pullsdk", "192.168.1.203"),
        )
        self.assertIsNone(config.password_for(added))

    def test_a_db_row_without_a_protocol_is_skipped(self) -> None:
        rows = [{"id": "ghost-device", "ip": "1.2.3.4", "port": 1, "comm_password": None}]
        self.assertEqual(merge_device_configs(DEVICES, rows), [])

    def test_a_retired_row_is_excluded(self) -> None:
        rows = _seeded_rows()
        rows[1]["retired_at"] = "2026-10-12 10:00:00"
        merged = merge_device_configs(DEVICES, rows)
        self.assertEqual([d.id for d in merged], [DEVICES[0].id])

    def test_device_id_filter_keeps_only_the_requested_device(self) -> None:
        merged = merge_device_configs(DEVICES, _seeded_rows(), device_id=DEVICES[1].id)
        self.assertEqual([d.id for d in merged], [DEVICES[1].id])

    def test_null_comm_password_falls_back_to_the_environment_password(self) -> None:
        rows = [
            {
                "id": DEVICES[0].id,
                "ip": DEVICES[0].ip,
                "port": DEVICES[0].port,
                "comm_password": None,
            }
        ]
        device = merge_device_configs(DEVICES, rows)[0]
        self.assertFalse(device.password)
        with patch.dict(os.environ, {"ESSL_X990_PASSWORD": "envpw"}):
            self.assertEqual(config.password_for(device), "envpw")

    def test_stored_comm_password_is_preferred_over_the_environment(self) -> None:
        rows = [
            {
                "id": DEVICES[0].id,
                "ip": DEVICES[0].ip,
                "port": DEVICES[0].port,
                "comm_password": "dbpw",
            }
        ]
        device = merge_device_configs(DEVICES, rows)[0]
        with patch.dict(os.environ, {"ESSL_X990_PASSWORD": "envpw"}):
            self.assertEqual(config.password_for(device), "dbpw")

    def test_door_denied_since_comes_from_the_row(self) -> None:
        # KTD5: the collector gates 24/25 capture on the device's policy.
        from datetime import datetime

        rows = _seeded_rows()
        rows[1]["door_denied_since"] = datetime(2026, 10, 26, 9, 0)
        merged = merge_device_configs(DEVICES, rows)
        self.assertIsNone(merged[0].door_denied_since)
        self.assertEqual(merged[1].door_denied_since, datetime(2026, 10, 26, 9, 0))
        rows[1]["door_denied_since"] = "2026-10-26 09:00:00"
        self.assertEqual(merge_device_configs(DEVICES, rows)[1].door_denied_since, datetime(2026, 10, 26, 9, 0))

    def test_no_rows_means_nothing_to_poll(self) -> None:
        # every device retired: nothing is polled, the static list is not a fallback
        self.assertEqual(merge_device_configs(DEVICES, []), [])

    def test_boot_list_equals_the_static_env_configured_list(self) -> None:
        self.assertEqual(collector_main.static_devices(), list(DEVICES))
        self.assertEqual(collector_main.static_devices(DEVICES[1].id), [DEVICES[1]])


class RefreshDevicesTest(unittest.TestCase):
    def test_success_returns_the_merged_list(self) -> None:
        rows = _seeded_rows()
        rows[0].update(ip="10.9.9.9", port=4371)
        database = FakeDatabase(connection_info=rows)
        merged = refresh_devices(database, collector_main.static_devices())
        self.assertEqual(merged[0].ip, "10.9.9.9")
        self.assertEqual(merged[0].port, 4371)
        self.assertEqual(merged[1].ip, DEVICES[1].ip)

    def test_database_failure_reuses_the_previous_list_and_logs_a_warning(self) -> None:
        class BrokenDatabase:
            def device_connection_info(self):
                raise RuntimeError("postgres is unreachable")

        previous = collector_main.static_devices()
        with self.assertLogs(collector_main._logger, level="WARNING") as captured:
            result = refresh_devices(BrokenDatabase(), previous)
        self.assertIs(result, previous)
        self.assertTrue(any("postgres is unreachable" in line for line in captured.output))


class DirectionRepairTest(unittest.TestCase):
    def _event(self) -> dict:
        return {"fingerprint": "fp", "device_id": "gachibowli-c3-400", "trusted_direction": "exit"}

    def test_history_batches_repair_directions_before_the_write(self) -> None:
        database = FakeDatabase()
        collect_once(
            database, DEVICES[1],
            collector=lambda d: CollectionResult([self._event()], "TCP history", "SN", "FW"),
        )
        self.assertEqual(database.direction_fills, [[self._event()]])
        self.assertEqual(len(database.inserted_punches), 1)

    def test_realtime_batches_do_not_repair(self) -> None:
        database = FakeDatabase()
        collect_once(
            database, DEVICES[1],
            collector=lambda d: CollectionResult([self._event()], "TCP realtime", "SN", "FW"),
        )
        self.assertEqual(database.direction_fills, [])

    def test_a_failed_repair_still_writes_the_poll(self) -> None:
        database = FakeDatabase()

        def broken(_punches):
            raise RuntimeError("turso down")

        database.fill_punch_directions = broken
        collect_once(
            database, DEVICES[1],
            collector=lambda d: CollectionResult([self._event()], "TCP history", "SN", "FW"),
        )
        self.assertEqual(len(database.successes), 1)


class DynamicDevicesCycleTest(unittest.TestCase):
    """U4: the devices table is authoritative for what the collector polls."""

    def setUp(self) -> None:
        import tempfile

        from essl_hub.db import Database

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Database(os.path.join(tmp.name, "essl.db"))
        self.addCleanup(self.database.close)
        self.database.initialize(DEVICES)
        self.spool = Spool(":memory:")
        self.state = collector_main.PollState(heartbeat_seconds=300, clock=_Clock())
        self.polled: list[str] = []

    def _collector(self, device):
        self.polled.append(device.id)
        return CollectionResult([], "TCP realtime", "SN", "FW")

    def _cycle(self) -> list[str]:
        self.polled = []
        collector_main.run_cycle(
            self.database, self.spool, collector_main.static_devices(),
            collector=self._collector, state=self.state,
        )
        return sorted(self.polled)

    def _add_third(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO devices (id, site, model, ip, port, protocol, created_at) "
                "VALUES ('kondapur-c3-400', 'Kondapur', 'C3-400', '192.168.1.203', 4370, "
                "'c3-pullsdk', '2026-10-12 10:00:00')"
            )

    def test_a_newly_added_row_is_polled_on_the_next_cycle(self) -> None:
        self.assertEqual(self._cycle(), sorted(d.id for d in DEVICES))
        self._add_third()
        self.assertEqual(self._cycle(), sorted([*(d.id for d in DEVICES), "kondapur-c3-400"]))

    def test_a_retired_row_is_not_polled(self) -> None:
        self._cycle()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE devices SET retired_at='2026-10-12 11:00:00' WHERE id=?", (DEVICES[1].id,)
            )
        self.assertEqual(self._cycle(), [DEVICES[0].id])

    def test_c3_history_schedule_is_tracked_per_new_device_id(self) -> None:
        from essl_hub import collectors

        self._add_third()
        self.addCleanup(collectors._C3_HISTORY_AT.pop, DEVICES[1].id, None)
        self.addCleanup(collectors._C3_HISTORY_AT.pop, "kondapur-c3-400", None)
        collectors._C3_HISTORY_AT[DEVICES[1].id] = collectors.time.monotonic()
        self.assertFalse(collectors._c3_history_due(DEVICES[1].id))
        self.assertTrue(collectors._c3_history_due("kondapur-c3-400"))


class DoorStepCycleTest(unittest.TestCase):
    """U4: the C3 door step runs after the poll, on its own, and never
    breaks the cycle."""

    REQUESTED = datetime(2026, 10, 26, 10, 0, 0)
    NOW = datetime(2026, 10, 26, 10, 1, 0)

    def setUp(self) -> None:
        from tests.test_collectors import _DoorPanel

        self.panel = _DoorPanel
        _DoorPanel.instances = []
        _DoorPanel.behaviour = {}
        _DoorPanel.connects = True
        self.c3 = DEVICES[1]
        self.state = collector_main.PollState(heartbeat_seconds=300, clock=_Clock())
        self.steps: list[tuple] = []

    def _rows(self, request=None, requested_at=None) -> list[dict]:
        rows = _seeded_rows()
        rows[1]["door_params_request"] = request
        rows[1]["door_params_requested_at"] = requested_at
        return rows

    def _cycle(self, database, *, transport="TCP realtime", events=(), door_step=None):
        def step(device, request, requested_at, *, now):
            self.steps.append((device.id, request, requested_at))
            return door_step(device, request, requested_at, now=now)

        with patch.object(collector_main, "_ist_now", return_value=self.NOW):
            collector_main.run_cycle(
                database, Spool(":memory:"), collector_main.static_devices(),
                collector=lambda d: CollectionResult(list(events), transport, "SN", "FW"),
                state=self.state, door_step=step,
            )

    def _real_step(self, device, request, requested_at, *, now):
        from essl_hub import collectors

        with patch("c3.C3", self.panel):
            return collectors.c3_door_step(device, request, requested_at, now=now)

    def test_merge_carries_the_pending_request(self) -> None:
        [_, c3] = merge_device_configs(DEVICES, self._rows({"Door1Intertime": 0}, self.REQUESTED))
        self.assertEqual(c3.door_request, {"Door1Intertime": 0})
        self.assertEqual(c3.door_requested_at, self.REQUESTED)
        [_, c3] = merge_device_configs(DEVICES, self._rows("junk", "2026-10-26 10:00:00"))
        self.assertIsNone(c3.door_request)

    def test_collect_once_reports_a_history_read(self) -> None:
        database = FakeDatabase()
        history = lambda d: CollectionResult([], "TCP history", "SN", "FW")
        realtime = lambda d: CollectionResult([], "TCP realtime", "SN", "FW")
        self.assertTrue(collect_once(database, self.c3, collector=history))
        self.assertFalse(collect_once(database, self.c3, collector=realtime))

    def test_no_request_and_no_history_read_skips_the_door_step(self) -> None:
        self._cycle(FakeDatabase(), door_step=self._real_step)
        self.assertEqual(self.steps, [])

    def test_a_history_read_refreshes_the_door_read_without_a_result(self) -> None:
        database = FakeDatabase()
        self._cycle(database, transport="TCP history", door_step=self._real_step)
        # The X990 never gets a door step.
        self.assertEqual(self.steps, [(self.c3.id, None, None)])
        [record] = database.door_records
        self.assertEqual(record["params"]["Door1Intertime"], 1)
        self.assertIsNone(record["result"])
        self.assertIsNone(record["previous"])
        self.assertEqual(database.door_clears, [])

    def test_a_pending_request_is_applied_recorded_and_cleared(self) -> None:
        database = FakeDatabase(self._rows({"Door1Intertime": 0}, self.REQUESTED))
        self._cycle(database, door_step=self._real_step)
        [record] = database.door_records
        self.assertEqual(record["result"]["state"], "applied")
        self.assertEqual(record["previous"]["Door1Intertime"], 1)
        self.assertEqual(record["previous_for"], self.REQUESTED)
        self.assertEqual(database.door_clears, [(self.c3.id, self.REQUESTED)])

    def test_a_password_rejection_fails_the_request_but_punches_are_stored(self) -> None:
        self.panel.behaviour = {"Door1Intertime": -14}
        database = FakeDatabase(self._rows({"Door1Intertime": 0}, self.REQUESTED))
        punch = {"fingerprint": "fp-door", "device_id": self.c3.id}
        self._cycle(database, events=[punch], door_step=self._real_step)
        self.assertIn(punch, database.inserted_punches)
        [record] = database.door_records
        self.assertEqual(record["result"]["state"], "failed")
        self.assertEqual(record["result"]["keys"]["Door1Intertime"]["message"],
                         "communication password rejected")
        self.assertEqual(database.door_clears, [(self.c3.id, self.REQUESTED)])

    def test_an_unreachable_panel_leaves_the_request_pending(self) -> None:
        self.panel.connects = False
        database = FakeDatabase(self._rows({"Door1Intertime": 0}, self.REQUESTED))
        with self.assertLogs(collector_main._logger, level="WARNING"):
            self._cycle(database, door_step=self._real_step)
        self.assertEqual(database.door_records, [])
        self.assertEqual(database.door_clears, [])

    def test_an_expired_request_is_recorded_and_cleared_without_a_write(self) -> None:
        old = self.NOW - timedelta(minutes=20)
        database = FakeDatabase(self._rows({"Door1Intertime": 0}, old))
        self._cycle(database, door_step=self._real_step)
        self.assertEqual(self.panel.instances, [])
        [record] = database.door_records
        self.assertEqual(record["result"]["state"], "expired")
        self.assertIsNone(record["params"])
        self.assertEqual(database.door_clears, [(self.c3.id, old)])

    def test_a_door_step_crash_never_breaks_the_poll_cycle(self) -> None:
        def broken(device, request, requested_at, *, now):
            raise RuntimeError("boom")

        database = FakeDatabase(self._rows({"Door1Intertime": 0}, self.REQUESTED))
        punch = {"fingerprint": "fp-c3", "device_id": self.c3.id}
        with self.assertLogs(collector_main._logger, level="WARNING"):
            self._cycle(database, events=[punch], door_step=broken)
        self.assertIn(punch, database.inserted_punches)
        self.assertEqual({w["device_id"] for w in database.writes}, {d.id for d in DEVICES})
        self.assertIsNotNone(self.state.last_poll)

    def test_a_stale_device_list_never_resends_an_answered_request(self) -> None:
        self.panel.behaviour = {"Door1Intertime": -14}
        database = FakeDatabase(self._rows({"Door1Intertime": 0}, self.REQUESTED))
        self._cycle(database, door_step=self._real_step)
        # The web's clear has not been re-read yet (e.g. a refresh failed).
        self._cycle(database, door_step=self._real_step)
        sent = [frame for panel in self.panel.instances for frame in panel.sent]
        self.assertEqual(sent, [(0x03, b"Door1Intertime=0")])
        self.assertEqual(len(database.door_records), 1)


class DoorStepDatabaseTest(unittest.TestCase):
    """U4 against a real database: guarded clear and the KTD9 snapshot."""

    T1 = datetime(2026, 10, 26, 10, 0, 0)
    T2 = datetime(2026, 10, 26, 10, 0, 30)
    NOW = datetime(2026, 10, 26, 10, 1, 0)

    def setUp(self) -> None:
        import tempfile

        from essl_hub.db import Database
        from tests.test_collectors import _DoorPanel

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Database(os.path.join(tmp.name, "essl.db"))
        self.addCleanup(self.database.close)
        self.database.initialize(DEVICES)
        self.c3_id = DEVICES[1].id
        self.panel = _DoorPanel
        _DoorPanel.instances = []
        _DoorPanel.behaviour = {}
        _DoorPanel.connects = True

    def _device(self):
        rows = self.database.device_connection_info()
        return next(d for d in merge_device_configs(DEVICES, rows) if d.id == self.c3_id)

    def _door_once(self, device) -> None:
        from essl_hub import collectors

        with patch("c3.C3", self.panel):
            collector_main.door_once(
                self.database, device, door_step=collectors.c3_door_step, now=self.NOW
            )

    def test_a_request_replaced_meanwhile_survives_the_collectors_clear(self) -> None:
        self.database.file_door_request(self.c3_id, {"Door1Intertime": 0}, requested_at=self.T1)
        device = self._device()
        self.database.file_door_request(self.c3_id, {"Door1Intertime": 2}, requested_at=self.T2)
        self._door_once(device)
        row = self.database.device_by_id(self.c3_id)
        # The superseded T1 request never reaches the door; T2 stays pending.
        self.assertEqual(row["door_params_request"], {"Door1Intertime": 2})
        self.assertEqual(row["door_params_requested_at"], self.T2)
        self.assertIsNone(row["door_params_result"])
        self.assertEqual([f for p in self.panel.instances for f in p.sent], [])

    def test_a_request_dismissed_after_the_device_list_was_read_is_never_sent(self) -> None:
        self.database.file_door_request(self.c3_id, {"Door1Intertime": 0}, requested_at=self.T1)
        device = self._device()
        self.database.dismiss_door_request(self.c3_id)
        self._door_once(device)
        self.assertEqual([f for p in self.panel.instances for f in p.sent], [])
        self.assertIsNone(self.database.device_by_id(self.c3_id)["door_params_result"])

    def test_a_failed_result_write_is_retried_not_resent_or_expired(self) -> None:
        from essl_hub import collectors

        self.database.file_door_request(self.c3_id, {"Door1Intertime": 0}, requested_at=self.T1)
        device = self._device()
        state = collector_main.PollState()
        with patch.object(self.database, "record_door_params", side_effect=OfflineError("busy")):
            with patch("c3.C3", self.panel), self.assertRaises(OfflineError):
                collector_main.door_once(self.database, device, door_step=collectors.c3_door_step,
                                         state=state, now=self.NOW)
        later = self.T1 + timedelta(minutes=30)
        with patch("c3.C3", self.panel):
            collector_main.door_once(self.database, device, door_step=collectors.c3_door_step,
                                     state=state, now=later)
        sent = [f for p in self.panel.instances for f in p.sent]
        self.assertEqual(sent, [(0x03, b"Door1Intertime=0")])
        row = self.database.device_by_id(self.c3_id)
        self.assertEqual(row["door_params_result"]["state"], "applied")
        self.assertIsNone(row["door_params_request"])

    def test_a_drop_clears_the_request_and_keeps_both_originals(self) -> None:
        self.panel.behaviour = {"Door2Intertime": "drop"}
        self.database.file_door_request(
            self.c3_id, {"Door1Intertime": 0, "Door2Intertime": 0}, requested_at=self.T1)
        self._door_once(self._device())
        row = self.database.device_by_id(self.c3_id)
        self.assertIsNone(row["door_params_request"])
        self.assertEqual(row["door_params_result"]["state"], "failed")
        self.assertEqual(row["door_params_result"]["keys"]["Door2Intertime"]["outcome"], "not_attempted")
        self.assertEqual(row["door_params_previous"]["Door1Intertime"], 1)
        self.assertEqual(row["door_params_previous"]["Door2Intertime"], 1)
        self.assertEqual(row["door_params_previous_for"], self.T1)
        self.assertEqual(row["door_params"]["Door1Intertime"], 0)
        self.assertEqual(row["door_params_read_at"], self.NOW)

    def test_a_read_only_request_leaves_previous_unchanged(self) -> None:
        self.database.file_door_request(self.c3_id, {"Door1Intertime": 0}, requested_at=self.T1)
        self._door_once(self._device())
        self.database.file_door_request(self.c3_id, {}, kind="door_read", requested_at=self.T2)
        self._door_once(self._device())
        row = self.database.device_by_id(self.c3_id)
        self.assertEqual(row["door_params_result"]["state"], "read")
        self.assertIsNone(row["door_params_request"])
        self.assertEqual(row["door_params_previous"]["Door1Intertime"], 1)
        self.assertEqual(row["door_params_previous_for"], self.T1)

    def test_an_expired_request_keeps_the_last_read(self) -> None:
        self.database.record_door_params(
            self.c3_id, params={"LockCount": 4, "Door1Intertime": 1}, read_at=self.T1)
        self.database.file_door_request(
            self.c3_id, {"Door1Intertime": 0}, requested_at=self.NOW - timedelta(minutes=16))
        self._door_once(self._device())
        row = self.database.device_by_id(self.c3_id)
        self.assertEqual(row["door_params_result"]["state"], "expired")
        self.assertIsNone(row["door_params_request"])
        self.assertEqual(row["door_params"], {"LockCount": 4, "Door1Intertime": 1})
        self.assertEqual(row["door_params_read_at"], self.T1)
        self.assertEqual(self.panel.instances, [])


class FetchedCopyTest(unittest.TestCase):
    def test_collector_main_refuses_a_fetched_copy_but_not_a_normal_database(self) -> None:
        import tempfile

        from essl_hub.db import FETCHED_COPY_KEY, Database

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "essl.db")
            database = Database(path)
            database.initialize(())
            database.set_setting(FETCHED_COPY_KEY, "2026-10-28 from recent/x")
            database.close()
            with patch.dict(os.environ, {"ESSL_DB_PATH": path}), \
                    patch.object(collector_main, "run_forever") as run:
                with self.assertRaisesRegex(SystemExit, "fetched copy"):
                    collector_main.main()
                run.assert_not_called()
                with self.assertRaises(SystemExit):
                    collector_main.main()  # still refused on a second start
            normal = os.path.join(tmp, "normal.db")
            fresh = Database(normal)
            fresh.initialize(())
            fresh.close()
            with patch.dict(os.environ, {"ESSL_DB_PATH": normal}), \
                    patch.object(collector_main, "run_forever") as run:
                collector_main.main()
                run.assert_called_once()


if __name__ == "__main__":
    unittest.main()


def _punch(fp: str, device=DEVICES[0]) -> dict:
    return {
        "fingerprint": fp,
        "device_id": device.id,
        "site": device.site,
        "device_user_id": "101",
        "event_timestamp": "2026-10-10 09:00:00",
    }


class _SwitchableDatabase(FakeDatabase):
    """FakeDatabase whose writes fail while ``offline`` (or with ``error``)."""

    def __init__(self) -> None:
        super().__init__()
        self.error: BaseException | None = None

    def _check(self) -> None:
        if self.offline:
            raise collector_main.frappe_sync.OfflineError("database unavailable")
        if self.error is not None:
            raise self.error

    def initialize(self, devices) -> None:
        if self.offline:
            raise collector_main.frappe_sync.OfflineError("database unavailable")
        super().initialize(devices)

    def record_poll(self, device_id, **kwargs) -> int:
        self._check()
        return super().record_poll(device_id, **kwargs)

    def insert_punches(self, punches) -> int:
        self._check()
        return super().insert_punches(punches)


class SpoolCycleTest(unittest.TestCase):
    """U2 / KTD9, KTD10, KTD12: no punch is lost through an outage."""

    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.spool = Spool(os.path.join(self.tmp, "collector-spool.db"))
        self.addCleanup(self.spool.close)
        self.status_path = os.path.join(self.tmp, "collector-status.json")
        self.state = collector_main.PollState(heartbeat_seconds=300, clock=_Clock())
        self.device = DEVICES[0]
        self.batches: list[list[dict]] = []

    def _collector(self, device):
        events = self.batches.pop(0) if self.batches else []
        return CollectionResult(events, "TCP realtime", "SN-1", "FW-1")

    def _cycle(self, database) -> dict:
        collector_main.run_cycle(
            database,
            self.spool,
            [self.device],
            collector=self._collector,
            state=self.state,
            status_path=self.status_path,
        )
        import json

        with open(self.status_path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_a_write_failure_of_any_exception_type_spools_the_batch(self) -> None:
        for error in (RuntimeError("x"), ValueError("y"), KeyError("z"), OSError("w")):
            database = _SwitchableDatabase()
            database.error = error
            spool = Spool(":memory:")
            with self.assertLogs(collector_main._logger, level="WARNING"):
                collect_once(
                    database,
                    self.device,
                    collector=lambda d: CollectionResult([_punch("fp-1")], "TCP", "S", "F"),
                    state=collector_main.PollState(),
                    spool=spool,
                )
            self.assertEqual([e.punches for e in spool.pending()], [[_punch("fp-1")]])

    def test_the_next_healthy_cycle_replays_in_order_and_empties_the_spool(self) -> None:
        database = _SwitchableDatabase()
        database.error = RuntimeError("write failed")
        self.batches = [[_punch("fp-1")], [_punch("fp-2")]]
        with self.assertLogs(collector_main._logger, level="WARNING"):
            self._cycle(database)
            status = self._cycle(database)
        self.assertEqual(status["spool_depth"], 2)
        database.error = None
        status = self._cycle(database)
        self.assertEqual([p["fingerprint"] for p in database.inserted_punches], ["fp-1", "fp-2"])
        self.assertEqual(status["spool_depth"], 0)
        self.assertIsNone(status["spool_oldest_at"])

    def test_status_times_are_ist_even_when_the_host_clock_is_utc(self) -> None:
        # The web reads these as naive IST; a UTC container once made a live
        # collector look 5.5 h stale ("Collector stopped reporting").
        import time
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        with patch.dict(os.environ, {"TZ": "UTC"}):
            time.tzset()
            database = _SwitchableDatabase()
            database.offline = True
            self.batches = [[_punch("fp-1")]]
            with self.assertLogs(collector_main._logger, level="WARNING"):
                status = self._cycle(database)
        time.tzset()
        ist_now = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        for key in ("last_poll", "spool_oldest_at"):
            stamp = datetime.fromisoformat(status[key])
            self.assertLess(abs(ist_now - stamp), timedelta(minutes=1), key)

    def test_boot_with_database_unavailable_polls_spools_and_reports_then_drains(self) -> None:
        database = _SwitchableDatabase()
        database.offline = True
        self.batches = [[_punch("fp-1")]]
        with self.assertLogs(collector_main._logger, level="WARNING"):
            status = self._cycle(database)
        self.assertTrue(status["offline"])
        self.assertEqual(status["spool_depth"], 1)
        self.assertIsNotNone(status["last_poll"])
        self.assertEqual(database.initialized, 0)
        database.offline = False
        status = self._cycle(database)
        self.assertEqual(database.initialized, 1)
        self.assertFalse(status["offline"])
        self.assertEqual(status["spool_depth"], 0)
        self.assertEqual([p["fingerprint"] for p in database.inserted_punches], ["fp-1"])

    def test_missing_database_spools_and_waits_without_creating_the_file(self) -> None:
        from essl_hub.db import Database

        path = os.path.join(self.tmp, "essl.db")
        database = Database(path, must_exist=True)
        self.batches = [[_punch("fp-1")]]
        with self.assertLogs(collector_main._logger, level="WARNING") as logs:
            status = self._cycle(database)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(self.state.ready)
        self.assertTrue(any("essl_hub.backup init" in line for line in logs.output))
        self.assertEqual(status["spool_depth"], 1)
        self.assertEqual(status["dead_count"], 0)

        # A backup command creates the file; the next cycle migrates and replays.
        Database(path).migrate_schema()
        status = self._cycle(database)
        self.assertTrue(self.state.ready)
        self.assertEqual(status["spool_depth"], 0)
        with database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"], 1)

    def test_a_busy_database_spools_and_replay_waits_uncounted(self) -> None:
        from essl_hub.db import BusyError

        database = _SwitchableDatabase()
        database.error = BusyError("database is locked")
        self.batches = [[_punch("fp-1")], [_punch("fp-2")]]
        with self.assertLogs(collector_main._logger, level="WARNING"):
            self._cycle(database)
            status = self._cycle(database)
        self.assertEqual(status["spool_depth"], 2)
        self.assertEqual(status["dead_count"], 0)
        self.assertEqual(self.spool.pending()[0].attempts, 0)

    def test_a_row_failing_twenty_replays_moves_to_dead(self) -> None:
        database = _SwitchableDatabase()
        database.error = RuntimeError("bad row")
        self.batches = [[_punch("fp-1")]]
        with self.assertLogs(collector_main._logger, level="WARNING"):
            self._cycle(database)
            for _ in range(19):
                status = self._cycle(database)
            self.assertEqual(status["dead_count"], 0)
            status = self._cycle(database)
        self.assertEqual(status["dead_count"], 1)
        self.assertEqual(status["spool_depth"], 0)

    def test_failures_while_offline_do_not_count_toward_dead(self) -> None:
        database = _SwitchableDatabase()
        database.offline = True
        self.batches = [[_punch("fp-1")]]
        with self.assertLogs(collector_main._logger, level="WARNING"):
            self._cycle(database)
        self.state.ready = True  # boot work done earlier; replay runs each cycle
        with self.assertLogs(collector_main._logger, level="WARNING"):
            for _ in range(25):
                collector_main.replay_spool(database, self.spool)
        self.assertEqual(self.spool.dead_count(), 0)
        self.assertEqual(self.spool.pending()[0].attempts, 0)

    def test_status_file_is_written_by_temp_file_and_rename(self) -> None:
        database = _SwitchableDatabase()
        renames: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src, dst):
            renames.append((src, dst))
            return real_replace(src, dst)

        with patch("essl_hub.collector_main.os.replace", spy):
            for _ in range(3):
                status = self._cycle(database)
        self.assertEqual(len(renames), 3)
        for src, dst in renames:
            self.assertEqual(dst, self.status_path)
            self.assertEqual(os.path.dirname(src), os.path.dirname(dst))
        self.assertEqual(
            set(status),
            {
                "last_poll",
                "spool_depth",
                "spool_oldest_at",
                "dead_count",
                "offline",
                "c3_history",
            },
        )
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".tmp")], [])

    def test_failing_then_healthy_real_database_lands_every_punch_exactly_once(self) -> None:
        from essl_hub.db import Database

        database = Database(os.path.join(self.tmp, "essl.db"))
        self.addCleanup(database.close)
        database.initialize(DEVICES)
        real_record_poll = database.record_poll
        real_insert = database.insert_punches
        failing = {"on": True}

        def record_poll(device_id, **kwargs):
            if failing["on"]:
                raise ValueError("disk I/O error")
            return real_record_poll(device_id, **kwargs)

        def insert_punches(punches):
            if failing["on"]:
                raise ValueError("disk I/O error")
            return real_insert(punches)

        database.record_poll = record_poll
        database.insert_punches = insert_punches
        # the device re-sends fp-2 in the next batch, as real devices do
        self.batches = [[_punch("fp-1"), _punch("fp-2")], [_punch("fp-2"), _punch("fp-3")]]
        with self.assertLogs(collector_main._logger, level="WARNING"):
            self._cycle(database)
            self._cycle(database)
        failing["on"] = False
        self.batches = [[_punch("fp-3"), _punch("fp-4")]]
        status = self._cycle(database)
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT fingerprint, COUNT(*) AS n FROM punches GROUP BY fingerprint ORDER BY fingerprint"
            ).fetchall()
        self.assertEqual(
            [(r["fingerprint"], r["n"]) for r in rows],
            [("fp-1", 1), ("fp-2", 1), ("fp-3", 1), ("fp-4", 1)],
        )
        self.assertEqual(status["spool_depth"], 0)

    def test_an_interrupted_replay_rerun_produces_no_duplicates(self) -> None:
        from essl_hub.db import Database

        database = Database(os.path.join(self.tmp, "essl.db"))
        self.addCleanup(database.close)
        database.initialize(DEVICES)
        self.spool.append(self.device.id, [_punch("fp-1"), _punch("fp-2")])
        # crash between the insert and the ack: the batch is still spooled
        real_ack = self.spool.ack
        self.spool.ack = lambda entry_id: (_ for _ in ()).throw(SystemExit("crash"))
        with self.assertRaises(SystemExit):
            collector_main.replay_spool(database, self.spool)
        self.spool.ack = real_ack
        self.assertEqual(self.spool.depth(), 1)
        collector_main.replay_spool(database, self.spool)
        with database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"]
        self.assertEqual(count, 2)
        self.assertEqual(self.spool.depth(), 0)
