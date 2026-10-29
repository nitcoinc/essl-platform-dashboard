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


def _rt_frame(event: int, pin: int, in_out: int, second: int, door: int = 1) -> bytes:
    """One 16-byte binary live-log record, laid out as c3.rtlog reads it."""
    return (b"\x00\x00\x00\x00" + pin.to_bytes(4, "little") + bytes([1, door, event, in_out])
            + second.to_bytes(4, "little"))


class _FakeRawC3(_FakeC3):
    """A panel answering the live-log command with raw frames, as the real
    library's ``_send_receive`` does: binary, or key/value firmware."""

    messages: list = []
    key_value = False

    def __init__(self, *a, **k):
        from c3 import consts

        super().__init__()
        self._messages = list(type(self).messages)
        self._rtlog_command = consts.Command.RTLOG_KEYVALUE if self.key_value else consts.Command.RTLOG_BINARY
        self.statuses: list = []

    def is_connected(self):
        return True

    def _send_receive(self, command):
        message = self._messages.pop(0) if self._messages else b""
        return message, len(message)

    def _parse_kv_from_message(self, message):
        return dict(pair.split("=", 1) for pair in message.decode().split("\t") if "=" in pair)

    def _update_inout_status(self, records):
        self.statuses.append(len(records))


class C3RawInOutTest(unittest.TestCase):
    """R3: the panel's own live-log in/out value is kept beside the decode."""

    SECOND = 800000000

    def setUp(self) -> None:
        self.device = next(d for d in DEVICES if d.protocol == "c3-pullsdk")

    def _realtime(self, *messages, key_value=False):
        from essl_hub import collectors

        _FakeRawC3.messages = list(messages)
        with patch("c3.C3", _FakeRawC3), patch.object(_FakeRawC3, "key_value", key_value), patch.dict(os.environ, {"ESSL_C3_MODE": "realtime", "ESSL_C3_RTLOG_BATCH": "3"}):
            return collectors.collect_c3(self.device).events

    def test_a_byte_the_library_cannot_decode_is_kept_raw_with_no_direction(self) -> None:
        [event] = self._realtime(_rt_frame(0, 173, 1, self.SECOND))
        self.assertEqual(event["raw"]["in_out_raw"], 1)
        self.assertEqual(event["raw_direction_byte"], 15)
        self.assertIsNone(event["trusted_direction"])

    def test_a_decoded_entry_keeps_its_raw_zero_and_direction(self) -> None:
        [event] = self._realtime(_rt_frame(0, 173, 0, self.SECOND))
        self.assertEqual((event["raw"]["in_out_raw"], event["raw_direction_byte"], event["trusted_direction"]),
                         (0, 0, "entry"))

    def test_several_records_in_one_reply_keep_their_own_values(self) -> None:
        events = self._realtime(_rt_frame(0, 173, 0, self.SECOND) + _rt_frame(0, 174, 4, self.SECOND + 1))
        self.assertEqual([e["raw"]["in_out_raw"] for e in events], [0, 4])

    def test_key_value_mode_keeps_inoutstatus(self) -> None:
        kv = "\t".join(["time=2025-05-09 10:13:20", "pin=173", "cardno=0", "eventaddr=1", "event=0",
                        "inoutstatus=2", "verifytype=1", "index=1"]).encode()
        [event] = self._realtime(kv, key_value=True)
        self.assertEqual(event["raw"]["in_out_raw"], 2)
        self.assertIsNone(event["trusted_direction"])

    def test_the_fingerprint_still_matches_the_history_twin(self) -> None:
        from essl_hub import collectors

        [live] = self._realtime(_rt_frame(0, 173, 1, self.SECOND))
        row = {"Pin": "173", "Cardno": "0", "Time_second": str(self.SECOND), "EventType": "0",
               "InOutState": "1", "DoorID": "1", "ReaderID": "1"}
        with patch("c3.C3", _FakeC3), patch.object(collectors, "read_c3_table", return_value=[row]), \
                patch.dict(os.environ, {"ESSL_C3_MODE": "history"}):
            [history] = collectors.collect_c3(self.device).events
        self.assertEqual(live["fingerprint"], history["fingerprint"])

    def test_a_reply_that_is_not_whole_records_switches_to_key_value(self) -> None:
        from c3 import consts
        from essl_hub.collectors import read_c3_rt_log

        _FakeRawC3.messages = [b"\x01" * 7]
        panel = _FakeRawC3()
        self.assertEqual(read_c3_rt_log(panel), [])
        self.assertEqual(panel._rtlog_command, consts.Command.RTLOG_KEYVALUE)
        self.assertEqual(panel.statuses, [0])

    def test_a_panel_without_the_internals_falls_back_to_get_rt_log(self) -> None:
        from c3.utils import C3DateTime
        from essl_hub import collectors

        _FakeC3.rt_batches = [[_RtRecord(0, "173", 15, C3DateTime.from_value(self.SECOND))]]
        with patch("c3.C3", _FakeC3), patch.dict(os.environ, {"ESSL_C3_MODE": "realtime"}):
            [event] = collectors.collect_c3(self.device).events
        self.assertNotIn("in_out_raw", event["raw"])


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
        # KTD4 (revised): realtime 15 carries no direction; history fills it.
        self.assertIsNone(rt["trusted_direction"])

    def test_realtime_directions_for_granted_and_interval_too_short(self) -> None:
        from c3.utils import C3DateTime

        entry, short_unknown = self._realtime([
            _RtRecord(0, "173", 0, C3DateTime.from_value(800000000)),
            _RtRecord(20, "173", 15, C3DateTime.from_value(800000005)),
        ])
        self.assertEqual(entry["trusted_direction"], "entry")
        self.assertEqual(entry["raw_state"], "0 · 0")
        self.assertIsNone(short_unknown["trusted_direction"])
        self.assertEqual(short_unknown["raw_direction_byte"], 15)

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

    def test_byte_fifteen_is_unknown_on_every_event(self) -> None:
        # 2026-10-29: the live log returns 15 for arrivals (outside reader)
        # and leavings (inside reader) alike, so it carries no direction;
        # a history read fills the real byte later.
        for event_type in (None, 0, 1, 2, 3, 8, 20, 24, 25):
            self.assertIsNone(_c3_trusted_direction(15, event_type), event_type)

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


