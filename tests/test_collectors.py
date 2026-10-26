from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".packages"))
sys.path.insert(0, str(ROOT))

import os
from unittest.mock import patch

from essl_hub.collectors import _c3_history_direction, _c3_trusted_direction, _normalized_event, c3_identity, c3_user_id
from essl_hub.config import DEVICES


class CollectorTests(unittest.TestCase):
    def test_event_identity_does_not_change_when_serial_lookup_recovers(self) -> None:
        values = {
            "raw": {"user_id": "7", "timestamp": "2026-09-10 10:00:00"},
            "user_id": "7",
            "employee_name": "",
            "timestamp": "2026-09-10 10:00:00",
            "raw_state": "punch",
        }
        without_serial = _normalized_event(DEVICES[0], serial_number="", **values)
        with_serial = _normalized_event(DEVICES[0], serial_number="SN-1", **values)
        self.assertEqual(without_serial["fingerprint"], with_serial["fingerprint"])

    def test_normalized_event_carries_raw_byte_and_device_user_id(self) -> None:
        event = _normalized_event(
            DEVICES[0],
            raw={"pin": "118"},
            serial_number="SN-1",
            user_id="118",
            employee_name="Ravi Kumar",
            timestamp="2026-09-10 09:00:00",
            raw_state="0",
            raw_direction_byte=0,
            trusted_direction="entry",
        )
        self.assertEqual(event["device_user_id"], "118")
        self.assertEqual(event["raw_direction_byte"], 0)
        self.assertEqual(event["trusted_direction"], "entry")


