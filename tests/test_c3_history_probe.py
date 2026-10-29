"""U4: the read-only history probe measures the collector's download and
tests the panel capabilities a faster download would rely on."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import struct
import tempfile
import unittest
import unittest.mock
import zlib
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "c3_history_probe", Path(__file__).resolve().parent.parent / "scripts" / "c3_history_probe.py")
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def _frame(command: int, payload: bytes) -> bytes:
    return bytes([0xAA, 0x01, command, len(payload) & 0xFF, len(payload) >> 8]) + payload + b"\x00\x00\x55"


def _buffer(rows: int) -> bytes:
    body = bytearray([5, 1, 2])  # table 5, one field: Pin (index 2)
    for pin in range(rows):
        body += bytes([4]) + pin.to_bytes(4, "little")
    return bytes(body)


class _Field:
    name, index, type = "Pin", 2, "i"


class _Cfg:
    name, index, fields = "transaction", 5, [_Field()]


class _Socket:
    def __init__(self, panel):
        self.panel = panel
        self.pending = b""

    def settimeout(self, _):
        pass

    def recv(self, count):
        if not self.pending:
            if not self.panel.replies:
                raise ConnectionResetError(104, "Connection reset by peer")
            self.pending = self.panel.replies.pop(0)
        out, self.pending = self.pending[:count], self.pending[count:]
        return out


class _Panel:
    """Queues one reply per command, so pipelined reads work when allowed.
    ``reset_after`` buffer reads, the panel drops the session; ``max_read``
    caps what one read returns (a panel that ignores longer requests)."""

    def __init__(self, buffer: bytes, chunk: int = 10, reset_after: int | None = None,
                 max_read: int | None = None, pipelines: bool = True):
        self.buffer, self.chunk = buffer, chunk
        self.reset_after, self.max_read, self.pipelines = reset_after, max_read, pipelines
        self._sock = _Socket(self)
        self.replies: list[bytes] = []
        self.sent: list[tuple[int, bytes]] = []
        self.reads = 0
        self.disconnected = False

    def _get_device_data_cfg(self):
        return [_Cfg()]

    def _send(self, command, data):
        data = bytes(data)
        self.sent.append((command, data))
        if command == 0x08:
            announce = b"\x00" + struct.pack("<IIII", len(self.buffer), len(self.buffer),
                                             zlib.crc32(self.buffer), self.chunk)
            self.replies.append(_frame(0x0D, announce))
            return
        self.reads += 1
        if self.reset_after is not None and self.reads > self.reset_after:
            self.replies.clear()
            return
        if not self.pipelines and self.replies:
            self.replies.clear()  # a panel that answers only the latest request
            return
        offset, size = struct.unpack("<II", data)
        size = min(size, self.max_read or size)
        self.replies.append(_frame(0xC8, struct.pack("<I", offset) + self.buffer[offset: offset + size]))

    def get_device_param(self, names):
        return {}

    def get_device_data(self, table):
        return [{"Pin": "1"}]

    def disconnect(self):
        self.disconnected = True


class _Clock:
    def __init__(self, step=1.0):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


class DownloadTest(unittest.TestCase):
    def test_a_full_download_reports_size_chunk_speed_and_rows(self) -> None:
        panel = _Panel(_buffer(20), chunk=10)
        result = probe.download(panel, connected_at=0.0, clock=_Clock(0.5), say=lambda _: None)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["total"], result["chunk"], result["rows"]), (len(panel.buffer), 10, 20))
        self.assertEqual(result["offset"], len(panel.buffer))
        self.assertGreater(result["bytes_per_s"], 0)
        self.assertIn("rtt_avg_ms", result)

    def test_a_reset_records_where_and_when_the_panel_cut_it_off(self) -> None:
        panel = _Panel(_buffer(20), chunk=10, reset_after=3)
        lines = []
        result = probe.download(panel, connected_at=0.0, clock=_Clock(2.0), say=lines.append)
        self.assertFalse(result["ok"])
        self.assertEqual(result["offset"], 30)
        self.assertIn("ConnectionResetError", result["error"])
        self.assertGreater(result["since_connect_s"], result["since_getdata_s"] - 0.001)
        self.assertTrue(any("bytes" in line for line in lines))  # progress every 5 s


class CapabilityTest(unittest.TestCase):
    def test_a_panel_that_honours_long_reads_returns_more_than_a_chunk(self) -> None:
        result = probe.read_length(_Panel(_buffer(50), chunk=10), 100_000)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["asked"], len(_buffer(50)))  # capped at the table size
        self.assertGreater(result["got"], 10)

    def test_a_panel_that_caps_reads_at_its_chunk_is_not_ok(self) -> None:
        result = probe.read_length(_Panel(_buffer(50), chunk=10, max_read=10), 100)
        self.assertFalse(result["ok"])
        self.assertEqual(result["got"], 10)

    def test_the_read_length_is_capped_at_the_frame_maximum(self) -> None:
        panel = _Panel(_buffer(20000), chunk=10)
        result = probe.read_length(panel, 1_000_000)
        self.assertEqual(result["asked"], probe.C3_MAX_READ_BYTES)

    def test_pipelined_reads_answered_in_order_are_ok(self) -> None:
        result = probe.pipeline(_Panel(_buffer(50), chunk=10), 4)
        self.assertEqual((result["ok"], result["depth"]), (True, 4))

    def test_a_panel_that_drops_queued_reads_fails_the_pipeline(self) -> None:
        result = probe.pipeline(_Panel(_buffer(50), chunk=10, pipelines=False), 4)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


class RunTest(unittest.TestCase):
    def test_every_mode_runs_on_its_own_connection_and_disconnects(self) -> None:
        panels = []

        def connect():
            panels.append(_Panel(_buffer(30), chunk=10))
            return panels[-1]

        lines = []
        code = probe.run(list(probe.MODES), connect=connect, read_bytes=1000, depth=3,
                         say=lines.append, pause=lambda _: None)
        self.assertEqual(code, 0)
        self.assertEqual(len(panels), 4)
        self.assertTrue(all(p.disconnected for p in panels))
        self.assertTrue(all(cmd in (0x08, 0x0E) for p in panels for cmd, _ in p.sent))  # read-only
        self.assertIn("\n== pipeline ==", lines)

    def test_a_reset_in_a_capability_test_stops_the_run(self) -> None:
        panels = []

        def connect():
            panels.append(_Panel(_buffer(30), chunk=10, reset_after=0 if len(panels) == 2 else None))
            return panels[-1]

        lines = []
        code = probe.run(list(probe.MODES), connect=connect, read_bytes=1000, depth=3,
                         say=lines.append, pause=lambda _: None)
        self.assertEqual(code, 1)
        self.assertEqual(len(panels), 3)  # pipeline never ran
        self.assertIn("  the panel refused or reset; stopping here", lines)


class EdgeTest(unittest.TestCase):
    def test_a_table_that_fits_one_reply_needs_no_buffer_reads(self) -> None:
        class _OneReply(_Panel):
            def _send(self, command, data):
                self.sent.append((command, bytes(data)))
                self.replies.append(_frame(0xC8, self.buffer))

        for check in (lambda p: probe.download(p, connected_at=0.0, say=lambda _: None),
                      lambda p: probe.read_length(p, 1000), lambda p: probe.pipeline(p, 3)):
            panel = _OneReply(_buffer(2))
            self.assertTrue(check(panel)["ok"])
            self.assertEqual([c for c, _ in panel.sent], [0x08])

    def test_a_failed_connect_ends_the_run(self) -> None:
        def connect():
            raise ConnectionError("rejected")

        lines = []
        self.assertEqual(probe.run(["measure"], connect=connect, read_bytes=1, depth=1, say=lines.append,
                                   pause=lambda _: None), 1)
        self.assertIn("  could not connect: ConnectionError: rejected", lines)

    def test_a_reset_before_the_download_is_reported_not_raised(self) -> None:
        class _ParamsReset(_Panel):
            def get_device_param(self, names):
                raise ConnectionResetError(104, "Connection reset by peer")

        panels = []

        def connect():
            panels.append(_ParamsReset(_buffer(30), chunk=10) if not panels else _Panel(_buffer(30), chunk=10))
            return panels[-1]

        lines = []
        code = probe.run(["measure", "fresh-session"], connect=connect, read_bytes=1, depth=1,
                         say=lines.append, pause=lambda _: None)
        self.assertEqual(code, 1)
        self.assertTrue(any("ConnectionResetError" in line for line in lines))
        self.assertIn("\n== fresh-session ==", lines)  # carried on
        self.assertTrue(all(p.disconnected for p in panels))


class CollectorGuardTest(unittest.TestCase):
    NOW = probe.datetime(2026, 10, 30, 21, 0)

    def _status(self, payload) -> str:
        import json

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "collector-status.json")
        if payload is not None:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        return path

    def test_a_recent_poll_means_the_collector_is_running(self) -> None:
        recent = self._status({"last_poll": "2026-10-30 20:59:30"})
        old = self._status({"last_poll": "2026-10-30 20:50:00"})
        self.assertTrue(probe.collector_is_polling(recent, self.NOW))
        self.assertFalse(probe.collector_is_polling(old, self.NOW))
        self.assertFalse(probe.collector_is_polling(self._status(None), self.NOW))
        self.assertFalse(probe.collector_is_polling(self._status({"last_poll": None}), self.NOW))

    def test_main_refuses_while_the_collector_polls_unless_forced(self) -> None:
        with unittest.mock.patch.object(probe, "collector_is_polling", return_value=True), \
                unittest.mock.patch.object(probe, "device_settings") as settings, \
                unittest.mock.patch("sys.stderr"):
            self.assertEqual(probe.main([]), 2)
        settings.assert_not_called()
        with unittest.mock.patch.object(probe, "collector_is_polling", return_value=True), \
                unittest.mock.patch.object(probe, "device_settings", return_value=("c3", "10.0.0.1", 4370, None)), \
                unittest.mock.patch.object(probe, "run", return_value=0) as run, \
                unittest.mock.patch("builtins.print"):
            self.assertEqual(probe.main(["--force"]), 0)
        run.assert_called_once()


class DeviceSettingsTest(unittest.TestCase):
    def test_reads_the_c3_row_read_only_and_falls_back_to_the_env_password(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "essl.db")
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE devices (id TEXT, ip TEXT, port INTEGER, comm_password TEXT, "
                               "protocol TEXT, retired_at TEXT)")
            connection.executemany("INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?)", [
                ("uppal-x990", "10.0.0.1", 4370, "1", "zk-attendance", None),
                ("gachibowli-c3-400", "10.10.16.6", 4370, None, "c3-pullsdk", None)])
        with unittest.mock.patch.dict(os.environ, {"ESSL_C3_PASSWORD": "pw"}):
            self.assertEqual(probe.device_settings(path, None), ("gachibowli-c3-400", "10.10.16.6", 4370, "pw"))
        with self.assertRaises(SystemExit):
            probe.device_settings(path, "missing")


if __name__ == "__main__":
    unittest.main()
