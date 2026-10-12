from __future__ import annotations

import io
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import openpyxl

from essl_hub import db as db_module

from essl_hub.roster import (
    RosterFileError,
    apply_roster,
    apply_rows,
    parse_xlsx,
)


class FakeDatabase:
    def __init__(self) -> None:
        self.employees: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, str, str]] = []

    def upsert_employee(self, *, device_user_id: str, name: str, email: str) -> str:
        self.calls.append((device_user_id, name, email))
        result = "updated" if device_user_id in self.employees else "added"
        self.employees[device_user_id] = {"name": name, "email": email}
        return result

    def employee_by_email(self, email: str):
        for device_user_id, row in self.employees.items():
            if row["email"].lower() == email.lower():
                return {**row, "device_user_id": device_user_id}
        return None


VALID_CSV = (
    "name,work email,device user id\r\n"
    "Abhiram,abhiram@example.com,18\r\n"
    "Ravi Kumar,ravi.kumar@example.com,118\r\n"
    "Karthik,karthik@example.com,184\r\n"
)


class ApplyRosterTest(unittest.TestCase):
    def test_valid_file_creates_one_employee_per_row(self) -> None:
        database = FakeDatabase()
        outcomes = apply_roster(database, VALID_CSV)
        self.assertEqual(len(outcomes), 3)
        self.assertTrue(all(o.result == "added" for o in outcomes))
        self.assertEqual(len(database.employees), 3)

    def test_reupload_with_corrected_name_updates_and_creates_none(self) -> None:
        database = FakeDatabase()
        apply_roster(database, VALID_CSV)
        corrected = VALID_CSV.replace("Karthik,karthik@", "Karthik Reddy,karthik@")
        outcomes = apply_roster(database, corrected)
        self.assertTrue(all(o.result == "updated" for o in outcomes))
        self.assertEqual(len(database.employees), 3)
        self.assertEqual(database.employees["184"]["name"], "Karthik Reddy")

    def test_file_missing_email_column_is_rejected_with_no_rows_written(self) -> None:
        database = FakeDatabase()
        broken = "name,device user id\r\nAbhiram,18\r\n"
        with self.assertRaises(RosterFileError):
            apply_roster(database, broken)
        self.assertEqual(database.calls, [])

    def test_two_rows_sharing_an_email_are_both_rejected(self) -> None:
        database = FakeDatabase()
        csv_text = (
            "name,work email,device user id\r\n"
            "Abhiram,shared@example.com,18\r\n"
            "Ravi Kumar,shared@example.com,118\r\n"
            "Karthik,karthik@example.com,184\r\n"
        )
        outcomes = apply_roster(database, csv_text)
        rejected = [o for o in outcomes if o.result == "rejected"]
        self.assertEqual({o.row_number for o in rejected}, {1, 2})
        self.assertEqual(len(database.employees), 1)

    def test_email_with_no_at_sign_is_reported_for_that_row(self) -> None:
        database = FakeDatabase()
        csv_text = "name,work email,device user id\r\nMeghana,meghana.rgmail.com,106\r\n"
        outcomes = apply_roster(database, csv_text)
        self.assertEqual(outcomes[0].result, "rejected")
        self.assertIn("@", outcomes[0].reason)

    def test_bom_and_windows_line_endings_parse_correctly(self) -> None:
        database = FakeDatabase()
        csv_text = "﻿" + VALID_CSV
        outcomes = apply_roster(database, csv_text)
        self.assertEqual(len(outcomes), 3)
        self.assertTrue(all(o.result == "added" for o in outcomes))


# --- U5: .xlsx roster uploads -------------------------------------------


def _xlsx_bytes(rows, *, title: str = "Sheet1", extra_sheets=None) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(list(row))
    for extra_title, extra_rows in (extra_sheets or {}).items():
        extra = workbook.create_sheet(extra_title)
        for row in extra_rows:
            extra.append(list(row))
    workbook.active = 0
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _rewrite_sheet_xml(source: bytes, transform) -> bytes:
    """Rewrite the active sheet's XML so tests can build payloads (raw
    numeric literals, XML entity declarations) that openpyxl will not write."""
    sheet_name = "xl/worksheets/sheet1.xml"
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        names = archive.namelist()
        contents = {name: archive.read(name) for name in names}
    contents[sheet_name] = transform(contents[sheet_name].decode("utf-8")).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, contents[name])
    return buffer.getvalue()


