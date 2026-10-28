from __future__ import annotations

import os
import tempfile
import threading
import unittest

from essl_hub import access
from essl_hub.db import Database, RuleViolation


class AccessTest(unittest.TestCase):
    """U5 against a real temp-file SQLite database. Hierarchy A -> B -> C."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(os.path.join(self._tmp.name, "essl.db"))
        self.db.create_schema()
        self.a = self._person("1", "manager")
        self.b = self._person("2", "manager")
        self.c = self._person("3", "employee")
        self.db.set_employee_manager(self.b["id"], self.a["id"])
        self.db.set_employee_manager(self.c["id"], self.b["id"])
        self.hr = self._person("4", "hr")
        self.admin = self._person("5", "admin")
        self.loner = self._person("6", "manager")

    def _person(self, device_user_id: str, role: str) -> dict:
        email = f"p{device_user_id}@example.com"
        self.db.upsert_employee(device_user_id=device_user_id, name=f"P{device_user_id}", email=email)
        person = self.db.employee_by_email(email)
        if role != "employee":
            self.db.set_employee_role(person["id"], role)
        return self.db.employee_by_id(person["id"])

    def _ids(self, *people) -> set[int]:
        return {p["id"] for p in people}

    # visibility ---------------------------------------------------------

    def test_manager_sees_whole_subtree(self) -> None:
        self.assertEqual(access.visible_employee_ids(self.db, self.a), self._ids(self.a, self.b, self.c))
        self.assertEqual(access.visible_employee_ids(self.db, self.b), self._ids(self.b, self.c))

    def test_employee_sees_only_self(self) -> None:
        self.assertEqual(access.visible_employee_ids(self.db, self.c), self._ids(self.c))

    def test_employee_with_reports_but_no_manager_role_sees_only_self(self) -> None:
        self.db.set_employee_role(self.b["id"], "employee")
        b = self.db.employee_by_id(self.b["id"])
        self.assertEqual(access.visible_employee_ids(self.db, b), self._ids(b))

    def test_manager_without_reports_sees_self(self) -> None:
        self.assertEqual(access.visible_employee_ids(self.db, self.loner), self._ids(self.loner))

    def test_hr_and_admin_see_everyone(self) -> None:
        everyone = {e["id"] for e in self.db.employees()}
        self.assertEqual(access.visible_employee_ids(self.db, self.hr), everyone)
        self.assertEqual(access.visible_employee_ids(self.db, self.admin), everyone)

    def test_can_view(self) -> None:
        self.assertTrue(access.can_view(self.db, self.a, self.c["id"]))
        self.assertFalse(access.can_view(self.db, self.b, self.a["id"]))
        self.assertFalse(access.can_view(self.db, self.c, self.b["id"]))
        self.assertTrue(access.can_view(self.db, self.c, self.c["id"]))
        self.assertTrue(access.can_view(self.db, self.hr, self.a["id"]))
        self.assertFalse(access.can_view(self.db, self.hr, 99999))

    # manager assignment -------------------------------------------------

    def test_cycle_is_detected_and_rejected(self) -> None:
        self.assertTrue(access.would_create_cycle(self.db, self.a["id"], self.c["id"]))
        with self.assertRaisesRegex(RuleViolation, "cycle"):
            self.db.set_employee_manager(self.a["id"], self.c["id"])
        self.assertIsNone(self.db.employee_by_id(self.a["id"])["manager_id"])

    def test_self_manager_is_rejected(self) -> None:
        self.assertTrue(access.would_create_cycle(self.db, self.a["id"], self.a["id"]))
        with self.assertRaisesRegex(RuleViolation, "own manager"):
            self.db.set_employee_manager(self.a["id"], self.a["id"])

    def test_valid_reassignment_and_clearing(self) -> None:
        self.assertFalse(access.would_create_cycle(self.db, self.c["id"], self.a["id"]))
        self.db.set_employee_manager(self.c["id"], self.a["id"])
        self.assertEqual(self.db.employee_by_id(self.c["id"])["manager_id"], self.a["id"])
        self.db.set_employee_manager(self.c["id"], None)
        self.assertIsNone(self.db.employee_by_id(self.c["id"])["manager_id"])

    def test_unknown_manager_is_rejected(self) -> None:
        with self.assertRaises(RuleViolation):
            self.db.set_employee_manager(self.c["id"], 99999)

    # role rules ---------------------------------------------------------

    def test_hr_role_rules(self) -> None:
        self.assertTrue(access.can_assign_role(self.hr, self.c, "manager"))
        self.assertFalse(access.can_assign_role(self.hr, self.c, "admin"))
        self.assertFalse(access.can_assign_role(self.hr, self.admin, "hr"))
        self.assertFalse(access.can_assign_role(self.a, self.c, "manager"))
        self.assertFalse(access.can_assign_role(self.admin, self.c, "root"))
        self.assertTrue(access.can_assign_role(self.admin, self.c, "admin"))

    def test_last_admin_cannot_be_demoted(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "last admin"):
            self.db.set_employee_role(self.admin["id"], "hr")
        self.assertEqual(self.db.employee_by_id(self.admin["id"])["role"], "admin")

    def test_with_two_admins_one_can_be_demoted(self) -> None:
        self.db.set_employee_role(self.hr["id"], "admin")
        self.db.set_employee_role(self.admin["id"], "hr")
        self.assertEqual(self.db.employee_by_id(self.admin["id"])["role"], "hr")

    def test_concurrent_demotions_leave_an_admin(self) -> None:
        self.db.set_employee_role(self.hr["id"], "admin")
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def demote(employee_id: int) -> None:
            barrier.wait()
            try:
                self.db.set_employee_role(employee_id, "employee")
            except RuleViolation as exc:
                errors.append(exc)

        threads = [threading.Thread(target=demote, args=(i,)) for i in (self.hr["id"], self.admin["id"])]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        admins = [e for e in self.db.employees() if e["role"] == "admin"]
        self.assertEqual(len(admins), 1)
        self.assertEqual(len(errors), 1)

    # editing admins -----------------------------------------------------

    def test_hr_cannot_edit_admin_email_or_device_id(self) -> None:
        for field in ("email", "device_user_id", "role"):
            self.assertFalse(access.can_modify_employee(self.hr, self.admin, field))
            self.assertTrue(access.can_modify_employee(self.admin, self.admin, field))
            self.assertTrue(access.can_modify_employee(self.hr, self.c, field))
        self.assertTrue(access.can_modify_employee(self.hr, self.admin, "name"))
        self.assertFalse(access.can_modify_employee(self.a, self.c, "name"))
        self.assertFalse(access.can_modify_employee(self.c, self.c, "email"))

    # fix requests (KTD6) --------------------------------------------------

    def _request_by(self, person: dict) -> dict:
        return {"id": 1, "employee_id": person["id"], "status": "pending"}

    def test_only_hr_and_admin_open_fix_reviews(self) -> None:
        self.assertTrue(access.can_view_fix_reviews(self.hr))
        self.assertTrue(access.can_view_fix_reviews(self.admin))
        self.assertFalse(access.can_view_fix_reviews(self.a))
        self.assertFalse(access.can_view_fix_reviews(self.c))
        self.assertFalse(access.can_view_fix_reviews(None))

    def test_hr_approves_anyone_but_themselves(self) -> None:
        self.assertTrue(access.can_approve_fix(self.hr, self._request_by(self.c), self.db))
        self.assertTrue(access.can_approve_fix(self.hr, self._request_by(self.admin), self.db))
        self.assertFalse(access.can_approve_fix(self.hr, self._request_by(self.hr), self.db))

    def test_managers_and_employees_never_approve(self) -> None:
        self.assertFalse(access.can_approve_fix(self.a, self._request_by(self.c), self.db))
        self.assertFalse(access.can_approve_fix(self.c, self._request_by(self.b), self.db))

    def test_admin_approves_anyone_but_themselves_even_with_an_hr_user(self) -> None:
        self.assertTrue(access.can_approve_fix(self.admin, self._request_by(self.c), self.db))
        self.assertTrue(access.can_approve_fix(self.admin, self._request_by(self.hr), self.db))
        self.assertFalse(access.can_approve_fix(self.admin, self._request_by(self.admin), self.db))

    def test_the_admin_rule_does_not_depend_on_who_else_is_hr(self) -> None:
        self.db.archive_employee(self.hr["id"])
        self.assertTrue(access.can_approve_fix(self.admin, self._request_by(self.c), self.db))
        self.db.set_employee_role(self.hr["id"], "employee")
        self.assertTrue(access.can_approve_fix(self.admin, self._request_by(self.c), self.db))


    # direct punch fixes (v10) ---------------------------------------------

    def test_only_an_admin_fixes_punches_directly_and_never_their_own(self) -> None:
        self.assertTrue(access.can_fix_punches_directly(self.admin, self.c["id"]))
        self.assertTrue(access.can_fix_punches_directly(self.admin, self.hr["id"]))
        self.assertFalse(access.can_fix_punches_directly(self.admin, self.admin["id"]))
        for person in (self.hr, self.a, self.c):
            self.assertFalse(access.can_fix_punches_directly(person, self.c["id"] + 100))
        self.assertFalse(access.can_fix_punches_directly(None, self.c["id"]))


if __name__ == "__main__":
    unittest.main()
