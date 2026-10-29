from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from essl_hub import db as db_module
from essl_hub import hashing
from essl_hub.routes import api as api_module
from essl_hub.config import DEVICES
from essl_hub.web import create_app

KEY = "test-api-key-123"


class CheckinsApiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"ESSL_DB_PATH": os.path.join(tmp.name, "e.db")})
        env.start()
        self.addCleanup(env.stop)
        self.database = db_module.Database(os.path.join(tmp.name, "e.db"))
        self.app = create_app(database=self.database)
        self.client = self.app.test_client()
        self.database.create_api_key(prefix="test", key_hash=hashing.sha256_hex(KEY), label="hr")
        device = DEVICES[0].id
        self.database.record_poll(device, status="connected", punches=[
            {"fingerprint": "a", "device_id": device, "site": "S", "device_user_id": "101",
             "event_timestamp": "2026-03-02 09:00:00", "trusted_direction": "entry"},
            {"fingerprint": "b", "device_id": device, "site": "S", "device_user_id": "101",
             "event_timestamp": "2026-03-02 18:00:00", "trusted_direction": "exit"},
            {"fingerprint": "c", "device_id": device, "site": "S", "device_user_id": "102",
             "event_timestamp": "2026-03-02 18:00:00", "trusted_direction": None},
            {"fingerprint": "d", "device_id": device, "site": "S", "device_user_id": "103",
             "event_timestamp": "2026-03-03 08:00:00", "trusted_direction": "entry"},
            {"fingerprint": "e", "device_id": device, "site": "S", "device_user_id": "",
             "event_timestamp": "2026-03-03 08:00:00", "trusted_direction": "entry"},
        ])
        self.device = device

    def get(self, query="", key=KEY):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return self.client.get(f"/api/v1/checkins{query}", headers=headers)

    def test_requires_key(self):
        self.assertEqual(self.get(key=None).status_code, 401)
        self.assertEqual(self.get(key="wrong").status_code, 401)

    def test_frappe_shape(self):
        data = self.get().get_json()
        self.assertEqual(data["count"], 4)  # blank device_user_id excluded
        first = data["results"][0]
        self.assertEqual(first, {
            "employee_field_value": "101", "employee_fieldname": "attendance_device_id",
            "timestamp": "2026-03-02 09:00:00", "time": "2026-03-02 09:00:00",
            "device_id": self.device, "log_type": "IN",
        })
        self.assertEqual(data["results"][1]["log_type"], "OUT")
        self.assertIsNone(data["results"][2]["log_type"])

    def test_since_exclusive(self):
        data = self.get("?since=2026-03-02T18:00:00").get_json()
        self.assertEqual([r["employee_field_value"] for r in data["results"]], ["103"])

    def test_pagination_does_not_split_a_second(self):
        data = self.get("?limit=3").get_json()
        # rows 2 and 3 share 18:00:00, so the page stops after 09:00:00.
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["next_since"], "2026-03-02 09:00:00")
        rest = self.get(f"?since={data['next_since']}&limit=3").get_json()
        self.assertEqual(rest["count"], 2)  # full page: trailing second deferred

    def test_bad_since(self):
        self.assertEqual(self.get("?since=garbage").status_code, 400)
        self.assertEqual(self.get("?since=2026-03-02T09:00:00%2B05:30").status_code, 400)

    # U9 / KTD11: key bookkeeping is best-effort and throttled.

    def test_pull_while_offline_returns_200_and_swallows_touch_failure(self):
        with patch.object(
            self.database, "touch_api_key", side_effect=db_module.OfflineError("down")
        ) as touch, self.assertLogs("essl_hub.routes.api", level="WARNING"):
            response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 4)
        touch.assert_called_once()

    def test_touch_api_key_at_most_once_an_hour(self):
        with patch.object(self.database, "touch_api_key", wraps=self.database.touch_api_key) as touch:
            self.get()
            self.get()
            self.get("?since=2026-03-02T09:00:00")
            self.assertEqual(touch.call_count, 1)
            later = db_module._now() + timedelta(hours=1, minutes=1)
            with patch.object(api_module, "_now_naive", return_value=later):
                self.get()
            self.assertEqual(touch.call_count, 2)


    # U2 / KTD4: approved fixes change hours, never the check-in feed.

    def test_fixes_change_hours_but_never_the_checkin_feed(self):
        from datetime import date, datetime

        before = self.get().get_json()
        for uid, name in (("101", "Asha"), ("900", "Hari")):
            self.database.upsert_employee(device_user_id=uid, name=name, email=f"{name.lower()}@x.com")
        asha = self.database.employee_by_device_user_id("101")["id"]
        hari = self.database.employee_by_device_user_id("900")["id"]
        self.database.set_employee_role(hari, "hr")
        request_id = self.database.file_fix_lines(asha, date(2026, 3, 2), [
            {"kind": "ignore", "target_fingerprint": "a"},
            {"kind": "flip", "target_fingerprint": "b", "direction": "entry"},
            {"kind": "add_out", "event_time": datetime(2026, 3, 2, 19, 0)},
        ], source="session")["request_id"]
        self.database.decide_fix_lines(request_id, [
            {"line_id": line["id"], "status": "approved"}
            for line in self.database.fix_request(request_id)["lines"]], actor_id=hari)
        self.assertEqual(self.get().get_json(), before)
        hours = self.client.get("/api/v1/hours?from=2026-03-02&to=2026-03-02",
                                headers={"Authorization": f"Bearer {KEY}"}).get_json()
        (row,) = [r for r in hours["results"] if r["device_user_id"] == "101"]
        # 09:00 ignored, 18:00 now an In, HR's 19:00 Out: one hour.
        self.assertEqual((row["effective_minutes"], row["status"]), (60, "short"))

    # Punch archive (v9, KTD5): archived punches leave the feed until restored.

    def test_archived_punches_leave_the_feed_until_restored(self):
        from datetime import datetime

        archive_id = self.database.archive_punches(
            "102", from_ts=None, to_ts=datetime(2026, 3, 3), reason="test card")
        users = [r["employee_field_value"] for r in self.get().get_json()["results"]]
        self.assertEqual(users, ["101", "101", "103"])
        self.database.restore_punch_archive(archive_id)
        self.assertEqual(self.get().get_json()["count"], 4)

if __name__ == "__main__":
    unittest.main()
