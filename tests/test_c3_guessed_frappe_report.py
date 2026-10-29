"""U5/R6: the read-only list of C3 live-log punches Frappe got as OUT on a guess."""

from __future__ import annotations

import csv
import importlib.util
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from essl_hub.config import DEVICES
from essl_hub.db import Database

_SPEC = importlib.util.spec_from_file_location(
    "c3_guessed_frappe_report", Path(__file__).resolve().parent.parent / "scripts" / "c3_guessed_frappe_report.py")
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)

AT = datetime(2026, 10, 28, 17, 51, 31)
CUTOVER = datetime(2026, 10, 30, 9, 0)


class GuessedFrappeReportTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "essl.db")
        self.database = Database(self.path)
        self.addCleanup(self.database.close)
        self.database.initialize(DEVICES)
        self.database.upsert_employee(device_user_id="NP0009", name="Pavan M", email="pavan@example.com")

    def _punch(self, fp, *, byte, direction, synced_after=None, live=True, at=AT, event=0):
        raw = {"pin": "9", "event_type": event, "source": "realtime_fallback"} if live else {"Pin": 9}
        self.database.insert_punches([{
            "fingerprint": fp, "device_id": DEVICES[1].id, "site": DEVICES[1].site, "device_user_id": "NP0009",
            "event_timestamp": at, "raw_direction_byte": byte, "trusted_direction": direction, "raw": raw}])
        if synced_after is not None:
            with self.database.transaction() as connection:
                punch_id = connection.execute("SELECT id FROM punches WHERE fingerprint=?", (fp,)).fetchone()["id"]
                connection.execute(
                    "INSERT INTO frappe_sync_log (punch_id, status, checkin_name, synced_at) VALUES (?, 'synced', ?, ?)",
                    (punch_id, f"EMP-CKIN-{fp}", at + synced_after))

    def _run(self, cutover=CUTOVER) -> list[dict]:
        if cutover is not None:
            self.database.set_setting(report.CUTOVER_KEY, cutover.isoformat(sep=" "))
        out = io.StringIO()
        count = report.report(self.path, out)
        rows = list(csv.DictReader(io.StringIO(out.getvalue())))
        self.assertEqual(count, len(rows))
        return rows

    def test_lists_only_outs_that_may_be_wrong(self) -> None:
        hold = report.FRAPPE_GUESS_HOLD
        self._punch("still-15", byte=15, direction=None, synced_after=hold + timedelta(minutes=1))
        self._punch("filled-entry-late", byte=0, direction="entry", synced_after=hold + timedelta(hours=1),
                    at=AT + timedelta(minutes=1))
        self._punch("filled-exit-late", byte=1, direction="exit", synced_after=hold + timedelta(hours=1),
                    at=AT + timedelta(minutes=2))
        self._punch("confirmed-first", byte=0, direction="entry", synced_after=timedelta(hours=1),
                    at=AT + timedelta(minutes=3))
        self._punch("never-sent", byte=15, direction=None, at=AT + timedelta(minutes=4))
        self._punch("history-row", byte=1, direction="exit", synced_after=timedelta(hours=1), live=False,
                    at=AT + timedelta(minutes=5))
        self._punch("after-cutover", byte=15, direction=None, synced_after=timedelta(days=3),
                    at=AT + timedelta(minutes=6))
        self._punch("door-denied", byte=15, direction=None, synced_after=hold + timedelta(minutes=1),
                    at=AT + timedelta(minutes=7), event=25)
        rows = self._run()
        self.assertEqual([(r["checkin_name"], r["sent_log_type"], r["direction_now"]) for r in rows], [
            ("EMP-CKIN-still-15", "OUT", "unconfirmed"),
            ("EMP-CKIN-filled-entry-late", "possibly OUT", "In"),
        ])
        self.assertEqual((rows[0]["employee"], rows[0]["day"], rows[0]["time"]), ("Pavan M", "2026-10-28", "17:51:31"))

    def test_an_empty_result_prints_only_the_header(self) -> None:
        out = io.StringIO()
        self.assertEqual(report.report(self.path, out), 0)
        self.assertEqual(out.getvalue().strip(), ",".join(report.COLUMNS))

    def test_the_file_is_opened_read_only(self) -> None:
        self._punch("still-15", byte=15, direction=None, synced_after=report.FRAPPE_GUESS_HOLD)
        before = os.path.getmtime(self.path)
        self._run(cutover=None)
        self.assertEqual(os.path.getmtime(self.path), before)


if __name__ == "__main__":
    unittest.main()
