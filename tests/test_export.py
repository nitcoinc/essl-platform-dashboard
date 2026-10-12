from __future__ import annotations

import csv
import io
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import openpyxl

from essl_hub import db as db_module
from essl_hub import export
from essl_hub.config import DEVICES


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = db_module.Database(os.path.join(tmp.name, "essl.db"))
        self.database.initialize(DEVICES)
        self.day = date(2026, 9, 1)

    def _people(self, count: int) -> list[dict]:
        for i in range(count):
            self.database.upsert_employee(device_user_id=str(100 + i), name=f"Person {i:03d}", email=f"p{i}@x.com")
        return self.database.employees()

    def _punch(self, uid: str, at: datetime, direction: str, n: int) -> dict:
        return {
            "fingerprint": f"fp-{uid}-{n}", "device_id": DEVICES[0].id, "site": "s", "device_serial": "x",
            "device_user_id": uid, "employee_name": "", "event_timestamp": at, "raw_state": "0",
            "door": "1", "reader": "", "raw_direction_byte": 0, "trusted_direction": direction, "raw": {},
        }

    def _rows(self, people, start, end):
        return export.export_rows(
            self.database, people, start, end, manager_names={}, target_minutes=480,
            today=date(2026, 10, 10), now=None,
        )

    def test_incomplete_day_exports_as_incomplete_with_zero_minutes(self) -> None:
        people = self._people(1)
        at = datetime.combine(self.day, datetime.min.time()) + timedelta(hours=9)
        self.database.insert_punches([self._punch("100", at, "entry", 1)])
        (row,) = self._rows(people, self.day, self.day)
        self.assertEqual(row[export.HEADER.index("status")], "incomplete")
        self.assertEqual(row[export.HEADER.index("effective_minutes")], 0)

    def test_formula_text_is_prefixed_in_csv_and_xlsx(self) -> None:
        self.database.upsert_employee(device_user_id="7", name='=HYPERLINK("x")', email="h@x.com")
        rows = self._rows(self.database.employees(), self.day, self.day)
        parsed = list(csv.DictReader(io.StringIO(export.to_csv(rows))))
        self.assertEqual(parsed[0]["name"], '\'=HYPERLINK("x")')
        sheet = openpyxl.load_workbook(io.BytesIO(export.to_xlsx(rows))).active
        cell = sheet.cell(row=2, column=2)
        self.assertEqual(cell.value, '\'=HYPERLINK("x")')
        self.assertEqual(cell.data_type, "s")
        for prefix in ("+", "-", "@", "\t", "\r"):
            self.assertEqual(export.safe_text(prefix + "a"), "'" + prefix + "a")
        self.assertEqual(export.safe_text("plain"), "plain")

    def test_xlsx_has_headers_frozen_pane_and_numeric_minutes(self) -> None:
        people = self._people(1)
        start = datetime.combine(self.day, datetime.min.time())
        self.database.insert_punches([
            self._punch("100", start + timedelta(hours=9), "entry", 1),
            self._punch("100", start + timedelta(hours=17, minutes=30), "exit", 2),
        ])
        workbook = openpyxl.load_workbook(io.BytesIO(export.to_xlsx(self._rows(people, self.day, self.day))))
        sheet = workbook.active
        self.assertEqual([c.value for c in sheet[1]], list(export.HEADER))
        self.assertEqual(sheet.freeze_panes, "A2")
        self.assertEqual(sheet.cell(row=2, column=6).value, 510)
        self.assertEqual(sheet.cell(row=2, column=6).data_type, "n")
        self.assertEqual(sheet.cell(row=2, column=7).data_type, "n")

    def test_range_validation(self) -> None:
        today = date(2026, 10, 10)
        self.assertEqual(export.parse_range(None, None, today), (today, today))
        self.assertEqual(export.parse_range("2026-07-01", "2026-10-01", today)[1], date(2026, 10, 1))  # 93 days
        for bad in (("2026-10-02", "2026-10-01"), ("2026-06-30", "2026-10-01"), ("nope", "2026-10-01")):
            with self.assertRaises(export.RangeError):
                export.parse_range(*bad, today)

    def test_query_count_is_constant_for_100_people_by_31_days(self) -> None:
        statements: list[str] = []
        original = db_module.Database._open

        def counting_open(database):
            connection = original(database)
            connection.set_trace_callback(
                lambda sql: statements.append(sql) if not sql.lstrip().upper().startswith("PRAGMA") else None
            )
            return connection

        def run(people, days) -> int:
            statements.clear()
            with mock.patch.object(db_module.Database, "_open", counting_open):
                rows = self._rows(people, self.day, self.day + timedelta(days=days - 1))
            self.assertEqual(len(rows), len(people) * days)
            return len(statements)

        everyone = self._people(100)
        start = datetime.combine(self.day, datetime.min.time())
        self.database.insert_punches(
            [self._punch(p["device_user_id"], start + timedelta(days=d, hours=9), "entry", d) for p in everyone for d in range(31)]
        )
        small = run(everyone[:2], 1)
        large = run(everyone, 31)
        self.assertEqual(small, large)
        self.assertLessEqual(large, 1)


if __name__ == "__main__":
    unittest.main()
