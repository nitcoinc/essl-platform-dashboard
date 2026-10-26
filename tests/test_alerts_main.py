from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from unittest import mock

from essl_hub import alert_settings, alerts_main
from essl_hub import db as db_module
from essl_hub.config import DeviceConfig
from essl_hub.db import OfflineError

X990 = "uppal-x990"
C3 = "gachibowli-c3-400"
SITES = {X990: "Uppal", C3: "Gachibowli"}
PUBLIC_URL = "https://attendance.example.com"
# Tuesday. History runs over the 14 days before it.
TODAY = date(2026, 10, 27)
CLEAN_DAY = (("09:00", "entry"), ("13:00", "exit"), ("13:40", "entry"), ("18:00", "exit"))


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm))


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class AlertsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.database = db_module.Database(os.path.join(self.dir, "essl.db"))
        self.database.initialize((
            DeviceConfig(id=X990, site="Uppal", model="X990", ip="192.168.1.201", port=4370,
                         protocol="zk-attendance", password_env="ESSL_X990_PASSWORD"),
            DeviceConfig(id=C3, site="Gachibowli", model="C3-400", ip="192.168.1.202", port=4370,
                         protocol="c3-pullsdk", password_env="ESSL_C3_PASSWORD"),
        ))
        self.addCleanup(self.database.close)
        self.now = at(TODAY, "14:45")
        # Every database timestamp (outbox due times, prunes) follows the fake clock.
        patcher = mock.patch.object(db_module, "_now", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"ESSL_PUBLIC_URL": "", "ESSL_HR_REPLY_TO": ""})
        env.start()
        self.addCleanup(env.stop)
        self.sent: list[dict] = []
        self.send_error: Exception | None = None
        self.status_path = os.path.join(self.dir, "collector-status.json")
        self.service = alerts_main.AlertsService(
            self.database, clock=lambda: self.now, send=self._send, status_path=self.status_path
        )
        self.logs = _ListHandler()
        logger = logging.getLogger("essl_hub.alerts_main")
        logger.addHandler(self.logs)
        self.addCleanup(logger.removeHandler, self.logs)
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.DEBUG)
        self._seq = 0

    # --- helpers -------------------------------------------------------------

    def _send(self, subject, recipients, text, html_body=None, *, reply_to=None):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"subject": subject, "to": list(recipients), "text": text,
                          "html": html_body, "reply_to": reply_to})
        return list(recipients)

    def employee(self, uid: str, name: str | None = None) -> int:
        self.database.upsert_employee(
            device_user_id=uid, name=name or f"Person {uid}", email=f"p{uid}@example.com"
        )
        return self.database.employee_by_device_user_id(uid)["id"]

    def punches(self, uid: str, day: date, swipes, *, device: str = X990, raw=None, byte=None) -> None:
        rows = []
        for hhmm, direction in swipes:
            self._seq += 1
            rows.append({
                "fingerprint": f"{device}-{uid}-{day}-{hhmm}-{self._seq}",
                "device_id": device,
                "site": SITES[device],
                "device_serial": "S1",
                "device_user_id": uid,
                "employee_name": uid,
                "event_timestamp": at(day, hhmm),
                "raw_state": "0",
                "raw_direction_byte": (0 if direction == "entry" else 1) if byte is None else byte,
                "trusted_direction": direction,
                "raw": raw or {"pin": uid},
            })
        self.database.insert_punches(rows)

    def history(self, uid: str, days: int = 14, swipes=CLEAN_DAY, *, end: date = TODAY) -> None:
        for back in range(1, days + 1):
            self.punches(uid, end - timedelta(days=back), swipes)

    def person(self, uid: str, today=(("09:00", "entry"), ("13:00", "exit")), **kw) -> int:
        """An employee with 14 clean days of history and today's swipes."""
        employee_id = self.employee(uid, **kw)
        self.history(uid)
        if today:
            self.punches(uid, TODAY, today)
        return employee_id

    def status(self, *, age_minutes: float = 1, spool_depth: int = 0) -> None:
        with open(self.status_path, "w", encoding="utf-8") as handle:
            json.dump({
                "last_poll": (self.now - timedelta(minutes=age_minutes)).isoformat(sep=" "),
                "spool_depth": spool_depth,
                "spool_oldest_at": None,
                "dead_count": 0,
                "offline": False,
            }, handle)

    def tick(self, when: datetime | None = None, *, fresh: bool = True) -> None:
        if when is not None:
            self.now = when
        if fresh:
            self.status()
        self.service.tick()

    def live_mode(self) -> None:
        self.database.set_setting(alert_settings.MODE_KEY, "live")
        os.environ["ESSL_PUBLIC_URL"] = PUBLIC_URL

    def alerts(self, day: date = TODAY, kind: str | None = None) -> list[dict]:
        return [a for a in self.database.alerts_for_day(day) if kind is None or a["kind"] == kind]

    def outbox(self) -> list[dict]:
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM mail_outbox ORDER BY id").fetchall()

    def snapshots(self) -> int:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(DISTINCT snapshot_id) AS n FROM attendance_baselines"
            ).fetchone()["n"]

    def messages(self, level: int = logging.WARNING, needle: str = "") -> list[str]:
        return [r.getMessage() for r in self.logs.records
                if r.levelno >= level and needle in r.getMessage()]

    # --- settings ------------------------------------------------------------

    def test_settings_defaults_and_garbage_fall_back(self) -> None:
        self.assertEqual(alert_settings.read(self.database), alert_settings.AlertSettings())
        self.assertEqual(alert_settings.read(self.database).mode, "shadow")
        self.database.set_setting("alerts_mode", "LOUD")
        self.database.set_setting("alerts_start_hour", "nine")
        self.database.set_setting("alerts_end_hour", "99")
        self.database.set_setting("alerts_morning_hour", "-1")
        self.assertEqual(alert_settings.read(self.database), alert_settings.AlertSettings())
        self.database.set_setting("alerts_start_hour", "21")
        self.database.set_setting("alerts_end_hour", "8")
        settings = alert_settings.read(self.database)
        self.assertEqual((settings.start_hour, settings.end_hour), (9, 20))

    def test_settings_parse_and_write(self) -> None:
        settings = alert_settings.parse("Live", "8", "19", "10")
        self.assertEqual(settings, alert_settings.AlertSettings("live", 8, 19, 10))
        alert_settings.write(self.database, settings)
        self.assertEqual(alert_settings.read(self.database), settings)
        for bad in (("loud", 9, 20, 9), ("off", 20, 9, 9), ("off", 9, 9, 9),
                    ("off", 9, 24, 9), ("off", "x", 20, 9), ("off", 9, 20, 30)):
            with self.subTest(bad=bad), self.assertRaises(alert_settings.SettingsError):
                alert_settings.parse(*bad)

    # --- shadow and live -----------------------------------------------------

    def test_shadow_records_and_sends_nothing(self) -> None:
        employee_id = self.person("101")
        self.tick(at(TODAY, "14:40"))
        self.assertEqual(self.alerts(), [])  # under limit + grace (90 + 15 min)
        self.tick(at(TODAY, "14:45"))
        [alert] = self.alerts()
        self.assertEqual((alert["employee_id"], alert["kind"], alert["mode"], alert["status"]),
                         (employee_id, "live", "shadow", "recorded"))
        self.assertEqual(alert["site"], "Uppal")
        self.assertEqual(alert["detail"]["out_minutes"], 105)
        self.assertEqual(alert["detail"]["limit"], 90.0)
        self.assertEqual(alert["detail"]["reason"], "out_too_long")
        self.assertEqual(alert["detail"]["out_since"], "2026-10-27 13:00:00")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.outbox(), [])

    def test_live_queues_and_sends_branded_email_with_report_link(self) -> None:
        employee_id = self.person("101", name="Ravi Kumar")
        self.live_mode()
        os.environ["ESSL_HR_REPLY_TO"] = "hr@example.com"
        self.tick()
        [alert] = self.alerts()
        self.assertEqual((alert["mode"], alert["status"]), ("live", "sent"))
        [mail] = self.sent
        self.assertEqual(mail["to"], ["p101@example.com"])
        self.assertEqual(mail["reply_to"], "hr@example.com")
        self.assertIn("Tue, 27 Oct", mail["subject"])
        self.assertIn("cid:nitco-logo", mail["html"])
        self.assertIn(f"{PUBLIC_URL}/privacy", mail["text"])
        token = re.search(re.escape(PUBLIC_URL) + r"/fix/([\w-]+)", mail["text"]).group(1)
        link = self.database.report_link(token)
        self.assertEqual((link["employee_id"], link["work_day"]), (employee_id, TODAY))
        self.assertEqual(link["expires_at"], datetime(2026, 11, 4, 6, 0))
        [row] = self.outbox()
        self.assertEqual(row["status"], "sent")
        self.assertIsNone(row["text_body"])

    def test_switching_to_live_same_day_does_not_resend(self) -> None:
        self.person("101")
        self.tick()
        self.assertEqual(self.alerts()[0]["status"], "recorded")
        self.live_mode()
        self.tick(self.now + timedelta(minutes=5))
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.outbox(), [])

    def test_no_realert_five_minutes_later(self) -> None:
        self.person("101")
        self.live_mode()
        self.tick()
        self.tick(self.now + timedelta(minutes=5))
        self.tick(self.now + timedelta(minutes=30))
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(len(self.sent), 1)

    def test_live_mode_without_public_url_behaves_as_shadow_and_warns_once(self) -> None:
        self.person("101")
        self.person("102", today=(("09:00", "entry"),))
        self.database.set_setting(alert_settings.MODE_KEY, "live")
        self.tick()
        [alert] = self.alerts()
        self.assertEqual((alert["mode"], alert["status"]), ("shadow", "recorded"))
        self.assertEqual(self.sent, [])
        # 102 goes out later; the second recording does not warn again.
        self.punches("102", TODAY, [("13:10", "exit")])
        self.tick(at(TODAY, "15:00"))
        self.assertEqual(len(self.alerts()), 2)
        self.assertEqual(len(self.messages(needle="ESSL_PUBLIC_URL")), 1)

    def test_live_check_runs_every_five_minutes_while_ticks_run_every_minute(self) -> None:
        self.person("101")
        with mock.patch.object(self.service, "run_live", wraps=self.service.run_live) as run_live:
            for minute in range(11):
                self.tick(at(TODAY, "14:45") + timedelta(minutes=minute))
        self.assertEqual(run_live.call_count, 3)  # 14:45, 14:50, 14:55

    def test_inside_person_outside_hours_and_absent_people_never_alert(self) -> None:
        self.person("101", today=CLEAN_DAY[:3])  # back inside at 13:40
        self.employee("102")
        self.history("102")  # absent today
        self.person("103")
        self.tick(at(TODAY, "20:00"))  # end of alert hours
        self.assertEqual(self.alerts(), [])
        self.tick(at(TODAY, "14:45") + timedelta(days=0, minutes=0))
        self.assertEqual([a["employee_id"] for a in self.alerts()],
                         [self.database.employee_by_device_user_id("103")["id"]])

    # --- suppression -----------------------------------------------------------

    def test_left_for_the_day_suppresses_live_and_morning(self) -> None:
        day = (("09:00", "entry"), ("11:00", "exit"), ("13:00", "exit"))  # orphan OUT at 13:00
        left = self.person("101", today=day)
        control = self.person("102", today=day)
        self.person("103", today=CLEAN_DAY[:3])
        self.person("104", today=CLEAN_DAY[:3])
        self.database.close_fix_day(left, TODAY, "closed_left", source="link")
        self.live_mode()
        self.tick()
        self.assertEqual([a["employee_id"] for a in self.alerts(kind="live")], [control])
        for uid in ("103", "104"):  # clean days by evening
            self.punches(uid, TODAY, [CLEAN_DAY[3]])
        self.tick(at(TODAY + timedelta(days=1), "09:05"))
        self.assertEqual([a["employee_id"] for a in self.alerts(kind="morning")], [control])
        self.assertEqual({tuple(m["to"]) for m in self.sent}, {("p102@example.com",)})
        self.assertEqual(len(self.sent), 2)

    # --- stale data ------------------------------------------------------------

    def test_stale_status_skips_detection_but_drains_outbox(self) -> None:
        self.person("101")
        self.database.enqueue_mail(["hr@example.com"], "Fix request from Ravi", "text", "<p>html</p>")
        self.status(age_minutes=15)
        self.tick(fresh=False)
        self.assertEqual(self.alerts(), [])
        self.assertEqual([m["subject"] for m in self.sent], ["Fix request from Ravi"])
        self.assertIn("collector last polled", " ".join(self.messages(logging.INFO, "skipped")))

    def test_missing_status_file_skips_detection(self) -> None:
        self.person("101")
        self.tick(fresh=False)
        self.assertEqual(self.alerts(), [])

    def test_idle_collector_with_old_device_success_is_not_stale(self) -> None:
        self.person("101")
        with self.database.transaction() as connection:
            connection.execute("UPDATE devices SET last_success = ?",
                               (self.now - timedelta(minutes=12),))
        self.status(age_minutes=1)
        self.tick(fresh=False)
        self.assertEqual(len(self.alerts()), 1)

    def test_spooled_punches_skip_detection(self) -> None:
        self.person("101")
        self.status(age_minutes=0, spool_depth=2)
        self.tick(fresh=False)
        self.assertEqual(self.alerts(), [])

    # --- circuit breaker -------------------------------------------------------

    def test_breaker_records_sends_nothing_and_warns_once(self) -> None:
        out = [self.person(f"2{n:02d}") for n in range(6)]
        for n in range(6, 20):
            self.person(f"2{n:02d}", today=CLEAN_DAY[:3])
        self.live_mode()
        self.tick()
        alerts = self.alerts()
        self.assertEqual(sorted(a["employee_id"] for a in alerts), sorted(out))
        self.assertEqual({a["status"] for a in alerts}, {"breaker"})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.outbox(), [])
        self.assertEqual(len(self.messages(needle="breaker")), 1)
        self.tick(self.now + timedelta(minutes=5))
        self.tick(self.now + timedelta(minutes=5))
        self.assertEqual(len(self.alerts()), 6)
        self.assertEqual(len(self.messages(needle="breaker")), 1)
        self.assertEqual(self.sent, [])

    def test_breaker_rule(self) -> None:
        self.assertFalse(alerts_main.breaker_tripped(1, 1))
        self.assertFalse(alerts_main.breaker_tripped(4, 20))
        self.assertTrue(alerts_main.breaker_tripped(5, 20))  # 25% of those in
        self.assertTrue(alerts_main.breaker_tripped(6, 100))
        self.assertFalse(alerts_main.breaker_tripped(5, 100))

    # --- morning job -----------------------------------------------------------

    def test_morning_holds_unconfirmed_c3_guess_then_sends_after_confirmation(self) -> None:
        employee_id = self.employee("301")
        self.history("301")
        self.punches("301", TODAY, [("09:00", "entry")], device=C3)
        guess = {"pin": "301", "source": "realtime_fallback"}
        self.punches("301", TODAY, [("13:00", "exit")], device=C3, raw=guess, byte=15)
        self.punches("301", TODAY, [("13:40", "entry")], device=C3)  # no final OUT
        self.live_mode()
        morning = at(TODAY + timedelta(days=1), "09:05")
        self.tick(morning)
        self.assertEqual(self.alerts(kind="morning"), [])
        self.tick(morning + timedelta(minutes=1))
        self.assertEqual(self.alerts(kind="morning"), [])
        [guessed] = [r for r in self.database.raw_punches_for_device_users_range(
            ["301"], at(TODAY, "06:00"), at(TODAY, "23:00")) if r["raw_direction_byte"] == 15]
        self.database.fill_punch_directions([{
            "fingerprint": guessed["fingerprint"], "trusted_direction": "exit", "raw_direction_byte": 1,
        }])
        self.tick(morning + timedelta(minutes=2))
        [alert] = self.alerts(kind="morning")
        self.assertEqual((alert["employee_id"], alert["status"], alert["site"]),
                         (employee_id, "sent", "Gachibowli"))
        self.assertEqual([i["kind"] for i in alert["detail"]["issues"]], ["missing_final_out"])
        [mail] = self.sent
        self.assertIn("Yesterday's attendance", mail["subject"])

    def test_morning_waits_for_morning_hour(self) -> None:
        self.employee("301")
        self.history("301")
        self.punches("301", TODAY, CLEAN_DAY[:3])
        self.tick(at(TODAY + timedelta(days=1), "08:59"))
        self.assertEqual(self.alerts(kind="morning"), [])
        self.tick(at(TODAY + timedelta(days=1), "09:00"))
        self.assertEqual(len(self.alerts(kind="morning")), 1)

    def test_punched_but_few_clean_days_gets_morning_but_no_live(self) -> None:
        employee_id = self.employee("401")
        # 12 punched days: the 8 oldest end on a swipe-in, the 4 newest are clean.
        for back in range(1, 13):
            swipes = CLEAN_DAY if back <= 4 else CLEAN_DAY[:3]
            self.punches("401", TODAY - timedelta(days=back), swipes)
        self.punches("401", TODAY, [("09:00", "entry"), ("13:00", "exit")])
        self.live_mode()
        self.tick()
        stored = self.database.latest_baselines()[employee_id]
        self.assertEqual((stored["punched_days"], stored["clean_days"]), (12, 4))
        self.assertEqual(self.alerts(), [])
        self.punches("401", TODAY, [("15:00", "entry")])  # back in, never swipes out
        self.tick(at(TODAY + timedelta(days=1), "09:05"))
        [alert] = self.alerts(kind="morning")
        self.assertEqual(alert["employee_id"], employee_id)
        self.assertEqual(len(self.sent), 1)

    def test_morning_needs_ten_punched_days(self) -> None:
        employee_id = self.employee("402")
        self.history("402", days=9)
        self.punches("402", TODAY, CLEAN_DAY[:3])
        self.tick()  # weekly job: 9 punched days before today
        self.assertEqual(self.database.latest_baselines()[employee_id]["punched_days"], 9)
        self.tick(at(TODAY + timedelta(days=1), "09:05"))
        self.assertEqual(self.alerts(kind="morning"), [])

    # --- sending and retries ---------------------------------------------------

    def test_send_failure_retries_then_fails_without_recreating_the_alert(self) -> None:
        self.person("101")
        self.live_mode()
        self.send_error = RuntimeError("Resend 500")
        self.tick()
        [row] = self.outbox()
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        self.tick(self.now + timedelta(minutes=1))  # not due yet (5 min backoff)
        self.assertEqual(self.outbox()[0]["attempts"], 1)
        self.tick(self.now + timedelta(minutes=5))
        self.assertEqual(self.outbox()[0]["attempts"], 2)
        self.tick(self.now + timedelta(minutes=10))
        [row] = self.outbox()
        self.assertEqual((row["status"], row["attempts"]), ("failed", 3))
        self.assertIsNone(row["text_body"])
        self.assertIn("Resend 500", row["last_error"])
        self.send_error = None
        self.tick(self.now + timedelta(minutes=30))
        [alert] = self.alerts()
        self.assertEqual(alert["status"], "failed")
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.sent, [])

    def test_one_failed_send_does_not_stop_the_batch(self) -> None:
        self.database.enqueue_mail(["a@example.com"], "first", "text")
        self.database.enqueue_mail(["b@example.com"], "second", "text")
        calls = []

        def flaky(subject, recipients, text, html_body=None, *, reply_to=None):
            calls.append(subject)
            if subject == "first":
                raise RuntimeError("boom")

        self.service._send = flaky
        self.assertEqual(self.service.drain_outbox(), (1, 1))
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual([r["status"] for r in self.outbox()], ["pending", "sent"])

    def test_mode_off_still_drains_outbox(self) -> None:
        self.person("101")
        self.database.set_setting(alert_settings.MODE_KEY, "off")
        self.database.enqueue_mail(["p101@example.com"], "Your attendance fix was approved", "ok")
        self.tick()
        self.assertEqual(self.alerts(), [])
        self.assertEqual([m["subject"] for m in self.sent], ["Your attendance fix was approved"])

    # --- weekly job ------------------------------------------------------------

    def test_weekly_job_runs_once_per_week_and_prunes(self) -> None:
        employee_id = self.person("101", today=())
        start = datetime(2026, 10, 21, 10, 0)  # Wednesday; first start runs at once
        self.tick(start)
        self.assertEqual(self.snapshots(), 1)
        self.assertEqual(self.database.latest_baselines()[employee_id]["clean_days"], 8)
        # Hourly ticks through the first Sunday (only 4 days old there) and the week.
        for hour in range(1, 24 * 10):
            self.now = start + timedelta(hours=hour)
            self.service.tick()
        self.assertEqual(self.snapshots(), 1)
        # Old rows for the prune, then the next Sunday 02:00.
        old_day = date(2026, 7, 1)
        self.database.record_alert(employee_id=employee_id, work_day=old_day, kind="live",
                                   mode="shadow", status="recorded")
        with self.database.transaction() as connection:
            connection.execute("UPDATE mail_outbox SET created_at = ?", (start - timedelta(days=60),))
        self.database.enqueue_mail(["x@example.com"], "old", "text")
        with self.database.transaction() as connection:
            connection.execute("UPDATE mail_outbox SET created_at = ?", (start - timedelta(days=60),))
        self.database.create_report_link(employee_id, old_day, expires_at=start)
        for _ in range(5):
            self.database.record_baselines([{"employee_id": employee_id, "punched_days": 1,
                                             "clean_days": 0, "metrics": None}])
        with self.database.transaction() as connection:
            connection.execute("UPDATE attendance_baselines SET computed_at = ?", (start,))
        sunday = datetime(2026, 11, 1, 2, 0)
        self.tick(sunday - timedelta(minutes=1))
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT MAX(snapshot_id) AS n FROM attendance_baselines").fetchone()["n"], 6)
        self.tick(sunday)
        self.tick(sunday + timedelta(hours=1))
        with self.database.connect() as connection:
            snapshot_ids = [r["snapshot_id"] for r in connection.execute(
                "SELECT DISTINCT snapshot_id FROM attendance_baselines ORDER BY snapshot_id")]
            links = connection.execute("SELECT COUNT(*) AS n FROM fix_report_links").fetchone()["n"]
        self.assertEqual(snapshot_ids, [4, 5, 6, 7])
        self.assertEqual(self.alerts(old_day), [])
        self.assertEqual(self.outbox(), [])
        self.assertEqual(links, 0)
        self.assertEqual(self.database.setting(alerts_main.BASELINES_AT_KEY), "2026-11-01 02:00:00")

    def test_weekly_job_with_nobody_does_not_rerun_every_tick(self) -> None:
        with mock.patch.object(self.service, "run_weekly", wraps=self.service.run_weekly) as run:
            for minute in range(5):
                self.tick(at(TODAY, "10:00") + timedelta(minutes=minute))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.snapshots(), 0)

    def test_weekly_boundary(self) -> None:
        self.assertEqual(alerts_main.last_weekly_boundary(datetime(2026, 11, 1, 2, 0)),
                         datetime(2026, 11, 1, 2, 0))
        self.assertEqual(alerts_main.last_weekly_boundary(datetime(2026, 11, 1, 1, 59)),
                         datetime(2026, 10, 25, 2, 0))
        self.assertEqual(alerts_main.last_weekly_boundary(datetime(2026, 10, 28, 12, 0)),
                         datetime(2026, 10, 25, 2, 0))

    # --- employee edge cases -----------------------------------------------------

    def test_archived_employee_is_never_evaluated(self) -> None:
        employee_id = self.person("101")
        self.database.archive_employee(employee_id)
        self.tick()
        self.assertEqual(self.alerts(), [])
        self.assertNotIn(employee_id, self.database.latest_baselines())
        self.punches("101", TODAY, [("15:00", "entry")])
        self.tick(at(TODAY + timedelta(days=1), "09:05"))
        self.assertEqual(self.alerts(), [])

    def test_main_and_extra_device_ids_are_evaluated_together(self) -> None:
        employee_id = self.employee("101")
        self.database.add_extra_device_id(employee_id, "9101")
        self.history("101")
        self.punches("101", TODAY, [("09:00", "entry")])
        self.punches("9101", TODAY, [("13:00", "exit")], device=C3)
        self.tick()
        [alert] = self.alerts()
        self.assertEqual((alert["employee_id"], alert["site"]), (employee_id, "Gachibowli"))
        self.assertEqual(alert["detail"]["out_since"], "2026-10-27 13:00:00")

    def test_door_denied_exit_never_counts_as_out(self) -> None:
        self.employee("101")
        self.history("101")
        self.punches("101", TODAY, [("09:00", "entry")])
        self._seq += 1
        self.database.insert_punches([{
            "fingerprint": "denied-1", "device_id": C3, "site": "Gachibowli",
            "device_user_id": "101", "event_timestamp": at(TODAY, "13:00"),
            "raw_state": "door denied: anti-passback", "raw_direction_byte": 1,
            "trusted_direction": "exit", "raw": {"pin": "101"},
        }])
        self.tick()
        self.assertEqual(self.alerts(), [])

    # --- resilience --------------------------------------------------------------

    def test_offline_error_during_tick_logs_and_continues(self) -> None:
        self.person("101")
        self.tick(at(TODAY, "10:00"))  # weekly job done before the outage
        self.database.enqueue_mail(["hr@example.com"], "queued", "text")
        with mock.patch.object(self.database, "employees", side_effect=OfflineError("database is locked")):
            self.tick(at(TODAY, "14:45"))
        self.assertEqual(self.alerts(), [])
        self.assertEqual([m["subject"] for m in self.sent], ["queued"])
        self.assertTrue(self.messages(needle="database unavailable"))
        self.tick(at(TODAY, "14:50"))
        self.assertEqual(len(self.alerts()), 1)

    def test_run_loop_survives_a_crashing_tick(self) -> None:
        service = mock.Mock()
        service.tick.side_effect = [RuntimeError("boom"), None, None]
        sleeps: list[float] = []
        alerts_main.run_loop(service, sleep=sleeps.append, ticks=3)
        self.assertEqual(service.tick.call_count, 3)
        self.assertEqual(len(sleeps), 3)

    def test_waits_for_schema_v8(self) -> None:
        self.person("101")
        self.database.enqueue_mail(["hr@example.com"], "queued", "text")
        with self.database.transaction() as connection:
            connection.execute("PRAGMA user_version=7")
            connection.execute("UPDATE settings SET value = '7' WHERE key = 'schema_version'")
        self.tick()
        self.tick(self.now + timedelta(minutes=1))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.snapshots(), 0)
        self.assertEqual(len(self.messages(needle="waiting for v8")), 1)
        with self.database.transaction() as connection:
            connection.execute("PRAGMA user_version=8")
            connection.execute("UPDATE settings SET value = '8' WHERE key = 'schema_version'")
        self.tick(self.now + timedelta(minutes=5))
        self.assertEqual([m["subject"] for m in self.sent], ["queued"])
        self.assertEqual(self.snapshots(), 1)
        self.assertEqual(len(self.alerts()), 1)

    def test_missing_database_waits(self) -> None:
        service = alerts_main.AlertsService(
            db_module.Database(os.path.join(self.dir, "absent.db"), must_exist=True),
            clock=lambda: self.now, send=self._send, status_path=self.status_path,
        )
        service.tick()
        self.assertFalse(os.path.exists(os.path.join(self.dir, "absent.db")))
        self.assertTrue(self.messages(needle="not set up"))


if __name__ == "__main__":
    unittest.main()
