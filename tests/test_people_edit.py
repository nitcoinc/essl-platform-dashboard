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


class ArchiveTest(unittest.TestCase):
    """Archive and restore people (KTD6) against the real Database."""

    setUp = PersonEditRealDatabaseTest.setUp
    _csrf = PersonEditRealDatabaseTest._csrf

    def _person(self, device_user_id: str, name: str, role: str = "employee") -> dict:
        email = f"{name.lower()}@example.com"
        self.database.upsert_employee(device_user_id=device_user_id, name=name, email=email)
        person = self.database.employee_by_email(email)
        if role != "employee":
            self.database.set_employee_role(person["id"], role)
        return self.database.employee_by_id(person["id"])

    def _punch(self, device_user_id: str, when) -> None:
        self.database.insert_punches([{
            "fingerprint": f"fp-{device_user_id}-{when.isoformat()}", "device_id": "uppal-x990", "site": "Uppal",
            "device_user_id": device_user_id, "employee_name": "Device Name", "event_timestamp": when,
        }])

    def _client_for(self, email: str):
        client = self.app.test_client()
        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda to, code: captured.update(code=code)
        try:
            client.post("/signin", data={"email": email})
            if "code" in captured:
                client.post("/signin/verify", data={"code": captured["code"]})
        finally:
            mail_module.send_login_code = original
        return client, captured

    def _archive(self, person: dict, client=None):
        client = client or self.client
        csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/people").text).group(1)
        return client.post(f"/people/{person['id']}/archive", data={"csrf": csrf})

    def test_archive_hides_person_everywhere_and_restore_brings_them_back(self) -> None:
        from datetime import datetime, timedelta

        asha = self._person("NP0118", "Asha")
        self.database.add_extra_device_id(asha["id"], "118")
        punched = (datetime.now() - timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
        self._punch("118", punched)
        before = self.database.employee_by_id(asha["id"])
        issues_before = self.client.get("/people/issues").text.count("count for nobody")

        response = self._archive(asha)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/people/archive"))
        archive_page = self.client.get("/people/archive").text
        self.assertIn("Archived Asha.", archive_page)
        self.assertIn("NP0118, 118", archive_page)
        for url in ("/team", "/people"):
            self.assertNotIn("asha@example.com", self.client.get(url).text, url)
        quick = self.client.get(f"/quick/{asha['id']}", headers={"HX-Request": "true"})
        self.assertEqual(quick.status_code, 404)
        self.assertEqual(self.client.get(f"/people/{asha['id']}/edit").status_code, 404)
        issues = self.client.get("/people/issues").text
        self.assertNotIn("Asha", issues)
        self.assertEqual(issues.count("count for nobody"), issues_before)

        # Past hours stay in exports for ranges where they punched, not others.
        day = punched.date().isoformat()
        export = self.client.get(f"/export.csv?from={day}&to={day}").text
        self.assertIn("NP0118,Asha", export)
        tomorrow = (punched.date() + timedelta(days=1)).isoformat()
        self.assertNotIn("NP0118,Asha", self.client.get(f"/export.csv?from={tomorrow}&to={tomorrow}").text)
        from essl_hub import hashing
        self.database.create_api_key(prefix="k1", key_hash=hashing.sha256_hex("secret"), label="t")
        hours = self.client.get(f"/api/v1/hours?from={day}&to={day}", headers={"Authorization": "Bearer secret"}).json
        self.assertIn("asha@example.com", {row["employee_email"] for row in hours["results"]})

        csrf = self._csrf("/people/archive")
        response = self.client.post(f"/people/{asha['id']}/restore", data={"csrf": csrf})
        self.assertEqual(response.status_code, 302)
        self.assertIn("Restored Asha.", self.client.get("/people/archive").text)
        self.assertIn("asha@example.com", self.client.get("/people").text)
        after = self.database.employee_by_id(asha["id"])
        for row in (before, after):
            row.pop("archived_at"), row.pop("updated_at")
        self.assertEqual(before, after)

    def test_archived_person_is_signed_out_and_cannot_sign_in(self) -> None:
        asha = self._person("7", "Asha")
        client, captured = self._client_for("asha@example.com")
        self.assertEqual(client.get("/day").status_code, 200)
        self._archive(asha)
        self.assertEqual(client.get("/day").status_code, 302)
        fresh, captured = self._client_for("asha@example.com")
        self.assertNotIn("code", captured)
        self.assertEqual(fresh.get("/day").status_code, 302)

    def test_archive_guards(self) -> None:
        self.assertEqual(self._archive(self.admin).status_code, 400)
        boss = self._person("8", "Boss")
        worker = self._person("9", "Worker")
        self.database.set_employee_manager(worker["id"], boss["id"])
        response = self._archive(boss)
        self.assertEqual(response.status_code, 400)
        self.assertIn("1 person reports to them", response.text)
        self.assertIsNotNone(self.database.employee_by_id(boss["id"]))
        other_admin = self._person("10", "Other", role="admin")
        self._person("11", "Hilda", role="hr")
        hr_client, _ = self._client_for("hilda@example.com")
        self.assertEqual(self._archive(other_admin, hr_client).status_code, 403)
        self.assertEqual(self._archive(worker, hr_client).status_code, 302)

    def test_new_person_clashing_with_archived_person_names_them(self) -> None:
        asha = self._person("12", "Asha")
        self._archive(asha)
        csrf = self._csrf("/people/new")
        response = self.client.post("/people/new", data={
            "csrf": csrf, "name": "New", "email": "asha@example.com", "device_user_id": "12",
            "manager_id": "", "role": "employee"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Asha (archived) already uses", response.text)

    def test_archive_tab_access_and_csrf(self) -> None:
        asha = self._person("13", "Asha")
        client, _ = self._client_for("asha@example.com")
        self.assertEqual(client.get("/people/archive").status_code, 403)
        self.assertEqual(self.client.post(f"/people/{asha['id']}/archive").status_code, 400)
        self.assertIsNotNone(self.database.employee_by_id(asha["id"]))

    def test_restore_with_archived_manager_notes_it(self) -> None:
        boss = self._person("14", "Boss")
        worker = self._person("15", "Worker")
        self.database.set_employee_manager(worker["id"], boss["id"])
        self._archive(worker)
        self._archive(boss)
        csrf = self._csrf("/people/archive")
        self.client.post(f"/people/{worker['id']}/restore", data={"csrf": csrf})
        page = self.client.get("/people/archive").text
        self.assertIn("Their manager, Boss, is archived", page)
        self.assertIsNotNone(self.database.employee_by_id(worker["id"]))

    def test_roster_import_rejects_archived_person(self) -> None:
        from essl_hub import roster

        asha = self._person("16", "Asha")
        self._archive(asha)
        outcomes = roster.apply_roster(
            self.database, "name,work email,device user id\r\nAsha B,asha@example.com,16\r\n", actor=self.admin
        )
        self.assertEqual(outcomes[0].result, "rejected")
        self.assertIn("archived person Asha", outcomes[0].reason)
        self.assertIsNone(self.database.employee_by_id(asha["id"]))
        self.assertEqual(self.database.employee_by_id(asha["id"], include_archived=True)["name"], "Asha")


if __name__ == "__main__":
    unittest.main()