_PANEL_VALUES = {
    "LockCount": "4", "ReaderCount": "4", "AntiPassback": "0", "InterLock": "0",
    **{f"Door{n}{k}": v for n in range(1, 5) for k, v in (
        ("Intertime", "1"), ("Drivertime", "5"), ("Detectortime", "15"),
        ("CloseAndLock", "1"), ("SensorType", "0"), ("VerifyType", "7"))},
}


class _DoorPanel:
    """Fake C3 for the door step. ``behaviour`` maps a key to what its
    SETPARAM does: "keep" (0xC8 but the panel keeps its value), an int error
    code (0xC9), "drop" (socket send fails) or "drop_after" (the reply never
    comes). Anything else stores the value."""

    instances: list["_DoorPanel"] = []
    behaviour: dict = {}
    connects = True

    def __init__(self, *a, **k):
        self.values = dict(_PANEL_VALUES)
        self.sent: list[tuple[int, bytes]] = []
        self.param_calls: list[list[str]] = []
        self.dead = False
        type(self).instances.append(self)

    def connect(self, password=None):
        if not type(self).connects:
            raise ConnectionError("timed out")
        return True

    def disconnect(self):
        pass

    @classmethod
    def _kv_to_message(cls, data):
        from c3.core import C3

        return C3._kv_to_message(data)

    def get_device_param(self, names):
        if self.dead:
            raise ConnectionError("Invalid response header received; expected 5 bytes, received b''")
        self.param_calls.append(list(names))
        return {name: self.values[name] for name in names if name in self.values}

    def _send_receive(self, command, data=None):
        if self.dead:
            raise ConnectionError("Unexpected connection end: [Errno 32] Broken pipe") from BrokenPipeError()
        key, value = bytes(data).decode("ascii").split("=")
        action = type(self).behaviour.get(key)
        if action == "drop":
            self.dead = True
            raise ConnectionError("Unexpected connection end: [Errno 32] Broken pipe") from BrokenPipeError()
        self.sent.append((command, bytes(data)))
        if isinstance(action, int):
            raise ConnectionError(f"Error {action} received in reply: Unknown")
        if action == "drop_after":
            self.dead = True
            raise ConnectionError("Invalid response header received; expected 5 bytes, received b''")
        if action != "keep":
            self.values[key] = value
        return bytearray(), 0