class ParseXlsxTest(unittest.TestCase):
    def test_well_formed_xlsx_matches_the_equivalent_csv_outcomes(self) -> None:
        csv_database = FakeDatabase()
        csv_outcomes = apply_roster(csv_database, VALID_CSV)

        payload = _xlsx_bytes(
            [
                ["name", "work email", "device user id"],
                ["Abhiram", "abhiram@example.com", "18"],
                ["Ravi Kumar", "ravi.kumar@example.com", "118"],
                ["Karthik", "karthik@example.com", "184"],
            ]
        )
        xlsx_database = FakeDatabase()
        xlsx_outcomes = apply_rows(xlsx_database, parse_xlsx(payload))

        self.assertEqual(xlsx_outcomes, csv_outcomes)
        self.assertEqual(xlsx_database.employees, csv_database.employees)

    def test_header_aliases_and_row_numbers_match_the_csv_path(self) -> None:
        payload = _xlsx_bytes(
            [
                ["Name", "Email", "ID"],
                ["Karthik", "karthik@example.com", "184"],
                ["Vijay", "vijay@example.com", "55"],
            ]
        )
        self.assertEqual(
            parse_xlsx(payload),
            [
                (1, "Karthik", "karthik@example.com", "184", None, None),
                (2, "Vijay", "vijay@example.com", "55", None, None),
            ],
        )

    def test_numeric_device_user_id_cells_coerce_without_a_decimal_point(self) -> None:
        payload = _xlsx_bytes(
            [
                ["name", "work email", "device user id"],
                ["Karthik", "karthik@example.com", 1023],
            ]
        )
        self.assertEqual(parse_xlsx(payload)[0][3], "1023")

        # Excel-compatible writers can store the same number as "1023.0".
        decimal_payload = _rewrite_sheet_xml(
            payload, lambda xml: xml.replace("<v>1023</v>", "<v>1023.0</v>")
        )
        self.assertEqual(parse_xlsx(decimal_payload)[0][3], "1023")

    def test_trailing_empty_rows_are_skipped(self) -> None:
        payload = _xlsx_bytes(
            [
                ["name", "work email", "device user id"],
                ["Karthik", "karthik@example.com", "184"],
                [None, None, None],
                ["", "   ", None],
            ]
        )
        self.assertEqual(
            parse_xlsx(payload), [(1, "Karthik", "karthik@example.com", "184", None, None)]
        )

    def test_multi_sheet_workbook_uses_the_active_sheet(self) -> None:
        payload = _xlsx_bytes(
            [
                ["name", "work email", "device user id"],
                ["Karthik", "karthik@example.com", "184"],
            ],
            title="Roster",
            extra_sheets={
                "Notes": [
                    ["name", "work email", "device user id"],
                    ["Ghost", "ghost@example.com", "999"],
                ]
            },
        )
        self.assertEqual(
            parse_xlsx(payload), [(1, "Karthik", "karthik@example.com", "184", None, None)]
        )

    def test_blank_active_sheet_reports_the_sheet_name(self) -> None:
        payload = _xlsx_bytes([], title="Roster")
        with self.assertRaises(RosterFileError) as ctx:
            parse_xlsx(payload)
        self.assertIn("Roster", str(ctx.exception))

    def test_missing_column_reports_the_sheet_name(self) -> None:
        payload = _xlsx_bytes(
            [["name", "device user id"], ["Karthik", "184"]], title="Roster"
        )
        with self.assertRaises(RosterFileError) as ctx:
            parse_xlsx(payload)
        self.assertIn("work email", str(ctx.exception))
        self.assertIn("Roster", str(ctx.exception))

    def test_corrupt_bytes_are_rejected(self) -> None:
        with self.assertRaises(RosterFileError) as ctx:
            parse_xlsx(b"PK\x03\x04 this is not a real workbook")
        self.assertIn("xlsx", str(ctx.exception))

    def test_xml_entity_payload_is_rejected_without_resolving_the_target(self) -> None:
        secret = "es-should-never-be-read"
        with TemporaryDirectory() as directory:
            secret_path = Path(directory) / "secret.txt"
            secret_path.write_text(secret, encoding="utf-8")
            payload = _xlsx_bytes(
                [
                    ["name", "work email", "device user id"],
                    ["Karthik", "karthik@example.com", "184"],
                ]
            )
            payload = _rewrite_sheet_xml(
                payload,
                lambda xml: (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                    f'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "{secret_path.as_uri()}">]>\n'
                    + xml
                ),
            )
            with self.assertRaises(RosterFileError) as ctx:
                parse_xlsx(payload)
            self.assertNotIn(secret, str(ctx.exception))

    def test_exactly_the_ceiling_of_data_rows_is_accepted(self) -> None:
        rows = [("name", "work email", "device user id")]
        rows.extend(
            (f"Person {i}", f"person{i}@example.com", str(i)) for i in range(5000)
        )
        parsed = parse_xlsx(_xlsx_bytes(rows))
        self.assertEqual(len(parsed), 5000)
        self.assertEqual(parsed[-1][0], 5000)

    def test_row_count_past_the_ceiling_is_rejected(self) -> None:
        rows = [("name", "work email", "device user id")]
        rows.extend(
            (f"Person {i}", f"person{i}@example.com", str(i)) for i in range(5001)
        )
        with self.assertRaises(RosterFileError) as ctx:
            parse_xlsx(_xlsx_bytes(rows))
        self.assertIn("5000", str(ctx.exception))


