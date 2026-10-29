"""U5/R6: the read-only list of C3 live-log punches Frappe got as OUT on a guess."""

from __future__ import annotations

import csv
import importlib.util
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from essl_hub.config import DEVICES
from essl_hub.db import Database

_SPEC = importlib.util.spec_from_file_location(
    "c3_guessed_frappe_report", Path(__file__).resolve().parent.parent / "scripts" / "c3_guessed_frappe_report.py")
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)

AT = datetime(2026, 10, 28, 17, 51, 31)  # after the 24 h hold reached production
BEFORE_HOLD = datetime(2026, 10, 20, 9, 0)
BEFORE_RULE = datetime(2026, 10, 10, 9, 0)
CUTOVER = datetime(2026, 10, 30, 9, 0)
HOLD = report.FRAPPE_GUESS_HOLD


class GuessedFrappeReportTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "essl.db")
        self.database = Database(self.path)
        self.addCleanup(self.database.close)
        self.database.initialize(DEVICES)
        self.database.upsert_employee(device_user_id="NP0009", name="Pavan M", email="pavan@example.com")

    def _punch(self, fp, *, at=AT, live_byte=15, byte=None, direction=None, synced_after=None, live=True,
               event=0, uid="NP0009"):
        """A stored punch. ``live_byte`` is what the live log decoded (kept in
        raw_json); ``byte``/``direction`` are the row now, after any fill."""
        raw = ({"pin": "9", "event_type": event, "in_out_state": live_byte, "source": "realtime_fallback"}
               if live else {"Pin": 9, "EventType": str(event)})
        self.database.insert_punches([{
            "fingerprint": fp, "device_id": DEVICES[1].id, "site": DEVICES[1].site, "device_user_id": uid,
            "event_timestamp": at, "raw_direction_byte": live_byte if byte is None else byte,
            "trusted_direction": direction, "raw": raw}])
        if synced_after is not None:
            with self.database.transaction() as connection:
                punch_id = connection.execute("SELECT id FROM punches WHERE fingerprint=?", (fp,)).fetchone()["id"]
                connection.execute(
                    "INSERT INTO frappe_sync_log (punch_id, status, checkin_name, synced_at) VALUES (?, 'synced', ?, ?)",
                    (punch_id, f"CK-{fp}", at + synced_after))

    def _run(self, cutover=CUTOVER) -> list[dict]:
        if cutover is not None:
            self.database.set_setting(report.CUTOVER_KEY, cutover.isoformat(sep=" "))
        out = io.StringIO()
        count = report.report(self.path, out)
        rows = list(csv.DictReader(io.StringIO(out.getvalue())))
        self.assertEqual(count, len(rows))
        return rows

    @staticmethod
    def _summary(rows) -> list[tuple[str, str, str]]:
        return [(r["checkin_name"], r["sent_log_type"], r["direction_now"]) for r in rows]

    def test_hold_era_lists_unconfirmed_and_late_filled_entries(self) -> None:
        minute = timedelta(minutes=1)
        self._punch("still-15", synced_after=HOLD + minute)
        self._punch("filled-entry-late", at=AT + minute, byte=0, direction="entry", synced_after=HOLD + timedelta(hours=1))
        self._punch("filled-exit-late", at=AT + 2 * minute, byte=1, direction="exit", synced_after=HOLD + timedelta(hours=1))
        self._punch("confirmed-first", at=AT + 3 * minute, byte=0, direction="entry", synced_after=timedelta(hours=1))
        self._punch("never-sent", at=AT + 4 * minute)
        self._punch("history-row", at=AT + 5 * minute, live=False, byte=1, direction="exit",
                    synced_after=timedelta(hours=1))
        self._punch("decoded-entry", at=AT + 6 * minute, live_byte=0, direction="entry", synced_after=HOLD)
        self._punch("after-cutover", at=AT + 7 * minute, synced_after=timedelta(days=3))
        self._punch("door-denied", at=AT + 8 * minute, event=25, synced_after=HOLD + minute)
        self._punch("interval-short", at=AT + 9 * minute, event=20, synced_after=HOLD + minute)
        self.assertEqual(self._summary(self._run()), [
            ("CK-still-15", "OUT", "unconfirmed"),
            ("CK-filled-entry-late", "possibly OUT", "In"),
            ("CK-interval-short", "OUT", "unconfirmed"),
        ])

    def test_before_the_hold_a_guess_went_out_at_once_even_if_history_filled_it_later(self) -> None:
        self._punch("pre-hold", at=BEFORE_HOLD, byte=0, direction="entry", synced_after=timedelta(minutes=1))
        self._punch("pre-hold-exit", at=BEFORE_HOLD + timedelta(minutes=5), byte=1, direction="exit",
                    synced_after=timedelta(minutes=1))
        self._punch("before-rule", at=BEFORE_RULE, byte=0, direction="entry", synced_after=timedelta(minutes=1))
        [row] = self._run()
        self.assertEqual((row["checkin_name"], row["sent_log_type"], row["direction_now"]),
                         ("CK-pre-hold", "OUT", "In"))
        self.assertEqual((row["employee"], row["day"], row["time"]), ("Pavan M", "2026-10-20", "09:00:00"))

    def test_a_punch_under_an_extra_device_id_names_its_owner(self) -> None:
        owner = self.database.employee_by_device_user_id("NP0009")
        self.database.add_extra_device_id(owner["id"], "9")
        self._punch("extra", uid="9", synced_after=HOLD + timedelta(minutes=1))
        [row] = self._run()
        self.assertEqual((row["employee"], row["device_user_id"]), ("Pavan M", "9"))

    def test_no_cutover_on_a_v11_file_lists_nothing(self) -> None:
        self._punch("still-15", synced_after=HOLD + timedelta(minutes=1))
        self.assertEqual(self._run(cutover=None), [])

    def test_no_cutover_on_an_older_file_counts_every_send(self) -> None:
        self._punch("still-15", synced_after=HOLD + timedelta(minutes=1))
        self.database.set_setting("schema_version", "10")
        self.assertEqual(self._summary(self._run(cutover=None)), [("CK-still-15", "OUT", "unconfirmed")])

    def test_an_unreadable_sync_time_is_skipped(self) -> None:
        self._punch("still-15", synced_after=HOLD + timedelta(minutes=1))
        with self.database.transaction() as connection:
            connection.execute("UPDATE frappe_sync_log SET synced_at='soon'")
        self.assertEqual(self._run(), [])

    def test_an_empty_result_prints_only_the_header(self) -> None:
        out = io.StringIO()
        self.assertEqual(report.report(self.path, out), 0)
        self.assertEqual(out.getvalue().strip(), ",".join(report.COLUMNS))

    def test_the_file_is_opened_read_only(self) -> None:
        with mock.patch.object(report.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            report.report(self.path, io.StringIO())
        [(uri,), kwargs] = connect.call_args
        self.assertIn("mode=ro", uri)
        self.assertTrue(kwargs.get("uri"))


if __name__ == "__main__":
    unittest.main()