class C3DoorStepTest(unittest.TestCase):
    """U4: KTD6/KTD7/KTD9 door-parameter reads and writes, on a fake panel."""

    def setUp(self) -> None:
        from datetime import datetime

        self.device = next(d for d in DEVICES if d.protocol == "c3-pullsdk")
        self.now = datetime(2026, 10, 26, 10, 5, 0)
        self.requested_at = datetime(2026, 10, 26, 10, 0, 0)
        _DoorPanel.instances = []
        _DoorPanel.behaviour = {}
        _DoorPanel.connects = True

    def _step(self, request, requested_at="default"):
        from essl_hub import collectors

        if requested_at == "default":
            requested_at = self.requested_at
        with patch("c3.C3", _DoorPanel):
            return collectors.c3_door_step(self.device, request, requested_at, now=self.now)

    def _sent(self):
        return [frame for panel in _DoorPanel.instances for frame in panel.sent]

    def test_plain_read_parses_every_door_in_batches_of_at_most_30(self) -> None:
        outcome = self._step(None, requested_at=None)
        self.assertIsNone(outcome.result)
        self.assertIsNone(outcome.previous)
        self.assertEqual(outcome.params["LockCount"], 4)
        self.assertEqual(outcome.params["Door4Detectortime"], 15)
        self.assertEqual(outcome.params["Door2VerifyType"], 7)
        self.assertEqual(outcome.read_at, self.now)
        calls = _DoorPanel.instances[0].param_calls
        self.assertEqual(calls[0], ["LockCount"])
        self.assertTrue(all(len(call) <= 30 for call in calls))
        self.assertEqual(self._sent(), [])

    def test_applied_when_the_read_back_matches(self) -> None:
        outcome = self._step({"Door1Intertime": 0})
        self.assertEqual(self._sent(), [(0x03, b"Door1Intertime=0")])
        self.assertEqual(outcome.result["state"], "applied")
        self.assertEqual(outcome.result["requested_at"], "2026-10-26 10:00:00")
        self.assertEqual(
            outcome.result["keys"]["Door1Intertime"],
            {"outcome": "applied", "old": 1, "new": 0, "now": 0, "message": ""},
        )
        self.assertEqual(outcome.previous["Door1Intertime"], 1)
        self.assertEqual(outcome.params["Door1Intertime"], 0)

    def test_unchanged_keys_are_not_sent(self) -> None:
        outcome = self._step({"Door1Intertime": 1, "InterLock": 0})
        self.assertEqual(self._sent(), [])
        self.assertEqual(outcome.result["state"], "applied")
        self.assertEqual({e["outcome"] for e in outcome.result["keys"].values()}, {"unchanged"})
        self.assertIsNone(outcome.previous)

    def test_a_value_the_panel_kept_fails_that_key_only(self) -> None:
        _DoorPanel.behaviour = {"Door1Intertime": "keep"}
        outcome = self._step({"Door1Intertime": 0, "Door2Intertime": 0})
        keys = outcome.result["keys"]
        self.assertEqual(outcome.result["state"], "failed")
        self.assertEqual(keys["Door1Intertime"]["outcome"], "failed")
        self.assertEqual(keys["Door1Intertime"]["message"], "panel kept 1")
        self.assertEqual(keys["Door2Intertime"]["outcome"], "applied")
        self.assertEqual(outcome.params["Door1Intertime"], 1)

    def test_0xc9_is_decoded_and_the_next_key_still_runs(self) -> None:
        _DoorPanel.behaviour = {"Door1Intertime": -14, "InterLock": -13}
        outcome = self._step({"Door1Intertime": 0, "InterLock": 1, "Door2Intertime": 0})
        keys = outcome.result["keys"]
        self.assertEqual(outcome.result["state"], "failed")
        self.assertEqual(keys["Door1Intertime"]["message"], "communication password rejected")
        self.assertEqual(keys["InterLock"]["message"], "command not available")
        self.assertEqual(keys["Door2Intertime"]["outcome"], "applied")

    def test_a_drop_after_key_one_fails_the_request_and_sends_nothing_more(self) -> None:
        _DoorPanel.behaviour = {"Door2Intertime": "drop"}
        outcome = self._step({"Door1Intertime": 0, "Door2Intertime": 0, "InterLock": 1})
        keys = outcome.result["keys"]
        self.assertEqual(outcome.result["state"], "failed")
        self.assertEqual(keys["Door1Intertime"]["outcome"], "applied")
        self.assertEqual(keys["Door2Intertime"]["outcome"], "not_attempted")
        self.assertEqual(keys["InterLock"]["outcome"], "not_attempted")
        self.assertIn("connection lost", outcome.result["message"])
        self.assertEqual(self._sent(), [(0x03, b"Door1Intertime=0")])
        self.assertEqual(outcome.previous["Door1Intertime"], 1)
        self.assertEqual(outcome.previous["Door2Intertime"], 1)

    def test_a_lost_reply_leaves_that_key_unconfirmed(self) -> None:
        _DoorPanel.behaviour = {"Door1Intertime": "drop_after"}
        outcome = self._step({"Door1Intertime": 0, "Door2Intertime": 0})
        keys = outcome.result["keys"]
        self.assertEqual(keys["Door1Intertime"]["outcome"], "failed")
        self.assertIn("not confirmed", keys["Door1Intertime"]["message"])
        self.assertEqual(keys["Door2Intertime"]["outcome"], "not_attempted")

    def test_read_from_device_returns_a_read_result_and_no_snapshot(self) -> None:
        outcome = self._step({})
        self.assertEqual(outcome.result["state"], "read")
        self.assertEqual(outcome.result["keys"], {})
        self.assertIsNone(outcome.previous)
        self.assertEqual(outcome.params["Door1Intertime"], 1)

    def test_a_request_older_than_15_minutes_expires_without_connecting(self) -> None:
        from datetime import timedelta

        outcome = self._step({"Door1Intertime": 0}, requested_at=self.now - timedelta(minutes=16))
        self.assertEqual(_DoorPanel.instances, [])
        self.assertEqual(outcome.result["state"], "expired")
        self.assertEqual(outcome.result["keys"]["Door1Intertime"]["outcome"], "not_attempted")
        self.assertIsNone(outcome.params)
        self.assertIsNone(outcome.previous)

    def test_the_collector_revalidates_against_the_fresh_lock_count(self) -> None:
        for request in ({"ComPwd": 1}, {"Door5Intertime": 0}, {"Door1Intertime": "0,ComPwd=1"}):
            _DoorPanel.instances = []
            outcome = self._step(request)
            self.assertEqual(outcome.result["state"], "failed")
            self.assertTrue(outcome.result["message"])
            self.assertEqual(self._sent(), [])

    def test_connect_failure_raises_without_the_password(self) -> None:
        from dataclasses import replace

        _DoorPanel.connects = False
        self.device = replace(self.device, password="timed")
        with self.assertRaises(ConnectionError) as caught:
            self._step({"Door1Intertime": 0})
        self.assertNotIn("timed", str(caught.exception))
