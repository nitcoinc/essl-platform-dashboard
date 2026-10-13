"""People edit form against the real Database (the web suite uses a fake)."""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from essl_hub import mail as mail_module
from essl_hub.db import Database
from essl_hub.web import create_app


class PersonEditRealDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Database(os.path.join(tmp.name, "hub.db"), sync_url="")
        self.addCleanup(self.database.close)
        self.database.migrate_schema()
        self.database.upsert_employee(device_user_id="admin@example.com", name="Admin", email="admin@example.com")
        self.admin = self.database.employee_by_email("admin@example.com")
        self.database.set_employee_role(self.admin["id"], "admin")
        self.app = create_app(database=self.database)
        self.app.testing = True
        self.client = self.app.test_client()
        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda email, code: captured.update(code=code)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})

    def test_admin_changes_own_name_and_alphanumeric_device_id(self) -> None:
        page = self.client.get(f"/people/{self.admin['id']}/edit").text
        self.assertNotIn('inputmode="numeric"', page)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        response = self.client.post(
            f"/people/{self.admin['id']}/edit",
            data={"csrf": csrf, "name": "A Admin", "email": "admin@example.com",
                  "device_user_id": "NP0009", "manager_id": "", "role": "admin"},
        )
        self.assertEqual(response.status_code, 302)
        updated = self.database.employee_by_id(self.admin["id"])
        self.assertEqual((updated["name"], updated["device_user_id"]), ("A Admin", "NP0009"))

    def _csrf(self, url: str) -> str:
        return re.search(r'name="csrf" value="([^"]+)"', self.client.get(url).text).group(1)

    def test_new_person_device_id_is_trimmed_and_upper_cased(self) -> None:
        csrf = self._csrf("/people/new")
        response = self.client.post("/people/new", data={
            "csrf": csrf, "name": "Asha", "email": "asha@example.com",
            "device_user_id": " np0105 ", "manager_id": "", "role": "employee"})
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(self.database.employee_by_device_user_id("NP0105"))

    def test_zero_device_ids_are_rejected_on_the_form(self) -> None:
        for value in ("0", "00", "NP0000"):
            with self.subTest(value=value):
                csrf = self._csrf("/people/new")
                response = self.client.post("/people/new", data={
                    "csrf": csrf, "name": "Z", "email": "z@example.com",
                    "device_user_id": value, "manager_id": "", "role": "employee"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("Device ID can&#39;t be zero", response.text)
                self.assertIsNone(self.database.employee_by_email("z@example.com"))

    def test_untouched_legacy_device_id_is_not_rewritten(self) -> None:
        self.database.upsert_employee(device_user_id="np0042", name="Old", email="old@example.com")
        old = self.database.employee_by_email("old@example.com")
        csrf = self._csrf(f"/people/{old['id']}/edit")
        response = self.client.post(f"/people/{old['id']}/edit", data={
            "csrf": csrf, "name": "Old Two", "email": "old@example.com",
            "device_user_id": "np0042", "manager_id": "", "role": "employee"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.database.employee_by_id(old["id"])["device_user_id"], "np0042")

    def test_changed_device_id_keeps_leading_zeros(self) -> None:
        self.database.upsert_employee(device_user_id="5", name="P", email="p@example.com")
        person = self.database.employee_by_email("p@example.com")
        csrf = self._csrf(f"/people/{person['id']}/edit")
        self.client.post(f"/people/{person['id']}/edit", data={
            "csrf": csrf, "name": "P", "email": "p@example.com",
            "device_user_id": "0118", "manager_id": "", "role": "employee"})
        self.assertEqual(self.database.employee_by_id(person["id"])["device_user_id"], "0118")

    def test_id_issues_page_renders_against_the_real_database(self) -> None:
        from datetime import datetime

        self.database.insert_punches([{
            "fingerprint": "fp-real", "device_id": "uppal-x990", "site": "Uppal", "device_user_id": "NP0231",
            "employee_name": "New Joiner", "event_timestamp": datetime(2026, 10, 9, 9, 0),
        }])
        page = self.client.get("/people/issues")
        self.assertEqual(page.status_code, 200)
        self.assertIn("09 Oct 2026", page.text)

    def test_people_row_has_edit_button(self) -> None:
        page = self.client.get("/people").text
        self.assertIn(f'href="/people/{self.admin["id"]}/edit"', page)
        self.assertRegex(page, r'class="button button-tertiary button-sm"[^>]*>Edit</a>')


if __name__ == "__main__":
    unittest.main()