class C3UserIdTest(unittest.TestCase):
    def test_pin_maps_into_the_x990_format(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(c3_user_id("9"), "NP0009")
            self.assertEqual(c3_user_id("173"), "NP0173")
            self.assertEqual(c3_user_id("1001"), "NP1001")

    def test_pin_zero_is_unlinked(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(c3_user_id("0"), "C3-0")
            self.assertEqual(c3_user_id("000"), "C3-000")
            self.assertEqual(c3_user_id("128"), "NP0128")

    def test_shared_cards_stay_unlinked(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(c3_user_id("2"), "C3-2")
            self.assertEqual(c3_user_id("1"), "C3-1")
        with patch.dict(os.environ, {"ESSL_C3_UNLINKED_PINS": "5"}):
            self.assertEqual(c3_user_id("2"), "NP0002")
            self.assertEqual(c3_user_id("5"), "C3-5")

    def test_format_is_configurable_and_card_is_the_fallback(self) -> None:
        with patch.dict(os.environ, {"ESSL_C3_ID_FORMAT": "{pin}"}):
            self.assertEqual(c3_user_id("9"), "9")
        with patch.dict(os.environ, {"ESSL_C3_ID_FORMAT": ""}):
            self.assertEqual(c3_user_id("9"), "NP0009")
        self.assertEqual(c3_user_id("", "2507221"), "2507221")

    def test_history_and_realtime_reads_of_one_swipe_share_a_fingerprint(self) -> None:
        device = next(d for d in DEVICES if d.protocol == "c3-pullsdk")
        common = dict(serial_number="", user_id="NP0173", employee_name="", timestamp="2026-10-10 01:21:40", raw_state="")
        # The history table returns the card ASCII-packed; the realtime log returns the number.
        history = _normalized_event(device, raw={"Pin": 173, "Cardno": 925906226},
                                    identity=c3_identity("173", "925906226", "2026-10-10 01:21:40", "1", "0"), **common)
        realtime = _normalized_event(device, raw={"pin": "173", "source": "realtime_fallback"},
                                     identity=c3_identity("173", "2507221", "2026-10-10 01:21:40", "1", "0"), **common)
        self.assertEqual(history["fingerprint"], realtime["fingerprint"])


def _frame(command: int, payload: bytes) -> bytes:
    return bytes([0xAA, 0x01, command, len(payload) & 0xFF, len(payload) >> 8]) + payload + b"\x00\x00\x55"


def _table_buffer(records: list[tuple[int, int, int]]) -> bytes:
    body = bytearray([5, 3, 2, 5, 7])  # table 5, fields Pin, EventType, Time_second
    for pin, event, ts in records:
        for value in (pin, event, ts):
            raw = value.to_bytes(4, "little")
            body += bytes([len(raw)]) + raw
    return bytes(body)


class _FakeSocket:
    def __init__(self, panel):
        self.panel = panel
        self.pending = b""

    def settimeout(self, _):
        pass

    def recv(self, count):
        if not self.pending:
            self.pending = self.panel.next_reply()
        out, self.pending = self.pending[:count], self.pending[count:]
        return out


class _Field:
    def __init__(self, name, index):
        self.name, self.index, self.type = name, index, "i"


class _Cfg:
    name, index = "transaction", 5
    fields = [_Field("Pin", 2), _Field("EventType", 5), _Field("Time_second", 7)]


class _FakePanel:
    def __init__(self, buffer: bytes, chunk: int = 7, large: bool = True, bad_crc: bool = False):
        import struct, zlib
        self.buffer, self.chunk, self.large = buffer, chunk, large
        crc = zlib.crc32(buffer) ^ (1 if bad_crc else 0)
        self.announce = b"\x00" + struct.pack("<IIII", len(buffer), len(buffer), crc, chunk)
        self._sock = _FakeSocket(self)
        self.sent = []

    def _get_device_data_cfg(self):
        return [_Cfg()]

    def _send(self, command, data):
        self.sent.append((command, bytes(data)))

    def next_reply(self):
        import struct
        command, data = self.sent[-1]
        if command == 0x08:
            return _frame(0x0D, self.announce) if self.large else _frame(0xC8, self.buffer)
        offset, size = struct.unpack("<II", data)
        return _frame(0xC8, struct.pack("<I", offset) + self.buffer[offset : offset + size])


class C3TableDownloadTest(unittest.TestCase):
    RECORDS = [(9, 0, 860548788), (173, 20, 860548873), (2, 0, 860548900)]

    def test_large_table_is_read_in_chunks_and_parsed(self) -> None:
        from essl_hub.collectors import read_c3_table

        panel = _FakePanel(_table_buffer(self.RECORDS), chunk=7)
        rows = read_c3_table(panel, "transaction")
        self.assertEqual([(r["Pin"], r["EventType"]) for r in rows], [(9, 0), (173, 20), (2, 0)])
        reads = [c for c, _ in panel.sent if c == 0x0E]
        self.assertGreater(len(reads), 3)

    def test_small_table_comes_in_one_reply(self) -> None:
        from essl_hub.collectors import read_c3_table

        rows = read_c3_table(_FakePanel(_table_buffer(self.RECORDS[:1]), large=False), "transaction")
        self.assertEqual(rows, [{"Pin": 9, "EventType": 0, "Time_second": 860548788}])

    def test_checksum_mismatch_is_refused(self) -> None:
        from essl_hub.collectors import read_c3_table

        with self.assertRaises(ConnectionError):
            read_c3_table(_FakePanel(_table_buffer(self.RECORDS), bad_crc=True), "transaction")

    def test_granted_and_interval_too_short_punches_count(self) -> None:
        from essl_hub.collectors import _c3_is_punch

        self.assertTrue(_c3_is_punch("0"))
        self.assertTrue(_c3_is_punch("20"))  # KTD11
        for denied in ("27", "24", "202", "206", "220"):
            self.assertFalse(_c3_is_punch(denied))

    def test_history_due_at_start_then_every_interval(self) -> None:
        from essl_hub import collectors

        collectors._C3_HISTORY_AT.pop("t", None)
        self.assertTrue(collectors._c3_history_due("t"))
        collectors._C3_HISTORY_AT["t"] = collectors.time.monotonic()
        self.assertFalse(collectors._c3_history_due("t"))
        with patch.dict(os.environ, {"ESSL_C3_HISTORY_HOURS": "0"}):
            self.assertTrue(collectors._c3_history_due("t"))


class _RtRecord:
    def __init__(self, event_type, pin, in_out, when, card="0", door=1):
        self.event_type, self.pin, self.card_no = event_type, pin, card
        self.in_out_state, self.time_second, self.port_nr, self.verified = in_out, when, door, 1

    def is_event(self):
        return True


class _FakeC3:
    rt_batches: list = []

    def __init__(self, *a, **k):
        self._batches = list(type(self).rt_batches)

    def connect(self, password=None):
        return True

    def disconnect(self):
        pass

    def get_device_param(self, names):
        return {}

    def get_device_data(self, table):
        return []

    def get_rt_log(self):
        return self._batches.pop(0) if self._batches else []


class C3IntervalTooShortTest(unittest.TestCase):
    """U12/KTD11: event 20 is stored as a flagged swipe."""

    def setUp(self) -> None:
        self.device = next(d for d in DEVICES if d.protocol == "c3-pullsdk")

    def _history(self, rows):
        from essl_hub import collectors

        with patch("c3.C3", _FakeC3), patch.object(collectors, "read_c3_table", return_value=rows), \
                patch.dict(os.environ, {"ESSL_C3_MODE": "history"}):
            return collectors.collect_c3(self.device).events

    def _realtime(self, records):
        from essl_hub import collectors

        _FakeC3.rt_batches = [records]
        with patch("c3.C3", _FakeC3), patch.dict(os.environ, {"ESSL_C3_MODE": "realtime"}):
            return collectors.collect_c3(self.device).events

    @staticmethod
    def _row(event, pin="173", card="0", in_out="1", second=800000000):
        return {"Pin": pin, "Cardno": card, "Time_second": str(second), "EventType": event,
                "InOutState": in_out, "DoorID": "1", "ReaderID": "1"}

    def test_history_event_20_with_pin_is_a_flagged_exit(self) -> None:
        [event] = self._history([self._row("20")])
        self.assertEqual(event["trusted_direction"], "exit")
        self.assertIn("interval too short", event["raw_state"])
        self.assertEqual(event["device_user_id"], c3_user_id("173"))

    def test_realtime_event_20_dedupes_with_history_read(self) -> None:
        from c3.utils import C3DateTime

        when = C3DateTime.from_value(800000000)
        [rt] = self._realtime([_RtRecord(20, "173", 15, when)])
        [hist] = self._history([self._row("20")])
        self.assertEqual(rt["fingerprint"], hist["fingerprint"])
        self.assertIn("interval too short", rt["raw_state"])
        # KTD4: the panel only emits bytes 0/1, so realtime 15 on event 20 is an exit.
        self.assertEqual(rt["trusted_direction"], "exit")

    def test_realtime_directions_for_granted_and_interval_too_short(self) -> None:
        from c3.utils import C3DateTime

        entry, short_exit = self._realtime([
            _RtRecord(0, "173", 0, C3DateTime.from_value(800000000)),
            _RtRecord(20, "173", 15, C3DateTime.from_value(800000005)),
        ])
        self.assertEqual(entry["trusted_direction"], "entry")
        self.assertEqual(entry["raw_state"], "0 · 0")
        self.assertEqual(short_exit["trusted_direction"], "exit")

    def test_event_20_without_pin_or_card_is_dropped(self) -> None:
        self.assertEqual(self._history([self._row("20", pin="", card="0")]), [])
        self.assertEqual(self._history([self._row("20", pin="0", card="")]), [])

    def test_other_denied_events_still_dropped(self) -> None:
        self.assertEqual(self._history([self._row(e) for e in ("27", "24", "202", "206")]), [])

    def test_two_people_back_to_back_both_stored(self) -> None:
        events = self._history([self._row("0", pin="173"), self._row("20", pin="39", second=800000001)])
        self.assertEqual({e["device_user_id"] for e in events}, {c3_user_id("173"), c3_user_id("39")})


class C3DoorDeniedTest(unittest.TestCase):
    """KTD5: interlock (25) and anti-passback (24) denials are captured only
    at/after the device's door_denied_since."""

    SINCE_SECOND = 800000000

    def setUp(self) -> None:
        from dataclasses import replace

        from c3.utils import C3DateTime

        base = next(d for d in DEVICES if d.protocol == "c3-pullsdk")
        self.since = C3DateTime.from_value(self.SINCE_SECOND)
        self.policy_off = base
        self.policy_on = replace(base, door_denied_since=self.since)

    def _history(self, rows, device):
        from essl_hub import collectors

        with patch("c3.C3", _FakeC3), patch.object(collectors, "read_c3_table", return_value=rows), \
                patch.dict(os.environ, {"ESSL_C3_MODE": "history"}):
            return collectors.collect_c3(device).events

    def _realtime(self, records, device):
        from essl_hub import collectors

        _FakeC3.rt_batches = [records]
        with patch("c3.C3", _FakeC3), patch.dict(os.environ, {"ESSL_C3_MODE": "realtime"}):
            return collectors.collect_c3(device).events

    _row = staticmethod(C3IntervalTooShortTest._row)

    def test_history_event_25_after_since_is_a_flagged_exit(self) -> None:
        [event] = self._history([self._row("25", second=self.SINCE_SECOND + 60)], self.policy_on)
        self.assertEqual(event["trusted_direction"], "exit")
        self.assertIn("door denied: interlock", event["raw_state"])
        [event] = self._history([self._row("24", in_out="0", second=self.SINCE_SECOND)], self.policy_on)
        self.assertEqual(event["trusted_direction"], "entry")  # at `since` counts
        self.assertIn("door denied: anti-passback", event["raw_state"])

    def test_door_denied_before_since_or_with_policy_off_is_dropped(self) -> None:
        self.assertEqual(self._history([self._row("25", second=self.SINCE_SECOND - 1)], self.policy_on), [])
        self.assertEqual(self._history([self._row("25", second=self.SINCE_SECOND + 60)], self.policy_off), [])

    def test_door_denied_without_pin_or_card_is_dropped(self) -> None:
        rows = [self._row(e, pin=pin, card=card, second=self.SINCE_SECOND + 60)
                for e in ("24", "25") for pin, card in (("", "0"), ("0", ""))]
        self.assertEqual(self._history(rows, self.policy_on), [])

    def test_event_27_is_always_dropped(self) -> None:
        self.assertEqual(self._history([self._row("27", second=self.SINCE_SECOND + 60)], self.policy_on), [])

    def test_realtime_event_25_with_15_has_no_direction_and_dedupes_with_history(self) -> None:
        from c3.utils import C3DateTime

        when = C3DateTime.from_value(self.SINCE_SECOND + 60)
        [rt] = self._realtime([_RtRecord(25, "173", 15, when)], self.policy_on)
        self.assertIsNone(rt["trusted_direction"])
        self.assertIn("door denied: interlock", rt["raw_state"])
        [hist] = self._history([self._row("25", second=self.SINCE_SECOND + 60)], self.policy_on)
        self.assertEqual(rt["fingerprint"], hist["fingerprint"])
        self.assertEqual(hist["trusted_direction"], "exit")  # what fill_punch_directions applies

    def test_realtime_event_25_with_policy_off_is_dropped(self) -> None:
        from c3.utils import C3DateTime

        when = C3DateTime.from_value(self.SINCE_SECOND + 60)
        self.assertEqual(self._realtime([_RtRecord(25, "173", 15, when)], self.policy_off), [])


class C3TrustedDirectionTest(unittest.TestCase):
    def test_byte_zero_is_trusted_as_entry(self) -> None:
        self.assertEqual(_c3_trusted_direction(0), "entry")

    def test_byte_three_is_trusted_as_exit(self) -> None:
        self.assertEqual(_c3_trusted_direction(3), "exit")

    def test_byte_fifteen_on_a_card_event_is_an_exit(self) -> None:
        # The library cannot decode the real exit byte (1) and keeps only its
        # fallback 15; the raw byte is not exposed on the record.
        for event_type in (0, 1, 2, 3):
            self.assertEqual(_c3_trusted_direction(15, event_type), "exit")

    def test_byte_fifteen_on_interval_too_short_is_an_exit(self) -> None:
        # KTD4: history carries only bytes 0/1, so 15 is byte 1 for event 20 too.
        self.assertEqual(_c3_trusted_direction(15, 20), "exit")

    def test_byte_fifteen_on_event_20_is_unknown_when_the_rule_is_off(self) -> None:
        from essl_hub import collectors

        # Gate-miss branch: the pre-implementation check could have failed.
        with patch.object(collectors, "C3_REALTIME_15_EXIT_EVENT_TYPES", frozenset(collectors.C3_GRANTED_EVENT_TYPES)):
            self.assertIsNone(_c3_trusted_direction(15, 20))
            self.assertEqual(_c3_trusted_direction(15, 0), "exit")

    def test_byte_fifteen_without_a_card_event_is_not_trusted(self) -> None:
        self.assertIsNone(_c3_trusted_direction(15))
        self.assertIsNone(_c3_trusted_direction(15, 8))
        # Door-denied rows wait for a history read (KTD4).
        self.assertIsNone(_c3_trusted_direction(15, 24))
        self.assertIsNone(_c3_trusted_direction(15, 25))

    def test_entry_and_exit_bytes_ignore_the_event_type(self) -> None:
        self.assertEqual(_c3_trusted_direction(0, 0), "entry")
        self.assertEqual(_c3_trusted_direction(3, 0), "exit")

    def test_byte_two_none_is_not_trusted(self) -> None:
        self.assertIsNone(_c3_trusted_direction(2))

    def test_history_table_bytes_are_protocol_bytes(self) -> None:
        self.assertEqual(_c3_history_direction("0"), "entry")
        self.assertEqual(_c3_history_direction("1"), "exit")
        self.assertIsNone(_c3_history_direction("2"))


if __name__ == "__main__":
    unittest.main()