class UpsertConflictTest(unittest.TestCase):
    def test_email_already_used_by_another_employee_is_rejected_per_row(self) -> None:
        class ConflictingDatabase(FakeDatabase):
            def upsert_employee(self, *, device_user_id, name, email):
                raise db_module.IntegrityError("duplicate key value violates unique constraint")

        database = ConflictingDatabase()
        outcomes = apply_rows(
            database, [(1, "Karthik", "karthik@example.com", "184")]
        )
        self.assertEqual(outcomes[0].result, "rejected")
        self.assertIn("already used", outcomes[0].reason)


class CaseInsensitiveEmailTest(unittest.TestCase):
    def setUp(self) -> None:
        from essl_hub.config import DEVICES
        from essl_hub.db import Database

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Database(str(Path(tmp.name) / "essl.db"))
        self.database.initialize(DEVICES)
        self.database.upsert_employee(device_user_id="18", name="Abhiram", email="abhiram@example.com")

    def test_case_variant_email_for_another_device_id_is_rejected_not_duplicated(self) -> None:
        outcomes = apply_rows(self.database, [(1, "Abhi", "ABHIRAM@ExAmple.com", "99")])
        self.assertEqual(outcomes[0].result, "rejected")
        self.assertEqual(outcomes[0].reason, "work email already used by another employee")
        self.assertEqual(len(self.database.employees()), 1)

    def test_case_variant_email_for_same_device_id_updates_and_stores_lowercase(self) -> None:
        outcomes = apply_rows(self.database, [(1, "Abhiram K", "Abhiram@EXAMPLE.com", "18")])
        self.assertEqual(outcomes[0].result, "updated")
        self.assertEqual(self.database.employees()[0]["email"], "abhiram@example.com")

    def test_schema_unique_email_ignores_case(self) -> None:
        with self.database.transaction() as connection, self.assertRaises(db_module.IntegrityError):
            connection.execute(
                "INSERT INTO employees (device_user_id, name, email, created_at, updated_at) "
                "VALUES ('7', 'X', 'ABHIRAM@example.com', '2026-01-01', '2026-01-01')"
            )


if __name__ == "__main__":
    unittest.main()


# --- U6: Manager Email and Role columns (KTD9) ----------------------------


