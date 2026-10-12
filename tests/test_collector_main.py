from __future__ import annotations

import os
import unittest
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
from essl_hub.spool import Spool


def _seeded_rows() -> list[dict]:
    """What device_connection_info returns right after seeding config.DEVICES."""
    return [
        {"id": d.id, "site": d.site, "model": d.model, "protocol": d.protocol,
         "ip": d.ip, "port": d.port, "comm_password": None, "connection_updated_at": None}
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
        self.last_sync_ok_at = None
        self.last_sync_error: str | None = None
        self.initialized = 0
        self.direction_fills: list[list[dict]] = []

    def sync(self) -> bool:
        return not self.offline

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
        with patch.object(self.database, "sync", return_value=True):
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
            raise collector_main.frappe_sync.OfflineError("Turso is unreachable")
        if self.error is not None:
            raise self.error

    def sync(self) -> bool:
        if self.offline:
            self.last_sync_error = "Turso is unreachable"
            return False
        self.last_sync_error = None
        return True

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

    def test_boot_with_turso_unreachable_polls_spools_and_reports_then_drains(self) -> None:
        database = _SwitchableDatabase()
        database.offline = True
        self.batches = [[_punch("fp-1")]]
        with self.assertLogs(collector_main._logger, level="WARNING"):
            status = self._cycle(database)
        self.assertTrue(status["offline"])
        self.assertEqual(status["last_sync_error"], "Turso is unreachable")
        self.assertEqual(status["spool_depth"], 1)
        self.assertIsNotNone(status["last_poll"])
        self.assertEqual(database.initialized, 0)
        database.offline = False
        status = self._cycle(database)
        self.assertEqual(database.initialized, 1)
        self.assertFalse(status["offline"])
        self.assertEqual(status["spool_depth"], 0)
        self.assertEqual([p["fingerprint"] for p in database.inserted_punches], ["fp-1"])

    def test_boot_against_an_unreachable_real_turso_url_does_not_block(self) -> None:
        import time as _time

        from essl_hub.db import Database

        database = Database(
            os.path.join(self.tmp, "replica.db"),
            sync_url="https://127.0.0.1:1",
            auth_token="t",
        )
        self.batches = [[_punch("fp-1")]]
        started = _time.monotonic()
        with self.assertLogs(level="WARNING"):
            status = self._cycle(database)
        self.assertLess(_time.monotonic() - started, 30)
        self.assertTrue(status["offline"])
        self.assertTrue(status["last_sync_error"])
        self.assertEqual(status["spool_depth"], 1)

    def test_a_row_failing_twenty_replays_while_syncs_succeed_moves_to_dead(self) -> None:
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
                "last_sync_ok_at",
                "last_sync_error",
                "replica_rebuilt_at",
                "spool_depth",
                "spool_oldest_at",
                "dead_count",
                "offline",
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
                raise ValueError("sync error: http dispatch error")
            return real_record_poll(device_id, **kwargs)

        def insert_punches(punches):
            if failing["on"]:
                raise ValueError("sync error: http dispatch error")
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
