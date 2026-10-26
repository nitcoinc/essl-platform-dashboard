"""U9: the Attendance alerts panel on the settings page (R15, KTD11)."""

from __future__ import annotations

import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.test_web import _DeviceSettingsBase, _ist_now, _ist_today

PUBLIC_URL = "https://attendance.example.com"


def _alerts_section(html: str) -> str:
    start = html.index('aria-labelledby="alerts-heading"')
    return html[start : html.index("</section>", start)]


class AlertSettingsWebTest(_DeviceSettingsBase):
    def _post(self, **fields):
        data = {"mode": "shadow", "start_hour": "9", "end_hour": "20", "morning_hour": "9"}
        data.update(fields)
        data["csrf"] = self._csrf()
        return self.client.post("/settings/alerts", data=data)

    def _add_hr(self) -> None:
        self._person("50", "Hema HR", "hema@example.com", "hr")

    def test_admin_sets_shadow_and_it_persists(self) -> None:
        self.database.set_setting("alerts_mode", "off")
        response = self._post(mode="shadow", start_hour="8", end_hour="19", morning_hour="10")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/settings?saved=alerts"))
        self.assertEqual(self.database.setting("alerts_mode"), "shadow")
        self.assertEqual(self.database.setting("alerts_start_hour"), "8")
        self.assertEqual(self.database.setting("alerts_end_hour"), "19")
        self.assertEqual(self.database.setting("alerts_morning_hour"), "10")
        section = _alerts_section(self.client.get("/settings?saved=alerts").text)
        self.assertIn('<option value="shadow" selected>', section)
        self.assertIn("Attendance alerts saved: Shadow", section)
        self.assertIn('value="10"', section)

    def test_alert_hours_must_start_before_they_end(self) -> None:
        response = self._post(start_hour="20", end_hour="9")
        self.assertEqual(response.status_code, 400)
        section = _alerts_section(response.text)
        self.assertIn("Alert hours must start before they end.", section)
        self.assertRegex(section, r'id="f-end_hour"[^>]*aria-invalid="true"')
        # The submitted values are kept so the admin can correct them.
        self.assertRegex(section, r'id="f-start_hour"[^>]*value="20"')
        self.assertIsNone(self.database.setting("alerts_start_hour"))

    def test_out_of_range_hour_is_refused(self) -> None:
        response = self._post(morning_hour="24")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Morning email hour must be a whole hour from 0 to 23.", response.text)
        self.assertRegex(response.text, r'id="f-morning_hour"[^>]*aria-invalid="true"')

    def test_live_refused_without_public_url(self) -> None:
        self._add_hr()
        with patch.dict(os.environ, {"ESSL_PUBLIC_URL": ""}):
            response = self._post(mode="live")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Set ESSL_PUBLIC_URL first so emails can link back to the dashboard", response.text)
        self.assertRegex(response.text, r'id="f-mode"[^>]*aria-invalid="true"')
        self.assertNotEqual(self.database.setting("alerts_mode"), "live")

    def test_live_refused_without_hr_user(self) -> None:
        with patch.dict(os.environ, {"ESSL_PUBLIC_URL": PUBLIC_URL}):
            response = self._post(mode="live")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Add at least one HR user first.", response.text)
        self.assertNotEqual(self.database.setting("alerts_mode"), "live")

    def test_live_accepted_with_public_url_and_hr_user(self) -> None:
        self._add_hr()
        with patch.dict(os.environ, {"ESSL_PUBLIC_URL": PUBLIC_URL}):
            response = self._post(mode="live")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.database.setting("alerts_mode"), "live")

    def test_non_admin_gets_403(self) -> None:
        csrf = self._csrf()
        # Two roles: sign-in codes are rate-limited per IP across clients.
        for role in ("hr", "employee"):
            with self.subTest(role=role):
                self.client = self.app.test_client()
                email = f"{role}-user@example.com"
                self._person(str(700 + len(role)), f"Ravi {role}", email, role)
                self._sign_in(email)
                response = self.client.post("/settings/alerts", data={"mode": "off", "csrf": csrf})
                self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.database.setting("alerts_mode"))

    def test_fresh_install_shows_empty_state(self) -> None:
        section = _alerts_section(self.client.get("/settings").text)
        # KTD11: a fresh install defaults to shadow.
        self.assertIn('<option value="shadow" selected>', section)
        self.assertIn("No alerts yet", section)
        self.assertIn("Baselines not built yet", section)
        self.assertNotIn("<table", section)
        self.assertNotIn("recompute", section.lower())

    def test_counts_and_baseline_coverage(self) -> None:
        self._person("2", "Asha", "asha@example.com", "employee")
        self._person("3", "Bala", "bala@example.com", "employee")
        computed = _ist_now() - timedelta(days=2)
        self.database.baselines = {
            1: {"employee_id": 1, "punched_days": 20, "clean_days": 15, "metrics": '{"break": {}}',
                "computed_at": computed},
            2: {"employee_id": 2, "punched_days": 12, "clean_days": 4, "metrics": None,
                "computed_at": computed},
        }
        today = _ist_today()
        rows = [
            ("Gachibowli", "live", "shadow", "recorded", today),
            ("Gachibowli", "live", "shadow", "recorded", today - timedelta(days=1)),
            ("Gachibowli", "live", "live", "sent", today),
            ("Gachibowli", "morning", "shadow", "breaker", today),
            ("Uppal", "morning", "shadow", "recorded", today),
            # Outside the 7-day window: not counted.
            ("Uppal", "live", "shadow", "recorded", today - timedelta(days=9)),
        ]
        self.database.alerts = [
            {"site": site, "kind": kind, "mode": mode, "status": status, "work_day": day}
            for site, kind, mode, status, day in rows
        ]
        section = _alerts_section(self.client.get("/settings").text)
        self.assertIn("1 of 3 people have enough history", section)
        self.assertIn(f"Baselines last rebuilt {computed.strftime('%d %b %Y')}", section)
        self.assertNotIn("No alerts yet", section)
        gachibowli = section[section.index(">Gachibowli<") : section.index("</tr>", section.index(">Gachibowli<"))]
        self.assertIn("2 would send, 1 sent", gachibowli)
        self.assertIn("1 held", gachibowli)
        uppal = section[section.index(">Uppal<") : section.index("</tr>", section.index(">Uppal<"))]
        self.assertIn("1 would send", uppal)
        self.assertIn("None", uppal)

    def test_week_footnote_no_longer_says_nobody_edits_punches(self) -> None:
        html = self.client.get("/week").text
        self.assertNotIn("Nobody can add or edit a punch by hand", html)
        self.assertIn("HR can approve a fix an employee reports", html)


if __name__ == "__main__":
    unittest.main()