class RosterManagersAndRolesTest(unittest.TestCase):
    HEADER = "name,work email,device user id,manager email,role\r\n"

    def setUp(self) -> None:
        from essl_hub.config import DEVICES
        from essl_hub.db import Database

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Database(str(Path(tmp.name) / "essl.db"))
        self.database.initialize(DEVICES)
        self.hr = {"id": 0, "role": "hr"}
        self.admin = {"id": 0, "role": "admin"}

    def _apply(self, body: str, header: str | None = None, actor=None):
        return apply_roster(self.database, (header or self.HEADER) + body, actor=actor or self.hr)

    def _get(self, email: str) -> dict:
        return self.database.employee_by_email(email)

    def test_manager_listed_later_in_the_file_is_linked(self) -> None:
        outcomes = self._apply("X,x@n.com,1,y@n.com,\r\nY,y@n.com,2,,\r\n")
        self.assertEqual(self._get("x@n.com")["manager_id"], self._get("y@n.com")["id"])
        self.assertTrue(all(not o.warnings for o in outcomes))

    def test_unknown_manager_warns_and_still_adds_the_employee(self) -> None:
        [outcome] = self._apply("X,x@n.com,1,ghost@n.com,\r\n")
        self.assertEqual(outcome.result, "added")
        self.assertIn("manager not found", outcome.warnings[0])
        self.assertIsNone(self._get("x@n.com")["manager_id"])

    def test_self_reference_is_a_warning(self) -> None:
        [outcome] = self._apply("X,x@n.com,1,x@n.com,\r\n")
        self.assertIn("cannot manage self", outcome.warnings[0])

    def test_two_row_cycle_links_the_first_and_warns_on_the_later_row(self) -> None:
        outcomes = self._apply("X,x@n.com,1,y@n.com,\r\nY,y@n.com,2,x@n.com,\r\n")
        self.assertEqual(self._get("x@n.com")["manager_id"], self._get("y@n.com")["id"])
        self.assertIsNone(self._get("y@n.com")["manager_id"])
        self.assertEqual(outcomes[0].warnings, ())
        self.assertTrue(outcomes[1].warnings)

    def test_blank_manager_clears_and_absent_column_keeps(self) -> None:
        self._apply("X,x@n.com,1,y@n.com,\r\nY,y@n.com,2,,\r\n")
        self._apply("X,x@n.com,1\r\n", header="name,work email,device user id\r\n")
        self.assertIsNotNone(self._get("x@n.com")["manager_id"])
        self._apply("X,x@n.com,1,,\r\n")
        self.assertIsNone(self._get("x@n.com")["manager_id"])

    def test_role_column(self) -> None:
        [outcome] = self._apply("X,x@n.com,1,,manager\r\n")
        self.assertEqual(self._get("x@n.com")["role"], "manager")
        self.assertEqual(outcome.warnings, ())

        [outcome] = self._apply("X,x@n.com,1,,admin\r\n".replace("X,", "Xavier,"))
        self.assertTrue(outcome.warnings)
        self.assertEqual(self._get("x@n.com")["role"], "manager")
        self.assertEqual(self._get("x@n.com")["name"], "Xavier")

        [outcome] = self._apply("X,x@n.com,1,,\r\n")
        self.assertEqual(self._get("x@n.com")["role"], "manager")

    def test_role_on_an_existing_admin_is_ignored(self) -> None:
        self._apply("A,a@n.com,1,,\r\n")
        self.database.set_employee_role(self._get("a@n.com")["id"], "admin")
        [outcome] = self._apply("A,a@n.com,1,,employee\r\n")
        self.assertEqual(outcome.warnings, ())
        self.assertEqual(self._get("a@n.com")["role"], "admin")

    def test_hr_cannot_change_an_admins_device_id_but_admin_can(self) -> None:
        self._apply("A,a@n.com,1,,\r\n")
        self.database.set_employee_role(self._get("a@n.com")["id"], "admin")
        [outcome] = self._apply("A,a@n.com,99,,\r\n")
        self.assertEqual(outcome.result, "rejected")
        self.assertEqual(self._get("a@n.com")["device_user_id"], "1")
        [outcome] = self._apply("A,other@n.com,1,,\r\n")
        self.assertEqual(outcome.result, "rejected")
        [outcome] = self._apply("Anita,a@n.com,1,,\r\n")
        self.assertEqual(outcome.result, "updated")
        [outcome] = self._apply("A,a2@n.com,1,,\r\n", actor=self.admin)
        self.assertEqual(outcome.result, "updated")
        self.assertIsNotNone(self._get("a2@n.com"))

    def test_issues_csv_lists_only_non_clean_rows(self) -> None:
        from essl_hub.roster import issues_csv, summarize

        outcomes = self._apply("X,x@n.com,1,ghost@n.com,\r\nY,y@n.com,2,,\r\nZ,bad,3,,\r\n")
        self.assertEqual(summarize(outcomes), {"added": 2, "updated": 0, "warnings": 1, "rejected": 1})
        text = issues_csv(outcomes)
        self.assertIn("ghost@n.com", text)
        self.assertIn("Z,bad", text)
        self.assertNotIn("Y,y@n.com", text)
