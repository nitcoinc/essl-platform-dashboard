from __future__ import annotations

import io

import openpyxl
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from zoneinfo import ZoneInfo

# The application computes days and expiries in Asia/Kolkata (`essl_hub.auth._now`),
# so a test must never use the system clock: on a UTC CI runner the local date can
# be a day behind the app's, which silently moves seeded punches to the wrong day
# and expires session/challenge rows the app still considers live.
_IST = ZoneInfo("Asia/Kolkata")


def _ist_now() -> datetime:
    return datetime.now(_IST).replace(tzinfo=None)


def _ist_today():
    # The app's "today" is the work day containing now (default start 06:00).
    return (_ist_now() - timedelta(hours=6)).date()


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".packages"))
sys.path.insert(0, str(ROOT))

from essl_hub import roster
from essl_hub.config import DEVICES
from essl_hub.db import OfflineError
from essl_hub.web import create_app


class FakeDatabase:
    """In-memory stand-in for essl_hub.db.Database, implementing the same
    interface, so the whole web layer can be exercised without Postgres."""

    def __init__(self) -> None:
        self.devices_by_id: dict[str, dict] = {}
        self.punches: list[dict] = []
        self.employees_by_id: dict[int, dict] = {}
        self._next_employee_id = 1
        self.settings: dict[str, str] = {}
        self.login_codes: list[dict] = []
        self._next_login_code_id = 1
        self.sessions: dict[str, dict] = {}
        self.api_keys_list: list[dict] = []
        # Local mode by default: no sync loop. `offline` makes every write
        # raise OfflineError the way a degraded replica does (KTD11).
        self.replica = False
        self.offline = False
        self.sync_calls = 0
        self.sync_loop_interval = None

    # replica / sync --------------------------------------------------------

    def sync(self) -> bool:
        self.sync_calls += 1
        return not self.offline

    def start_sync_loop(self, interval):
        self.sync_loop_interval = interval

    def _check_online(self) -> None:
        if self.offline:
            raise OfflineError("Turso is unreachable")

    # devices ------------------------------------------------------------

    def initialize(self, devices) -> None:
        self._check_online()
        for device in devices:
            self.devices_by_id[device.id] = {
                "id": device.id,
                "site": device.site,
                "model": device.model,
                "ip": device.ip,
                "port": device.port,
                "protocol": device.protocol,
                "status": "never_synced",
                "transport": None,
                "serial_number": None,
                "firmware": None,
                "last_attempt": None,
                "last_success": None,
                "last_error": None,
                "records_seen": 0,
                "comm_password": None,
                "connection_updated_at": None,
                "created_at": None,
                "retired_at": None,
            }
        self.settings.setdefault("daily_target_minutes", "480")

    def devices(self) -> list[dict]:
        result = []
        for device in self.devices_by_id.values():
            stored = sum(1 for p in self.punches if p["device_id"] == device["id"])
            result.append({**device, "stored_punches": stored})
        return result

    def device_by_id(self, device_id: str):
        return self.devices_by_id.get(device_id)

    def device_id_summary(self):
        rows: dict[str, dict] = {}
        for p in self.punches:
            uid = p.get("device_user_id")
            if not uid:
                continue
            row = rows.setdefault(uid, {"device_user_id": uid, "punch_count": 0, "first_seen": None, "last_seen": None, "device_name": "", "sites": set()})
            row["punch_count"] += 1
            row["first_seen"] = min(filter(None, [row["first_seen"], p["event_timestamp"]]))
            # Device name is the latest punch's, like the SQL.
            if row["last_seen"] is None or p["event_timestamp"] >= row["last_seen"]:
                row["device_name"] = p.get("employee_name") or ""
            row["last_seen"] = max(filter(None, [row["last_seen"], p["event_timestamp"]]))
            if p.get("site"):
                row["sites"].add(p["site"])
        return [{**r, "sites": ",".join(sorted(r["sites"]))} for r in rows.values()]

    def min_device_last_success(self):
        values = [d.get("last_success") for d in self.devices_by_id.values()]
        if not values or any(v is None for v in values):
            return None
        return min(values)

    def update_device(self, device_id: str, *, ip: str, port: int, comm_password):
        device = self.devices_by_id[device_id]
        device["ip"] = ip
        device["port"] = port
        device["comm_password"] = comm_password
        device["connection_updated_at"] = _ist_now()

    def add_device(self, *, device_id, site, model, ip, port, protocol, comm_password) -> None:
        self._check_online()
        if device_id in self.devices_by_id:
            raise ValueError("duplicate device id")
        now = _ist_now()
        self.devices_by_id[device_id] = {
            "id": device_id, "site": site, "model": model, "ip": ip, "port": port,
            "protocol": protocol, "status": "never_synced", "transport": None,
            "serial_number": None, "firmware": None, "last_attempt": None,
            "last_success": None, "last_error": None, "records_seen": 0,
            "comm_password": comm_password, "connection_updated_at": now,
            "created_at": now, "retired_at": None,
        }

    def set_device_retired(self, device_id: str, retired: bool) -> None:
        self._check_online()
        self.devices_by_id[device_id]["retired_at"] = _ist_now() if retired else None

    def devices_at_address(self, ip: str, port: int) -> list[dict]:
        return [d for d in self.devices_by_id.values() if d["ip"] == ip and d["port"] == port]

    # punches --------------------------------------------------------------

    def insert_punches(self, punches) -> int:
        inserted = 0
        for punch in punches:
            if any(p["fingerprint"] == punch["fingerprint"] for p in self.punches):
                continue
            self.punches.append(dict(punch))
            inserted += 1
        return inserted

    def punches_for_employee_range(self, device_user_id: str, start: datetime, end: datetime):
        rows = [
            p
            for p in self.punches
            if p.get("device_user_id") == device_user_id and start <= p["event_timestamp"] < end
        ]
        rows.sort(key=lambda p: p["event_timestamp"])
        return [
            {
                "event_timestamp": p["event_timestamp"],
                "trusted_direction": p.get("trusted_direction"),
                "raw_direction_byte": p.get("raw_direction_byte"),
                "raw_state": p.get("raw_state", ""),
            }
            for p in rows
        ]

    def punches_for_device_users_range(self, device_user_ids, start: datetime, end: datetime):
        wanted = set(device_user_ids)
        rows = [
            p for p in self.punches
            if p.get("device_user_id") in wanted and start <= p["event_timestamp"] < end
        ]
        rows.sort(key=lambda p: (p["device_user_id"], p["event_timestamp"]))
        return [
            {
                "device_user_id": p["device_user_id"],
                "event_timestamp": p["event_timestamp"],
                "trusted_direction": p.get("trusted_direction"),
                "site": p.get("site"),
            }
            for p in rows
        ]

    def unmapped_device_user_ids(self):
        mapped = {e["device_user_id"] for e in self.employees_by_id.values()}
        counts: dict[str, int] = {}
        for p in self.punches:
            uid = p.get("device_user_id")
            if uid and uid not in mapped:
                counts[uid] = counts.get(uid, 0) + 1
        return [{"device_user_id": uid, "punch_count": n} for uid, n in sorted(counts.items())]

    # employees ------------------------------------------------------------

    def upsert_employee(self, *, device_user_id: str, name: str, email: str) -> str:
        existing = next(
            (e for e in self.employees_by_id.values() if e["device_user_id"] == device_user_id),
            None,
        )
        if existing:
            existing["name"] = name
            existing["email"] = email
            return "updated"
        employee = {
            "id": self._next_employee_id,
            "device_user_id": device_user_id,
            "name": name,
            "email": email,
            "role": "employee",
        }
        self.employees_by_id[employee["id"]] = employee
        self._next_employee_id += 1
        return "added"

    def update_employee(self, employee_id: int, *, name: str, email: str, device_user_id: str) -> None:
        self.employees_by_id[employee_id].update(name=name, email=email, device_user_id=device_user_id)

    def employees(self):
        return sorted(
            self.employees_by_id.values(),
            key=lambda e: e["name"],
        )

    def employee_by_email(self, email: str):
        return next(
            (
                e
                for e in self.employees_by_id.values()
                if e["email"].lower() == email.strip().lower()
            ),
            None,
        )

    def employee_by_id(self, employee_id: int):
        return self.employees_by_id.get(employee_id)

    def set_employee_role(self, employee_id: int, role: str) -> None:
        self._check_online()
        from essl_hub.db import RuleViolation

        current = self.employees_by_id[employee_id]
        admins = sum(e["role"] == "admin" for e in self.employees_by_id.values())
        if current["role"] == "admin" and role != "admin" and admins <= 1:
            raise RuleViolation("The last admin cannot be demoted.")
        current["role"] = role

    def set_employee_manager(self, employee_id: int, manager_id) -> None:
        self.employees_by_id[employee_id]["manager_id"] = manager_id

    def subtree_ids(self, root_id: int) -> list[int]:
        found = [root_id]
        for current in found:
            found += [
                e["id"] for e in self.employees_by_id.values()
                if e.get("manager_id") == current and e["id"] not in found
            ]
        return found

    def manager_chain(self, employee_id: int) -> list[int]:
        chain = [employee_id]
        while (m := self.employees_by_id[chain[-1]].get("manager_id")) and m not in chain:
            chain.append(m)
        return chain

    def employee_by_device_user_id(self, device_user_id: str):
        return next(
            (
                e
                for e in self.employees_by_id.values()
                if e["device_user_id"] == device_user_id
            ),
            None,
        )

    def has_admin(self) -> bool:
        return any(e["role"] == "admin" for e in self.employees_by_id.values())

    def create_first_admin(self, *, name: str, email: str, device_user_id: str) -> None:
        existing = self.employee_by_email(email)
        if existing is not None:
            existing["role"] = "admin"
            return
        employee = {
            "id": self._next_employee_id,
            "device_user_id": device_user_id,
            "name": name,
            "email": email,
            "role": "admin",
        }
        self.employees_by_id[employee["id"]] = employee
        self._next_employee_id += 1

    # bootstrap admin ------------------------------------------------------

    def ensure_bootstrap_admin(self, email) -> None:
        self._check_online()
        if not email:
            return
        if any(e["role"] == "admin" for e in self.employees_by_id.values()):
            return
        for employee in self.employees_by_id.values():
            if employee["email"].lower() == email.strip().lower():
                employee["role"] = "admin"

    # login codes / sessions ------------------------------------------------

    def count_recent_login_codes(self, *, email: str, since: datetime) -> int:
        return sum(
            1
            for row in self.login_codes
            if row["email"].lower() == email.lower() and row["created_at"] >= since
        )

    def count_recent_login_codes_by_ip(self, *, ip: str, since: datetime) -> int:
        return sum(1 for row in self.login_codes if row["requested_ip"] == ip and row["created_at"] >= since)

    def create_login_code(self, *, email, code_hash, pending_token_hash, requested_ip, expires_at):
        self._check_online()
        self.login_codes.append(
            {
                "id": self._next_login_code_id,
                "email": email,
                "code_hash": code_hash,
                "pending_token_hash": pending_token_hash,
                "requested_ip": requested_ip,
                "expires_at": expires_at,
                "attempts": 0,
                "consumed_at": None,
                "created_at": _ist_now(),
            }
        )
        self._next_login_code_id += 1

    def active_login_code(self, *, pending_token_hash: str):
        candidates = [
            row
            for row in self.login_codes
            if row["pending_token_hash"] == pending_token_hash and row["consumed_at"] is None
        ]
        return candidates[-1] if candidates else None

    def increment_login_code_attempts(self, login_code_id: int) -> None:
        for row in self.login_codes:
            if row["id"] == login_code_id:
                row["attempts"] += 1

    def consume_login_code(self, login_code_id: int) -> None:
        for row in self.login_codes:
            if row["id"] == login_code_id:
                row["consumed_at"] = _ist_now()

    def create_session(self, *, employee_id, token_hash, csrf_token, expires_at):
        self.sessions[token_hash] = {
            "employee_id": employee_id,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
            "revoked": False,
        }

    def session_employee(self, token_hash: str):
        session = self.sessions.get(token_hash)
        if session is None or session["revoked"] or session["expires_at"] < _ist_now():
            return None
        employee = self.employees_by_id.get(session["employee_id"])
        if employee is None:
            return None
        return {**employee, "csrf_token": session["csrf_token"]}

    def revoke_session(self, token_hash: str) -> None:
        if token_hash in self.sessions:
            self.sessions[token_hash]["revoked"] = True

    # api keys --------------------------------------------------------------

    def create_api_key(self, *, prefix: str, key_hash: str, label: str) -> None:
        self.api_keys_list.append(
            {
                "prefix": prefix,
                "key_hash": key_hash,
                "label": label,
                "created_at": _ist_now(),
                "revoked_at": None,
                "last_used_at": None,
            }
        )

    def active_api_key_count(self) -> int:
        return sum(1 for k in self.api_keys_list if k["revoked_at"] is None)

    def api_key_by_hash(self, key_hash: str):
        return next(
            (k for k in self.api_keys_list if k["key_hash"] == key_hash and k["revoked_at"] is None),
            None,
        )

    def touch_api_key(self, key_hash: str) -> None:
        for k in self.api_keys_list:
            if k["key_hash"] == key_hash:
                k["last_used_at"] = _ist_now()

    def revoke_api_key(self, prefix: str) -> None:
        for k in self.api_keys_list:
            if k["prefix"] == prefix:
                k["revoked_at"] = _ist_now()

    def api_keys(self):
        return sorted(self.api_keys_list, key=lambda k: k["created_at"], reverse=True)

    # settings ---------------------------------------------------------------

    def setting(self, key: str, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value


def _make_app():
    database = FakeDatabase()
    app = create_app(database=database)
    app.testing = True
    return app, database


def _assert_auth_layout(test: unittest.TestCase, html: str, heading: str) -> None:
    """U3: every auth page shares the NITCO split layout."""
    test.assertIn('class="auth-panel"', html)
    test.assertIn('alt="NITCO"', html)
    test.assertIn("brand/wordmark-white.png", html)
    test.assertIn("Attendance Hub</p>", html)
    test.assertNotIn("auth-brand", html)
    test.assertNotIn("brand-mark", html)
    test.assertNotIn("auth-bg", html)
    test.assertNotIn('"/brand/', html)
    test.assertEqual(html.count("<main"), 1)
    test.assertIn(f"<h1>{heading}</h1>", html)
    # The brand panel sits before main; the heading precedes any form.
    test.assertLess(html.index('class="auth-panel"'), html.index("<main"))
    if "<form" in html:
        test.assertLess(html.index("<h1>"), html.index("<form"))


class TurnstileSigninTest(unittest.TestCase):
    ENV = {"TURNSTILE_SITE_KEY": "site-key-1", "TURNSTILE_SECRET_KEY": "secret-1"}

    def setUp(self) -> None:
        with patch.dict(os.environ, self.ENV):
            self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com")
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.sent = []
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda email, code: self.sent.append(email)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def test_widget_and_script_render_with_site_key(self) -> None:
        page = self.client.get("/signin").text
        self.assertIn('data-sitekey="site-key-1"', page)
        self.assertIn("challenges.cloudflare.com/turnstile/v0/api.js", page)
        self.assertNotIn("secret-1", page)

    def test_widget_renders_inside_the_form_column(self) -> None:
        page = self.client.get("/signin").text
        main = page[page.index("<main"):page.index("</main>")]
        form = main[main.index("<form"):main.index("</form>")]
        self.assertIn('class="cf-turnstile auth-turnstile"', form)
        self.assertNotIn("cf-turnstile", page[:page.index("<main")])

    def test_signin_renders_brand_panel_and_email_form(self) -> None:
        page = self.client.get("/signin").text
        _assert_auth_layout(self, page, "Sign in")
        self.assertIn('name="email"', page)

    def test_code_page_uses_the_same_layout(self) -> None:
        with patch("essl_hub.turnstile.verify", return_value=True):
            page = self.client.post(
                "/signin", data={"email": "ravi.kumar@example.com", "cf-turnstile-response": "tok"}
            ).text
        _assert_auth_layout(self, page, "Check your email")
        self.assertIn('name="code"', page)

    def test_failed_check_sends_nothing(self) -> None:
        with patch("essl_hub.turnstile.verify", return_value=False) as verify:
            response = self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm you are human", response.text)
        self.assertEqual(self.database.login_codes, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(verify.call_args.args[1], "")  # no widget token posted

    def test_passed_check_sends_code(self) -> None:
        with patch("essl_hub.turnstile.verify", return_value=True) as verify:
            response = self.client.post(
                "/signin",
                data={"email": "ravi.kumar@example.com", "cf-turnstile-response": "tok"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sent, ["ravi.kumar@example.com"])
        self.assertEqual(verify.call_args.args[1], "tok")


class TurnstileOffTest(unittest.TestCase):
    def test_no_widget_without_keys(self) -> None:
        with patch.dict(os.environ, {"TURNSTILE_SITE_KEY": "", "TURNSTILE_SECRET_KEY": ""}):
            app, _ = _make_app()
        self.assertNotIn("cf-turnstile", app.test_client().get("/signin").text)


class WebFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com")
        self.client = self.app.test_client()

    def _capture_mailer(self):
        from essl_hub import web as web_module

        captured = {}

        def fake_mailer(email, code):
            captured["email"] = email
            captured["code"] = code

        from essl_hub import mail as mail_module

        original = mail_module.send_login_code
        mail_module.send_login_code = fake_mailer
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        return captured

    def _signin_from(self, behind_tunnel: str | None) -> str | None:
        self._capture_mailer()
        env = {} if behind_tunnel is None else {"ESSL_BEHIND_TUNNEL": behind_tunnel}
        with patch.dict(os.environ, env):
            if behind_tunnel is None:
                os.environ.pop("ESSL_BEHIND_TUNNEL", None)
            self.client.post(
                "/signin",
                data={"email": "ravi.kumar@example.com"},
                headers={"CF-Connecting-IP": "203.0.113.9"},
                environ_base={"REMOTE_ADDR": "172.18.0.5"},
            )
        return self.database.login_codes[-1]["requested_ip"]

    def test_signin_rate_limits_on_cf_connecting_ip_behind_tunnel(self) -> None:
        self.assertEqual(self._signin_from("1"), "203.0.113.9")

    def test_signin_ignores_cf_connecting_ip_without_tunnel_flag(self) -> None:
        self.assertEqual(self._signin_from(None), "172.18.0.5")

    def test_health_check(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True})

    def test_root_redirects_to_signin_when_signed_out(self) -> None:
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/signin", response.headers["Location"])

    def test_full_sign_in_flow_lands_on_today(self) -> None:
        captured = self._capture_mailer()
        response = self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("code", response.text.lower())
        self.assertEqual(captured["email"], "ravi.kumar@example.com")

        verify = self.client.post(
            "/signin/verify", data={"code": captured["code"]}, follow_redirects=False
        )
        self.assertEqual(verify.status_code, 302)
        self.assertTrue(verify.headers["Location"].endswith("/day"))

        week = self.client.get("/week")
        self.assertEqual(week.status_code, 200)
        self.assertIn("My week", week.text)

    def test_unknown_email_gets_identical_response_and_sends_nothing(self) -> None:
        # Covers AE4.
        captured = self._capture_mailer()
        known = self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        captured.clear()
        unknown = self.client.post("/signin", data={"email": "ghost@example.com"})
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual({}, captured)

    def test_mail_send_failure_gives_the_same_response_as_success(self) -> None:
        # Covers R11: a Resend failure must not change the response shape,
        # since that would itself reveal the email is on the roster.
        from essl_hub import mail as mail_module
        from essl_hub.mail import MailError

        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: (_ for _ in ()).throw(MailError("down"))
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

        failing = self.client.post("/signin", data={"email": "ravi.kumar@example.com"})

        mail_module.send_login_code = lambda e, c: None
        succeeding = self.client.post("/signin", data={"email": "ravi.kumar@example.com"})

        self.assertEqual(failing.status_code, succeeding.status_code)
        self.assertEqual(failing.text, succeeding.text)

    def test_employee_cannot_view_hr_route(self) -> None:
        self._capture_mailer()
        self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        code = self.database.login_codes[-1]
        # sign in properly via the captured code path
        captured = self._capture_mailer()
        self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        # /hr now redirects to /team, which sends an employee to their own day.
        response = self.client.get("/hr", follow_redirects=True)
        self.assertEqual(response.request.path, "/day")

    def test_hr_navigation_offers_employee_data_but_not_the_admin_console(self) -> None:
        self.database.upsert_employee(device_user_id="900", name="Priya HR", email="priya@example.com")
        priya = self.database.employee_by_email("priya@example.com")
        self.database.set_employee_role(priya["id"], "hr")
        captured = self._capture_mailer()
        self.client.post("/signin", data={"email": "priya@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        page = self.client.get("/team")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/team"', page.text)
        self.assertIn("<span>Everyone</span>", page.text)
        self.assertNotIn('href="/admin"', page.text)

    def test_hr_cannot_view_admin_route(self) -> None:
        self.database.upsert_employee(device_user_id="900", name="Priya HR", email="priya@example.com")
        priya = self.database.employee_by_email("priya@example.com")
        self.database.set_employee_role(priya["id"], "hr")
        captured = self._capture_mailer()
        self.client.post("/signin", data={"email": "priya@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        self.assertEqual(self.client.get("/settings").status_code, 403)
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/devices"))
        # the old admin-only role route is gone; roles moved to /people
        role_response = self.client.post("/admin/employees/1/role", data={"role": "employee"})
        self.assertEqual(role_response.status_code, 404)


def _punch_row(fingerprint: str, at: datetime, direction: str, uid: str = "118") -> dict:
    return {
        "fingerprint": fingerprint,
        "device_id": DEVICES[1].id,
        "site": "Gachibowli",
        "device_user_id": uid,
        "event_timestamp": at,
        "trusted_direction": direction,
    }


def _fixed_now(at: datetime):
    """Pin the routes' clock (not auth's) so work-day rules are deterministic."""
    return patch.multiple("essl_hub.routes.common", _now_naive=lambda: at), patch(
        "essl_hub.routes.me._now_naive", lambda: at
    )


class HoursRenderingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com")
        self.client = self.app.test_client()

    def _sign_in(self) -> dict:
        from essl_hub import mail as mail_module

        captured = {}

        def fake_mailer(email, code):
            captured["code"] = code

        original = mail_module.send_login_code
        mail_module.send_login_code = fake_mailer
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        return captured

    def test_week_view_renders_with_a_normal_day_of_punches(self) -> None:
        self._sign_in()
        today = _ist_today()
        # a closed day earlier this week
        monday = today - timedelta(days=today.weekday())
        self.database.insert_punches(
            [
                {
                    "fingerprint": "fp-1",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "118",
                    "event_timestamp": datetime.combine(monday, datetime.min.time()) + timedelta(hours=9),
                    "trusted_direction": "entry",
                },
                {
                    "fingerprint": "fp-2",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "118",
                    "event_timestamp": datetime.combine(monday, datetime.min.time()) + timedelta(hours=18),
                    "trusted_direction": "exit",
                },
            ]
        )
        response = self.client.get("/week")
        self.assertEqual(response.status_code, 200)
        self.assertIn("9h 00m", response.text)

    def test_short_day_renders_the_exact_short_by_label(self) -> None:
        # Pins the shared status-chip macro's parameterised short-label path.
        self._sign_in()
        today = _ist_today()
        monday = today - timedelta(days=today.weekday())
        start = datetime.combine(monday, datetime.min.time())
        self.database.insert_punches(
            [
                {
                    "fingerprint": "fp-short-in",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "118",
                    "event_timestamp": start + timedelta(hours=9),
                    "trusted_direction": "entry",
                },
                {
                    "fingerprint": "fp-short-out",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "118",
                    "event_timestamp": start + timedelta(hours=15),
                    "trusted_direction": "exit",
                },
            ]
        )
        response = self.client.get("/week")
        self.assertIn("Short by 2h 00m", response.text)

    def test_incomplete_day_shows_exact_label_and_zero_effective(self) -> None:
        self._sign_in()
        today = _ist_today()
        # A past weekday: today would still be "open", and on Monday the
        # day before is a weekend.
        yesterday = today - timedelta(days=1)
        while yesterday.weekday() >= 5:
            yesterday -= timedelta(days=1)
        start_of_day = datetime.combine(yesterday, datetime.min.time())
        self.database.insert_punches(
            [
                {
                    "fingerprint": "fp-3",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "118",
                    "event_timestamp": start_of_day + timedelta(hours=9),
                    "trusted_direction": "entry",
                }
            ]
        )
        response = self.client.get(f"/day?date={yesterday.isoformat()}")
        self.assertIn("Missing exit punch", response.text)
        self.assertIn("No exit recorded", response.text)

    def _at(self, at: datetime):
        common, me = _fixed_now(at)
        common.start()
        me.start()
        self.addCleanup(common.stop)
        self.addCleanup(me.stop)

    def test_at_three_am_today_is_still_the_previous_work_day_and_open(self) -> None:
        self._sign_in()
        day = datetime(2026, 9, 9)  # a Wednesday
        self.database.insert_punches([_punch_row("fp-n1", day + timedelta(hours=22, minutes=10), "entry")])
        self._at(day + timedelta(days=1, hours=3))
        response = self.client.get("/day")
        self.assertIn("<h1>Today</h1>", response.text)
        self.assertIn("Wednesday 09 September 2026", response.text)
        self.assertIn("work day from 06:00", response.text)
        self.assertIn("Now, inside", response.text)
        self.assertIn("+1</sup>", response.text)  # the 03:00 "now" node

    def test_day_view_reads_one_clock_instant_per_request(self) -> None:
        # U10: a clock that ticks on every read must still yield one stretch
        # value measured from the first (and only) read.
        self._sign_in()
        day = datetime(2026, 9, 9)
        self.database.insert_punches([_punch_row("fp-s1", day + timedelta(hours=9), "entry")])
        first = day + timedelta(hours=11, seconds=5)
        ticks = iter(first + timedelta(seconds=n) for n in range(100))
        clock = lambda: next(ticks)  # noqa: E731
        with patch("essl_hub.routes.common._now_naive", clock), patch("essl_hub.routes.me._now_naive", clock):
            response = self.client.get("/day")
        self.assertIn('data-timer-seconds="7205"', response.text)

    def test_night_shift_day_view_marks_post_midnight_exit(self) -> None:
        self._sign_in()
        day = datetime(2026, 9, 9)
        self.database.insert_punches([
            _punch_row("fp-n1", day + timedelta(hours=22, minutes=10), "entry"),
            _punch_row("fp-n2", day + timedelta(days=1, hours=3, minutes=40), "exit"),
        ])
        self._at(datetime(2026, 9, 12, 12))
        response = self.client.get("/day?date=2026-09-09")
        self.assertIn("5h 30m", response.text)
        self.assertEqual(response.text.count("+1</sup>"), 1)
        next_day = self.client.get("/day?date=2026-09-10")
        self.assertNotIn("5h 30m", next_day.text)

    def test_flagged_day_counts_pairs_and_shows_needs_review(self) -> None:
        self._sign_in()
        day = datetime(2026, 9, 9)
        self.database.insert_punches([
            _punch_row("fp-r1", day + timedelta(hours=9), "entry"),
            _punch_row("fp-r2", day + timedelta(hours=14, minutes=33), "entry"),
            _punch_row("fp-r3", day + timedelta(hours=18), "exit"),
        ])
        self._at(datetime(2026, 9, 12, 12))
        response = self.client.get("/day?date=2026-09-09")
        self.assertIn("Needs review.", response.text)
        self.assertIn("No exit after 09:00", response.text)
        self.assertIn("3h 27m", response.text)
        self.assertNotIn("Missing exit punch", response.text)

    def test_week_at_monday_three_am_still_shows_previous_week(self) -> None:
        self._sign_in()
        self._at(datetime(2026, 9, 14, 3))  # Monday
        response = self.client.get("/week")
        self.assertIn("Mon 07 Sep to Fri 11 Sep 2026", response.text)
        self.assertIn("work days from 06:00", response.text)

    def test_day_start_zero_restores_midnight_days(self) -> None:
        self._sign_in()
        self.database.set_setting("day_start_hour", "0")
        day = datetime(2026, 9, 9)
        self.database.insert_punches([
            _punch_row("fp-n1", day + timedelta(hours=22, minutes=10), "entry"),
            _punch_row("fp-n2", day + timedelta(days=1, hours=3, minutes=40), "exit"),
        ])
        self._at(datetime(2026, 9, 10, 3))
        response = self.client.get("/day")
        self.assertIn("Thursday 10 September 2026", response.text)
        self.assertIn("work day from 00:00", response.text)
        past = self.client.get("/day?date=2026-09-09")
        self.assertIn("Missing exit punch", past.text)

    def test_employee_navigation_has_neither_hr_nor_admin_links(self) -> None:
        self._sign_in()
        page = self.client.get("/week")
        self.assertNotIn('href="/team"', page.text)
        self.assertNotIn('href="/admin"', page.text)

    def test_hr_export_csv_contains_headers(self) -> None:
        self.database.upsert_employee(device_user_id="900", name="Priya HR", email="priya@example.com")
        priya = self.database.employee_by_email("priya@example.com")
        self.database.set_employee_role(priya["id"], "hr")
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "priya@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        response = self.client.get("/hr/export.csv?from=2026-10-01&to=2026-10-02")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/export.csv?from=2026-10-01&to=2026-10-02"))
        response = self.client.get(response.headers["Location"])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.text.startswith("\ufeffEmp ID,Name,"))
        self.assertIn("Effective (h)", response.text)


class LiveTodayTest(unittest.TestCase):
    """U11: the live #day-live region, its states, polling and partials."""

    DAY = datetime(2026, 9, 9)  # a Wednesday

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com")
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _sign_in(self, email: str = "ravi.kumar@example.com") -> None:
        self.client.post("/signin", data={"email": email})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _at(self, at: datetime) -> None:
        common, me = _fixed_now(at)
        common.start()
        me.start()
        self.addCleanup(common.stop)
        self.addCleanup(me.stop)

    def _punches(self, *rows) -> None:
        self.database.insert_punches(
            [_punch_row(f"lv-{i}", self.DAY + offset, direction) for i, (offset, direction) in enumerate(rows)]
        )

    def _today(self, at: timedelta, *rows, headers=None) -> str:
        self._sign_in()
        self._punches(*rows)
        self._at(self.DAY + at)
        return self.client.get("/day", headers=headers or {}).text

    @staticmethod
    def _attr(page: str, name: str) -> str:
        return re.search(rf'{name}="([^"]*)"', page).group(1)

    def test_live_render_carries_one_instant_for_ring_stretch_and_epoch(self) -> None:
        page = self._today(timedelta(hours=11, seconds=5), (timedelta(hours=9), "entry"))
        self.assertEqual(self._attr(page, "data-base-seconds"), "7205")
        self.assertEqual(self._attr(page, "data-target-seconds"), "28800")
        self.assertEqual(self._attr(page, "data-live"), "true")
        self.assertEqual(self._attr(page, "data-state"), "inside")
        expected = int((self.DAY + timedelta(hours=11, seconds=5)).replace(tzinfo=_IST).timestamp() * 1000)
        self.assertEqual(self._attr(page, "data-server-epoch-ms"), str(expected))
        # ring digits, this stretch, the open log stretch and gross (inside
        # since first in) all agree to the second
        self.assertEqual(page.count('data-tick="7205"'), 4)
        self.assertIn(">2:00:05<", page)

    def test_inside_under_target(self) -> None:
        page = self._today(timedelta(hours=11, seconds=5), (timedelta(hours=9), "entry"))
        self.assertIn('<span class="live-dot" aria-hidden="true"></span>Inside</h2>', page)
        self.assertIn('data-left>6h 00m left<', page)

    def test_inside_met(self) -> None:
        page = self._today(timedelta(hours=17, minutes=12), (timedelta(hours=9), "entry"))
        self.assertIn("Inside · Target met</h2>", page)
        self.assertIn("data-left>+12m over<", page)
        self.assertEqual(self._attr(page, "data-state"), "inside")

    def test_out_under_target_is_frozen(self) -> None:
        page = self._today(
            timedelta(hours=18),
            (timedelta(hours=10), "entry"),
            (timedelta(hours=17, minutes=43), "exit"),
        )
        self.assertIn("Out since 17:43:00</h2>", page)
        self.assertIn("data-left>17m left<", page)
        self.assertEqual(self._attr(page, "data-live"), "false")
        self.assertEqual(self._attr(page, "data-state"), "out")
        self.assertIn("data-poll=", page)

    def test_out_met(self) -> None:
        page = self._today(
            timedelta(hours=18),
            (timedelta(hours=9), "entry"),
            (timedelta(hours=17, minutes=12), "exit"),
        )
        self.assertIn("Out · Target met</h2>", page)
        self.assertIn("data-left>+12m over<", page)
        self.assertEqual(self._attr(page, "data-state"), "met")

    def test_review_flag_adds_needs_review_line(self) -> None:
        page = self._today(
            timedelta(hours=15),
            (timedelta(hours=8), "entry"),
            (timedelta(hours=9), "entry"),
            (timedelta(hours=12), "exit"),
            (timedelta(hours=14, minutes=33), "entry"),
        )
        self.assertIn("Inside</h2>", page)
        self.assertIn('<p class="review-line">Needs review: no exit after 08:00</p>', page)
        self.assertIn("data-left>", page)

    def test_today_with_no_complete_pair_shows_incomplete_and_empty_ring(self) -> None:
        page = self._today(timedelta(hours=12), (timedelta(hours=10), "exit"))
        self.assertIn("Incomplete: no exit yet</h2>", page)
        self.assertNotIn("data-left", page)
        self.assertNotIn('class="figure-value', page)
        self.assertNotIn("No punches yet today", page)
        self.assertIn("data-poll=", page)

    def test_no_punches_still_polls_and_notes_previous_day_entry(self) -> None:
        self._sign_in()
        self.database.insert_punches([_punch_row("lv-prev", self.DAY - timedelta(hours=2), "entry")])
        self._at(self.DAY + timedelta(hours=7))
        page = self.client.get("/day").text
        self.assertIn("No punches yet today", page)
        self.assertIn('id="day-live"', page)
        self.assertIn('data-poll="/day?employee=1&amp;date=2026-09-09"', page)
        self.assertIn('Last punch: entry at <span class="num">22:00</span> on the previous work day', page)

    def test_past_day_is_static(self) -> None:
        self._sign_in()
        self._punches((timedelta(hours=9), "entry"), (timedelta(hours=17), "exit"))
        self._at(datetime(2026, 9, 12, 12))
        page = self.client.get("/day?date=2026-09-09").text
        self.assertIn("Target met at 17:00</h2>", page)
        self.assertNotIn("data-poll=", page)
        self.assertNotIn("data-left", page)
        self.assertEqual(self._attr(page, "data-live"), "false")

    def test_htmx_request_gets_only_the_region_and_restore_gets_the_page(self) -> None:
        partial = self._today(
            timedelta(hours=11), (timedelta(hours=9), "entry"), headers={"HX-Request": "true"}
        )
        self.assertTrue(partial.lstrip().startswith("<div id=\"day-live\""))
        self.assertNotIn("<html", partial)
        self.assertIn('<ol class="day-log">', partial)
        self.assertNotIn("live-status", partial)
        restore = self.client.get("/day", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
        self.assertIn("<html", restore.text)
        self.assertIn("HX-Request", restore.headers.get("Vary", ""))

    def test_poll_for_a_rolled_over_date_refreshes_the_page(self) -> None:
        self._sign_in()
        self._at(self.DAY + timedelta(days=1, hours=6, seconds=1))
        response = self.client.get("/day?date=2026-09-09", headers={"HX-Request": "true"})
        self.assertEqual(response.headers.get("HX-Refresh"), "true")
        self.assertEqual(response.text, "")

    def test_unauthenticated_partial_gets_hx_redirect_not_signin_html(self) -> None:
        response = self.client.get("/day", headers={"HX-Request": "true"})
        self.assertEqual(response.headers.get("HX-Redirect"), "/signin")
        self.assertNotIn("<form", response.text)
        self.assertEqual(self.client.get("/day").status_code, 302)

    def test_manager_poll_for_a_report_returns_the_reports_region(self) -> None:
        self.database.upsert_employee(device_user_id="900", name="Boss", email="boss@example.com")
        boss = self.database.employee_by_email("boss@example.com")
        self.database.set_employee_role(boss["id"], "manager")
        ravi = self.database.employee_by_email("ravi.kumar@example.com")
        self.database.set_employee_manager(ravi["id"], boss["id"])
        self._sign_in("boss@example.com")
        self._punches((timedelta(hours=9), "entry"))
        self._at(self.DAY + timedelta(hours=10))
        page = self.client.get(
            f"/day?employee={ravi['id']}&date=2026-09-09", headers={"HX-Request": "true"}
        ).text
        self.assertEqual(self._attr(page, "data-base-seconds"), "3600")
        self.assertIn(f'data-poll="/day?employee={ravi["id"]}&amp;date=2026-09-09"', page)

    def test_ring_digits_are_silent_and_status_region_is_outside_and_empty(self) -> None:
        page = self._today(timedelta(hours=11), (timedelta(hours=9), "entry"))
        self.assertIn('class="figure-value is-live" aria-hidden="true" data-tick=', page)
        self.assertIn('<span class="visually-hidden" data-tick-hours="7200">2h 00m</span>', page)
        self.assertNotIn('role="timer"', page)
        self.assertNotIn('aria-live="polite" data-timer', page)
        status = '<div id="live-status" class="visually-hidden" role="status" aria-live="polite"></div>'
        self.assertIn(status, page)
        region_end = page.index('<script src="/static/timer.js"')
        self.assertGreater(page.index(status), region_end)
        self.assertIn('role="img" aria-label="2h 00m effective, 25% of the 8h 00m target"', page)

    def test_log_times_have_seconds_and_stretches_sum_to_the_ring(self) -> None:
        page = self._today(
            timedelta(hours=15, minutes=7, seconds=9),
            (timedelta(hours=9, seconds=12), "entry"),
            (timedelta(hours=12, minutes=30, seconds=41), "exit"),
            (timedelta(hours=13, minutes=2, seconds=3), "entry"),
        )
        self.assertIn('log-time num">09:00:12</span>', page)
        self.assertIn('log-time num">12:30:41</span>', page)
        stretches = [int(v) for v in re.findall(r'data-log-stretch="(\d+)"', page)]
        self.assertEqual(len(stretches), 2)
        self.assertEqual(sum(stretches), int(self._attr(page, "data-base-seconds")))
        self.assertIn("Break <span class=\"num\">0:31:22</span>", page)

    def test_week_table_keeps_hours_and_minutes(self) -> None:
        self._sign_in()
        self._punches((timedelta(hours=9), "entry"), (timedelta(hours=16, minutes=42, seconds=30), "exit"))
        self._at(datetime(2026, 9, 11, 12))
        page = self.client.get("/week").text
        self.assertIn("7h 42m", page)
        self.assertNotIn("7:42:30", page)


class QuickViewTest(unittest.TestCase):
    """Quick-view drawer fragment: same Today context as /day, same access."""

    DAY = datetime(2026, 9, 9)

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.ravi = self._person("118", "Ravi Kumar")
        common, me = _fixed_now(self.DAY + timedelta(hours=11))
        common.start()
        me.start()
        self.addCleanup(common.stop)
        self.addCleanup(me.stop)

    def _person(self, uid: str, name: str, role: str = "employee") -> dict:
        email = f"{uid}@example.com"
        self.database.upsert_employee(device_user_id=uid, name=name, email=email)
        person = self.database.employee_by_email(email)
        if role != "employee":
            self.database.set_employee_role(person["id"], role)
        return self.database.employee_by_email(email)

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _quick(self, employee_id: int, query: str = ""):
        return self.client.get(f"/quick/{employee_id}{query}", headers={"HX-Request": "true"})

    def test_hr_gets_fragment_with_name_live_region_and_links(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        response = self._quick(self.ravi["id"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("HX-Request", response.headers.get("Vary", ""))
        page = response.text
        self.assertNotIn("<html", page)
        self.assertIn('id="quick-view-title"', page)
        self.assertIn(">Ravi Kumar</h2>", page)
        self.assertIn('id="day-live"', page)
        self.assertIn(f'href="/day?employee={self.ravi["id"]}"', page)
        self.assertIn(f'href="/week?employee={self.ravi["id"]}"', page)

    def test_person_inside_gets_live_region_polling_their_day(self) -> None:
        self.database.insert_punches([_punch_row("qv-1", self.DAY + timedelta(hours=9), "entry")])
        self._sign_in(self._person("1", "Hema HR", "hr"))
        page = self._quick(self.ravi["id"]).text
        self.assertIn('data-live="true"', page)
        self.assertIn(f'data-poll="/day?employee={self.ravi["id"]}&amp;date=2026-09-09"', page)

    def test_edit_details_only_from_people_for_editors(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        self.assertIn("Edit details", self._quick(self.ravi["id"], "?from=people").text)
        self.assertNotIn("Edit details", self._quick(self.ravi["id"]).text)

    def test_manager_sees_subtree_only_and_unknown_is_404(self) -> None:
        boss = self._person("900", "Boss", "manager")
        other = self._person("901", "Other")
        self.database.set_employee_manager(self.ravi["id"], boss["id"])
        self._sign_in(boss)
        self.assertEqual(self._quick(self.ravi["id"]).status_code, 200)
        self.assertEqual(self._quick(other["id"]).status_code, 404)
        self.assertEqual(self._quick(99999).status_code, 404)
        self.assertNotIn("Edit details", self._quick(self.ravi["id"], "?from=people").text)

    def test_no_punches_shows_empty_state(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        self.assertIn("No punches yet today", self._quick(self.ravi["id"]).text)

    def test_expired_session_gets_hx_redirect(self) -> None:
        response = self._quick(self.ravi["id"])
        self.assertEqual(response.headers.get("HX-Redirect"), "/signin")
        self.assertNotIn("<form", response.text)

    def test_plain_get_redirects_to_full_day(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        response = self.client.get(f"/quick/{self.ravi['id']}")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/day?employee={self.ravi['id']}"))


class AdminViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="1", name="Admin", email="admin@example.com")
        admin = self.database.employee_by_email("admin@example.com")
        self.database.set_employee_role(admin["id"], "admin")
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/settings").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _set_role(self, employee_id: int, role: str):
        return self.client.post(
            f"/people/{employee_id}/role", data={"role": role, "csrf": self._csrf()}
        )

    def _add_employee(self, device_user_id: str = "118", name: str = "Ravi Kumar") -> dict:
        self.database.upsert_employee(
            device_user_id=device_user_id,
            name=name,
            email=f"{device_user_id}@example.com",
        )
        return self.database.employee_by_email(f"{device_user_id}@example.com")

    def test_admin_redirects_to_devices(self) -> None:
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/devices"))

    def test_settings_holds_target_and_keys_without_an_employee_table(self) -> None:
        self.database.upsert_employee(
            device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com"
        )
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Daily target", response.text)
        self.assertIn("HRMS API keys", response.text)
        self.assertNotIn("Ravi Kumar", response.text)

    def test_valid_roster_upload_creates_employees_and_shows_outcomes(self) -> None:
        csv_bytes = (
            b"name,work email,device user id\r\n"
            b"Karthik,karthik@example.com,184\r\n"
            b"Vijay,vijay@example.com,55\r\n"
        )
        response = self.client.post(
            "/people/import",
            data={"roster": (io.BytesIO(csv_bytes), "roster.csv"), "csrf": self._csrf()},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("<strong>2</strong> added", response.text)
        self.assertIsNotNone(self.database.employee_by_email("karthik@example.com"))
        self.assertIsNotNone(self.database.employee_by_email("vijay@example.com"))

    def test_setting_target_changes_the_stored_minutes(self) -> None:
        response = self.client.post(
            "/settings/target",
            data={"hours": "7", "minutes": "30", "csrf": self._csrf()},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database.setting("daily_target_minutes"), "450")

    def test_setting_day_start_hour_is_stored_and_confirmed(self) -> None:
        self.assertEqual(self.database.setting("day_start_hour", "6"), "6")
        self.assertIn('name="hour"', self.client.get("/settings").text)
        response = self.client.post(
            "/settings/day-start", data={"hour": "0", "csrf": self._csrf()}, follow_redirects=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database.setting("day_start_hour"), "0")
        self.assertIn("Work days now start at 00:00.", response.text)

    def test_day_start_hour_outside_range_is_refused_inline(self) -> None:
        for bad in ("24", "-1", "abc", ""):
            response = self.client.post("/settings/day-start", data={"hour": bad, "csrf": self._csrf()})
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn("Enter a whole hour from 0 to 23.", response.text)
        self.assertIsNone(self.database.setting("day_start_hour"))

    def test_day_start_hour_requires_csrf(self) -> None:
        response = self.client.post("/settings/day-start", data={"hour": "5"})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.database.setting("day_start_hour"))

    def test_covers_r21_unmapped_device_id_is_shown_on_the_devices_page(self) -> None:
        # Covers R21.
        self.database.insert_punches(
            [
                {
                    "fingerprint": "fp-unmapped",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "231",
                    "event_timestamp": _ist_now(),
                }
            ]
        )
        devices = self.client.get("/devices").text
        self.assertIn('href="/people/issues"', devices)
        people = self.client.get("/people").text
        self.assertIn('class="nav-count"', people)
        self.assertIn('class="subtab-count"', people)
        page = self.client.get("/people/issues").text
        self.assertIn("231", page)
        self.assertIn("Not on roster", page)
        self.assertIn('href="/people/new?device_user_id=231', page)

    def test_name_mismatch_can_be_marked_same_person(self) -> None:
        self.database.upsert_employee(device_user_id="NP0002", name="Ravi Teja", email="ravi@example.com")
        self.database.insert_punches([{"fingerprint": "fp-nm", "device_id": DEVICES[0].id, "site": "Uppal",
                                       "device_user_id": "NP0002", "employee_name": "Mohan Das", "event_timestamp": _ist_now()}])
        page = self.client.get("/people/issues").text
        self.assertIn("Name differs", page)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        self.client.post("/people/issues/dismiss", data={"csrf": csrf, "key": "name:NP0002:das mohan"})
        self.assertNotIn("Name differs", self.client.get("/people/issues").text)
        # A rename on the device brings the issue back.
        self.database.insert_punches([{"fingerprint": "fp-nm2", "device_id": DEVICES[0].id, "site": "Uppal",
                                       "device_user_id": "NP0002", "employee_name": "Suresh K",
                                       "event_timestamp": _ist_now() + timedelta(seconds=5)}])
        self.assertIn("Name differs", self.client.get("/people/issues").text)

    def test_device_name_is_the_latest_punch_name(self) -> None:
        now = _ist_now()
        self.database.insert_punches([
            {"fingerprint": "fp-a", "device_id": DEVICES[0].id, "site": "Uppal", "device_user_id": "NP0777",
             "employee_name": "Zara Old", "event_timestamp": now - timedelta(days=2)},
            {"fingerprint": "fp-b", "device_id": DEVICES[0].id, "site": "Uppal", "device_user_id": "NP0777",
             "employee_name": "Bala New", "event_timestamp": now},
        ])
        page = self.client.get("/people/issues").text
        self.assertIn("On device: <strong>Bala New</strong>", page)
        self.assertNotIn("Zara Old", page)

    def test_link_confirm_states_punch_count(self) -> None:
        self.database.upsert_employee(device_user_id="sita@example.com", name="Sita Rao", email="sita@example.com")
        self.database.insert_punches([{"fingerprint": f"fp-c{i}", "device_id": DEVICES[0].id, "site": "Uppal",
                                       "device_user_id": "NP0231", "employee_name": "Sita R",
                                       "event_timestamp": _ist_now() - timedelta(minutes=i)} for i in range(3)])
        page = self.client.get("/people/issues?confirm=1&device_user_id=NP0231&person=sita@example.com").text
        self.assertIn("Link NP0231 to Sita Rao?", page)
        self.assertIn("<strong>3 punches</strong> will start counting for Sita Rao", page)
        # Nothing changes until the confirm form posts.
        self.assertEqual(self.database.employee_by_email("sita@example.com")["device_user_id"], "sita@example.com")

    def test_crafted_error_param_is_not_rendered(self) -> None:
        page = self.client.get("/people/issues?error=Call+999+now&linked=X&who=Evil").text
        # Only the theme form's escaped return URL may echo the query.
        self.assertNotIn('class="notice notice-bad"', page)
        self.assertNotIn("Linked X to Evil", page)

    def test_never_punched_dismiss_lowers_badge(self) -> None:
        self.database.upsert_employee(device_user_id="NP0500", name="Ghost", email="ghost@example.com")
        page = self.client.get("/people/issues").text
        before = int(re.search(r'class="subtab-count"[^>]*>(\d+)<', page).group(1))
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        self.client.post("/people/issues/dismiss", data={"csrf": csrf, "key": "never:NP0500"})
        page = self.client.get("/people/issues").text
        self.assertNotIn("never:NP0500", page)
        after = re.search(r'class="subtab-count"[^>]*>(\d+)<', page)
        self.assertEqual(int(after.group(1)) if after else 0, before - 1)

    def test_htmx_partial_skips_badge(self) -> None:
        from essl_hub.routes import people as people_routes

        with patch.object(people_routes, "issue_count", side_effect=AssertionError("badge computed")):
            response = self.client.get("/people", headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 200)

    def test_old_devices_ids_url_redirects_to_people(self) -> None:
        response = self.client.get("/devices/ids")
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response.headers["Location"].endswith("/people/issues"))

    def test_unknown_id_can_be_linked_to_a_roster_person(self) -> None:
        self.database.upsert_employee(device_user_id="sita@example.com", name="Sita Rao", email="sita@example.com")
        self.database.insert_punches([{"fingerprint": "fp-link", "device_id": DEVICES[0].id, "site": "Uppal",
                                       "device_user_id": "NP0231", "employee_name": "Sita R", "event_timestamp": _ist_now()}])
        page = self.client.get("/people/issues").text
        self.assertIn("Link to Sita Rao", page)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        sita = self.database.employee_by_email("sita@example.com")
        response = self.client.post("/people/issues/link", data={"csrf": csrf, "device_user_id": "NP0231", "employee_id": sita["id"]},
                                    follow_redirects=True)
        self.assertIn("Linked NP0231 to Sita Rao", response.text)
        self.assertIn("1 past punch now count", response.text)
        # The flash shows once.
        self.assertNotIn("Linked NP0231", self.client.get("/people/issues").text)
        self.assertEqual(self.database.employee_by_id(sita["id"])["device_user_id"], "NP0231")
        self.assertNotIn("Not on roster", response.text)

    def test_link_refuses_an_id_someone_else_has(self) -> None:
        self.database.upsert_employee(device_user_id="NP0005", name="Owner", email="owner@example.com")
        self.database.upsert_employee(device_user_id="new@example.com", name="New", email="new@example.com")
        new = self.database.employee_by_email("new@example.com")
        response = self.client.post("/people/issues/link", data={"csrf": self._csrf(), "device_user_id": "NP0005", "employee_id": new["id"]},
                                    follow_redirects=True)
        self.assertIn("Owner already uses device ID NP0005", response.text)
        self.assertEqual(self.database.employee_by_id(new["id"])["device_user_id"], "new@example.com")

    def test_add_person_form_is_prefilled_from_the_issue(self) -> None:
        page = self.client.get("/people/new?device_user_id=NP0231&name=New+Joiner").text
        self.assertIn('value="NP0231"', page)
        self.assertIn('value="New Joiner"', page)

    def test_admin_navigation_links_to_the_employee_data(self) -> None:
        # An admin is authorised for Everyone, so the nav must offer it.
        page = self.client.get("/settings")
        self.assertIn('href="/team"', page.text)

    # roster templates (R1) --------------------------------------------------

    def test_roster_templates_match_the_importer(self) -> None:
        csv_response = self.client.get("/people/import/template.csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("attachment", csv_response.headers["Content-Disposition"])
        self.assertEqual(
            csv_response.text.strip(), "name,work email,device user id,manager email,role"
        )
        # the file the admin downloads must import cleanly as-is
        self.assertEqual(roster.parse_csv(csv_response.text), [])

        xlsx_response = self.client.get("/people/import/template.xlsx")
        self.assertEqual(xlsx_response.status_code, 200)
        self.assertEqual(roster.parse_xlsx(xlsx_response.data), [])
        workbook = openpyxl.load_workbook(io.BytesIO(xlsx_response.data))
        self.assertEqual(workbook.sheetnames, ["Roster", "Instructions"])
        header = [cell.value for cell in workbook["Roster"][1]]
        self.assertIn("manager email", header)
        self.assertIn("role", header)

    def test_import_page_links_both_templates_and_admin_links_to_it(self) -> None:
        page = self.client.get("/people/import")
        self.assertIn('href="/people/import/template.csv"', page.text)
        self.assertIn('href="/people/import/template.xlsx"', page.text)
        self.assertIn('href="/people/import"', self.client.get("/people").text)

    # roles (R3 follow-up) --------------------------------------------------

    def test_admin_can_set_an_employees_role(self) -> None:
        ajay = self._add_employee()
        response = self._set_role(ajay["id"], "hr")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.database.employee_by_id(ajay["id"])["role"], "hr")
        self.assertIn("hr", self.client.get("/people").text)

    def test_role_change_requires_csrf(self) -> None:
        ajay = self._add_employee()
        response = self.client.post(
            f"/people/{ajay['id']}/role", data={"role": "hr"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.database.employee_by_id(ajay["id"])["role"], "employee")

    def test_unknown_role_is_refused_without_a_write(self) -> None:
        ajay = self._add_employee()
        response = self._set_role(ajay["id"], "superuser")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Choose employee, manager, hr or admin", response.text)
        self.assertEqual(self.database.employee_by_id(ajay["id"])["role"], "employee")

    def test_an_admin_cannot_change_their_own_role(self) -> None:
        admin = self.database.employee_by_email("admin@example.com")
        response = self._set_role(admin["id"], "hr")
        self.assertEqual(response.status_code, 200)
        self.assertIn("cannot change your own role", response.text)
        self.assertEqual(self.database.employee_by_id(admin["id"])["role"], "admin")

    def test_creating_a_third_active_key_is_refused(self) -> None:
        self.client.post("/settings/keys", data={"label": "Key one", "csrf": self._csrf()})
        self.client.post("/settings/keys", data={"label": "Key two", "csrf": self._csrf()})
        response = self.client.post("/settings/keys", data={"label": "Key three", "csrf": self._csrf()})
        self.assertIn("already active", response.text)
        self.assertEqual(self.database.active_api_key_count(), 2)


class AdminDeviceEditTest(unittest.TestCase):
    """Covers R4, R6, R15, R20: editing a device's connection settings."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="1", name="Admin", email="admin@example.com")
        admin = self.database.employee_by_email("admin@example.com")
        self.database.set_employee_role(admin["id"], "admin")
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/settings").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _post(self, device_id: str, **data):
        data.setdefault("csrf", self._csrf())
        return self.client.post(f"/devices/{device_id}", data=data)

    def test_edit_persists_and_redirects_with_saved_marker(self) -> None:
        response = self._post("uppal-x990", ip="192.168.5.5", port="5000", comm_password="")
        self.assertEqual(response.status_code, 302)
        self.assertIn("saved=uppal-x990", response.headers["Location"])
        device = self.database.device_by_id("uppal-x990")
        self.assertEqual((device["ip"], device["port"]), ("192.168.5.5", 5000))
        self.assertIsNotNone(device["connection_updated_at"])
        page = self.client.get("/devices?saved=uppal-x990")
        self.assertIn("192.168.5.5", page.text)
        self.assertIn("Saved", page.text)

    def test_pending_until_a_newer_attempt_then_applied(self) -> None:
        self._post("uppal-x990", ip="192.168.1.201", port="4370", comm_password="")
        device = self.database.device_by_id("uppal-x990")
        device["last_attempt"] = device["connection_updated_at"] - timedelta(minutes=5)
        page = self.client.get("/devices")
        self.assertIn("Change pending", page.text)
        device["last_attempt"] = device["connection_updated_at"] + timedelta(minutes=1)
        page = self.client.get("/devices")
        self.assertIn("Change applied", page.text)
        self.assertNotIn("Change pending", page.text)

    def test_non_numeric_port_renders_card_scoped_error_without_writing(self) -> None:
        before = dict(self.database.device_by_id("uppal-x990"))
        response = self._post("uppal-x990", ip="192.168.1.1", port="abc", comm_password="")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.text.count("Port must be a whole number between 1 and 65535"), 1
        )
        self.assertIn('value="abc"', response.text)
        after = self.database.device_by_id("uppal-x990")
        self.assertEqual((before["ip"], before["port"]), (after["ip"], after["port"]))

    def test_port_out_of_range_is_rejected(self) -> None:
        response = self._post("uppal-x990", ip="192.168.1.201", port="70000", comm_password="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Port must be a whole number between 1 and 65535", response.text)

    def test_blank_ip_is_rejected(self) -> None:
        response = self._post("uppal-x990", ip="   ", port="4370", comm_password="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("IP address is required", response.text)

    def test_non_numeric_comm_password_on_zk_device_is_rejected(self) -> None:
        response = self._post(
            "uppal-x990", ip="192.168.1.201", port="4370", comm_password="not-a-number"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count("comm password for Uppal must be a number"), 1)
        self.assertIsNone(self.database.device_by_id("uppal-x990")["comm_password"])

    def test_blank_comm_password_keeps_the_stored_value(self) -> None:
        self.database.device_by_id("gachibowli-c3-400")["comm_password"] = "secret"
        self._post("gachibowli-c3-400", ip="192.168.1.202", port="4370", comm_password="")
        self.assertEqual(self.database.device_by_id("gachibowli-c3-400")["comm_password"], "secret")

    def test_clear_checkbox_sets_null_and_env_fallback_applies(self) -> None:
        # Covers R6: clearing returns the device to the environment fallback.
        from essl_hub import config

        self.database.device_by_id("gachibowli-c3-400")["comm_password"] = "secret"
        response = self._post(
            "gachibowli-c3-400",
            ip="192.168.1.202",
            port="4370",
            comm_password="",
            clear_comm_password="1",
        )
        self.assertEqual(response.status_code, 302)
        stored = self.database.device_by_id("gachibowli-c3-400")["comm_password"]
        self.assertIsNone(stored)
        device = config.DeviceConfig(
            id="gachibowli-c3-400",
            site="Gachibowli",
            model="C3-400",
            ip="192.168.1.202",
            port=4370,
            protocol="c3-pullsdk",
            password_env="ESSL_C3_PASSWORD",
            password=stored,
        )
        with patch.dict(os.environ, {"ESSL_C3_PASSWORD": "envpw"}):
            self.assertEqual(config.password_for(device), "envpw")

    def test_unknown_device_id_is_404(self) -> None:
        response = self._post("ghost-device", ip="1.2.3.4", port="4370", comm_password="")
        self.assertEqual(response.status_code, 404)

    def test_missing_csrf_token_is_rejected_without_writing(self) -> None:
        before = dict(self.database.device_by_id("uppal-x990"))
        response = self.client.post(
            "/devices/uppal-x990", data={"ip": "192.168.5.5", "port": "5000"}
        )
        self.assertEqual(response.status_code, 400)
        after = self.database.device_by_id("uppal-x990")
        self.assertEqual((before["ip"], before["port"]), (after["ip"], after["port"]))

    def test_invalid_csrf_token_is_rejected_without_writing(self) -> None:
        response = self.client.post(
            "/devices/uppal-x990",
            data={"csrf": "bogus", "ip": "192.168.5.5", "port": "5000"},
        )
        self.assertEqual(response.status_code, 400)

    def test_non_admin_role_is_forbidden(self) -> None:
        self.client.post("/signout")
        self.database.upsert_employee(device_user_id="900", name="Priya HR", email="priya@example.com")
        priya = self.database.employee_by_email("priya@example.com")
        self.database.set_employee_role(priya["id"], "hr")
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "priya@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        response = self.client.post(
            "/devices/uppal-x990",
            data={"csrf": "x", "ip": "1.2.3.4", "port": "4370"},
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_device_post_redirects_to_signin(self) -> None:
        self.client.post("/signout")
        response = self.client.post(
            "/devices/uppal-x990", data={"ip": "1.2.3.4", "port": "4370"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/signin", response.headers["Location"])


class AdminDeviceLifecycleTest(AdminDeviceEditTest):
    """Covers R7, R8, R9: adding, retiring and re-enabling devices."""

    def _add(self, **data):
        data.setdefault("csrf", self._csrf())
        data.setdefault("site", "Kondapur")
        data.setdefault("family", "c3-400")
        data.setdefault("port", "4370")
        return self.client.post("/devices/add", data=data)

    def test_admin_adds_a_c3_at_a_new_ip_and_it_shows_pending(self) -> None:
        response = self._add(ip="192.168.1.210")
        self.assertEqual(response.status_code, 302)
        self.assertIn("added=kondapur-c3-400", response.headers["Location"])
        device = self.database.device_by_id("kondapur-c3-400")
        self.assertEqual(
            (device["site"], device["model"], device["protocol"], device["ip"], device["port"]),
            ("Kondapur", "C3-400", "c3-pullsdk", "192.168.1.210", 4370),
        )
        self.assertIsNone(device["retired_at"])
        page = self.client.get("/devices?added=kondapur-c3-400").text
        self.assertIn("Pending", page)
        self.assertIn("about 6 minutes", page)

    def test_duplicate_active_address_is_rejected_inline(self) -> None:
        before = set(self.database.devices_by_id)
        response = self._add(ip="192.168.1.201", site="Uppal Annex")
        self.assertEqual(response.status_code, 200)
        self.assertIn("192.168.1.201:4370 is already used by Uppal X990", response.text)
        self.assertIn('value="Uppal Annex"', response.text)
        self.assertEqual(set(self.database.devices_by_id), before)

    def test_edit_onto_another_active_address_is_rejected(self) -> None:
        response = self._post("uppal-x990", ip="192.168.1.202", port="4370", comm_password="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("already used by Gachibowli C3-400", response.text)
        self.assertEqual(self.database.device_by_id("uppal-x990")["ip"], "192.168.1.201")

    def test_address_of_a_retired_device_offers_reenable(self) -> None:
        self.database.set_device_retired("gachibowli-c3-400", True)
        before = set(self.database.devices_by_id)
        response = self._add(ip="192.168.1.202")
        self.assertEqual(response.status_code, 200)
        self.assertIn("retired device Gachibowli C3-400", response.text)
        self.assertIn("/devices/gachibowli-c3-400/enable", response.text)
        self.assertEqual(set(self.database.devices_by_id), before)

    def test_invalid_inputs_give_inline_errors(self) -> None:
        cases = [
            ({"ip": "999.1.1.1"}, "is not a valid IP address"),
            ({"ip": "192.168.1.210", "port": "0"}, "Port must be a whole number"),
            ({"ip": "192.168.1.210", "family": "x990", "site": "Miyapur",
              "comm_password": "abc"}, "comm password for Miyapur must be a number"),
            ({"ip": "192.168.1.210", "site": " "}, "Site name is required"),
            ({"ip": "192.168.1.210", "family": "bogus"}, "Choose a device family"),
        ]
        before = set(self.database.devices_by_id)
        for data, message in cases:
            with self.subTest(message=message):
                response = self._add(**data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, response.text)
        self.assertEqual(set(self.database.devices_by_id), before)

    def test_retire_then_reenable_keeps_punches(self) -> None:
        self.database.punches.append({"fingerprint": "f1", "device_id": "uppal-x990"})
        response = self.client.post("/devices/uppal-x990/retire", data={"csrf": self._csrf()})
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(self.database.device_by_id("uppal-x990")["retired_at"])
        page = self.client.get("/devices").text
        self.assertIn("Retired (1)", page)
        self.assertIn("Re-enable", page)
        response = self.client.post("/devices/uppal-x990/enable", data={"csrf": self._csrf()})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.database.device_by_id("uppal-x990")["retired_at"])
        self.assertEqual(len(self.database.punches), 1)

    def test_reenable_refused_when_address_now_taken(self) -> None:
        self.database.set_device_retired("gachibowli-c3-400", True)
        self._add(ip="192.168.1.202", site="Gachibowli New")
        # The retired match blocks the add; move the new device in by edit instead.
        self._add(ip="192.168.1.220", site="Gachibowli New")
        self._post("gachibowli-new-c3-400", ip="192.168.1.202", port="4370", comm_password="")
        response = self.client.post("/devices/gachibowli-c3-400/enable", data={"csrf": self._csrf()})
        self.assertEqual(response.status_code, 200)
        self.assertIn("already used by Gachibowli New", response.text)
        self.assertIsNotNone(self.database.device_by_id("gachibowli-c3-400")["retired_at"])

    def test_slug_collision_gets_a_suffix(self) -> None:
        self._add(ip="192.168.1.230", site="Uppal", family="x990")
        self.assertIsNotNone(self.database.device_by_id("uppal-x990-2"))
        self._add(ip="192.168.1.231", site="Uppal", family="x990")
        self.assertIsNotNone(self.database.device_by_id("uppal-x990-3"))

    def test_missing_csrf_is_refused_for_add_and_retire(self) -> None:
        before = set(self.database.devices_by_id)
        self.assertEqual(
            self.client.post("/devices/add", data={"site": "X", "family": "x990",
                                                   "ip": "10.0.0.1", "port": "4370"}).status_code,
            400,
        )
        self.assertEqual(self.client.post("/devices/uppal-x990/retire").status_code, 400)
        self.assertEqual(set(self.database.devices_by_id), before)
        self.assertIsNone(self.database.device_by_id("uppal-x990")["retired_at"])

    def test_hr_cannot_add_or_retire(self) -> None:
        self.client.post("/signout")
        self.database.upsert_employee(device_user_id="900", name="Priya HR", email="priya@example.com")
        priya = self.database.employee_by_email("priya@example.com")
        self.database.set_employee_role(priya["id"], "hr")
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "priya@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        response = self.client.post("/devices/add", data={
            "csrf": "x", "site": "X", "family": "x990", "ip": "10.0.0.1", "port": "4370"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/devices/uppal-x990/retire", data={"csrf": "x"})
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.database.device_by_id("uppal-x990")["retired_at"])


class AdminRosterUploadTest(unittest.TestCase):
    """Covers R1, R2, R18: .xlsx roster uploads and every per-file failure."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="1", name="Admin", email="admin@example.com")
        admin = self.database.employee_by_email("admin@example.com")
        self.database.set_employee_role(admin["id"], "admin")
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/settings").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _upload(self, data: bytes, filename: str):
        return self.client.post(
            "/people/import",
            data={"roster": (io.BytesIO(data), filename), "csrf": self._csrf()},
            content_type="multipart/form-data",
        )

    def _xlsx_bytes(self, rows) -> bytes:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        for row in rows:
            sheet.append(list(row))
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
        return buffer.getvalue()

    def test_valid_xlsx_upload_creates_employees(self) -> None:
        payload = self._xlsx_bytes(
            [
                ["name", "work email", "device user id"],
                ["Karthik", "karthik@example.com", 184],
                ["Vijay", "vijay@example.com", 55],
            ]
        )
        response = self._upload(payload, "roster.xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<strong>2</strong> added", response.text)
        self.assertIsNotNone(self.database.employee_by_email("karthik@example.com"))
        self.assertIsNotNone(self.database.employee_by_email("vijay@example.com"))

    def test_xls_extension_is_rejected_before_parsing(self) -> None:
        response = self._upload(b"\xd0\xcf\x11\xe0not-really-xls", "roster.xls")
        self.assertEqual(response.status_code, 400)
        self.assertIn(".xlsx", response.text)

    def test_unknown_extension_is_rejected_before_parsing(self) -> None:
        response = self._upload(b"name,email\nA,a@x.com\n", "roster.txt")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported file type", response.text)

    def test_binary_csv_upload_renders_roster_error_not_a_server_error(self) -> None:
        # Covers R2: a non-UTF-8 .csv must not become a 500.
        response = self._upload(b"\xff\xfe\x00\x01binary,payload\n", "roster.csv")
        self.assertEqual(response.status_code, 400)
        self.assertIn("not UTF-8 text", response.text)
        self.assertNotIn("Internal Server Error", response.text)

    def test_malformed_xlsx_upload_renders_roster_error(self) -> None:
        response = self._upload(b"PK\x03\x04 broken workbook", "roster.xlsx")
        self.assertEqual(response.status_code, 400)
        self.assertIn("xlsx", response.text)

    def test_upload_over_the_size_cap_renders_the_import_page_with_an_error(self) -> None:
        # Covers R2: the 5 MB cap must re-render the admin page, not a bare 413.
        oversized = b"a" * (6 * 1024 * 1024)
        response = self._upload(oversized, "roster.csv")
        self.assertEqual(response.status_code, 413)
        self.assertIn("5 MB", response.text)
        self.assertIn("Import roster", response.text)

    def test_anonymous_oversized_upload_keeps_the_bare_413_page(self) -> None:
        # Deliberate deviation from the U5 packet: the 413 handler must not
        # render the admin page (device IPs/ports, API-key prefixes, unmapped
        # IDs) to a client that never signed in.
        anonymous = self.app.test_client()
        oversized = b"a" * (6 * 1024 * 1024)
        response = anonymous.post(
            "/signin",
            data={"email": (io.BytesIO(oversized), "roster.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 413)
        self.assertNotIn("Devices, roster and access", response.text)
        self.assertNotIn("Edit connection settings", response.text)
        self.assertNotIn("API keys", response.text)


class ApiHoursTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com")
        self.client = self.app.test_client()

    def _csrf(self) -> str:
        html = self.client.get("/settings").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _create_key(self) -> str:
        from essl_hub import web as web_module

        with self.app.test_request_context():
            pass
        # Exercise the real admin flow to create a key, signed in as admin.
        self.database.upsert_employee(device_user_id="1", name="Admin", email="admin@example.com")
        admin = self.database.employee_by_email("admin@example.com")
        self.database.set_employee_role(admin["id"], "admin")
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        response = self.client.post("/settings/keys", data={"label": "HRMS", "csrf": self._csrf()})
        # Parse the reveal block specifically: the page holds other <code>
        # elements (the roster example row), so 'first <code>' is not the key.
        block = response.text.split('<div class="key-reveal">')[1]
        start = block.index("<code>") + len("<code>")
        end = block.index("</code>", start)
        return block[start:end].strip()

    def test_missing_bearer_header_is_refused(self) -> None:
        response = self.client.get("/api/v1/hours?from=2026-09-01&to=2026-09-01")
        self.assertEqual(response.status_code, 401)

    def test_covers_ae6_revoked_key_is_refused_second_key_still_works(self) -> None:
        # Covers AE6.
        key = self._create_key()
        self.client.post("/signout")
        response = self.client.get(
            "/api/v1/hours?from=2026-09-01&to=2026-09-01",
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual(response.status_code, 200)

        prefix = key[:12]
        # revoke as admin again
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        self.client.post(f"/settings/keys/{prefix}/revoke", data={"csrf": self._csrf()})
        self.client.post("/signout")

        revoked_response = self.client.get(
            "/api/v1/hours?from=2026-09-01&to=2026-09-01",
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual(revoked_response.status_code, 401)

    def test_hours_response_carries_day_start_and_uses_the_work_day(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        day = datetime(2026, 9, 1)
        self.database.insert_punches([
            _punch_row("fp-a1", day + timedelta(hours=22, minutes=10), "entry"),
            _punch_row("fp-a2", day + timedelta(days=1, hours=3, minutes=40), "exit"),
        ])
        response = self.client.get(
            "/api/v1/hours?from=2026-09-01&to=2026-09-02",
            headers={"Authorization": f"Bearer {key}"},
        )
        body = response.get_json()
        self.assertEqual(body["day_start"], "06:00")
        ravi = {r["date"]: r for r in body["results"] if r["device_user_id"] == "118"}
        self.assertEqual(ravi["2026-09-01"]["effective_minutes"], 330)
        self.assertEqual(ravi["2026-09-02"]["status"], "empty")

    def test_hours_reads_day_start_setting_once_per_request(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        original = self.database.setting
        calls = []

        def counting(name, *args, **kwargs):
            calls.append(name)
            return original(name, *args, **kwargs)

        with patch.object(self.database, "setting", side_effect=counting):
            response = self.client.get(
                "/api/v1/hours?from=2026-09-01&to=2026-09-07",
                headers={"Authorization": f"Bearer {key}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.get_json()["results"]), 7)
        self.assertEqual(calls.count("day_start_hour"), 1)

    def test_hours_response_carries_anomalies(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        day = datetime(2026, 9, 1)
        self.database.insert_punches([
            _punch_row("fp-o1", day + timedelta(hours=8), "exit"),
            _punch_row("fp-o2", day + timedelta(hours=9), "entry"),
            _punch_row("fp-o3", day + timedelta(hours=17), "exit"),
        ])
        response = self.client.get(
            "/api/v1/hours?from=2026-09-01&to=2026-09-01",
            headers={"Authorization": f"Bearer {key}"},
        )
        ravi = [r for r in response.get_json()["results"] if r["device_user_id"] == "118"][0]
        self.assertEqual(ravi["effective_minutes"], 480)
        self.assertEqual(ravi["anomalies"], ["Exit at 08:00 with no entry"])

    def test_hours_response_adds_exact_effective_seconds(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        day = datetime(2026, 9, 1)
        self.database.insert_punches([
            _punch_row("fp-x1", day + timedelta(hours=9, seconds=10), "entry"),
            _punch_row("fp-x2", day + timedelta(hours=16, minutes=59, seconds=40), "exit"),
        ])
        response = self.client.get(
            "/api/v1/hours?from=2026-09-01&to=2026-09-01",
            headers={"Authorization": f"Bearer {key}"},
        )
        ravi = [r for r in response.get_json()["results"] if r["device_user_id"] == "118"][0]
        self.assertEqual(ravi["effective_seconds"], 7 * 3600 + 59 * 60 + 30)
        self.assertEqual(ravi["effective_minutes"], 479)
        self.assertEqual(ravi["status"], "short")

    def test_range_longer_than_cap_is_refused(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        response = self.client.get(
            "/api/v1/hours?from=2026-01-01&to=2026-12-31",
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual(response.status_code, 400)

    def test_no_route_accepts_a_write_method(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        response = self.client.delete(
            "/api/v1/hours?from=2026-09-01&to=2026-09-01",
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual(response.status_code, 405)


class SetupPageTest(unittest.TestCase):
    TOKEN = "setup-token-1234567890"

    def _make_app_with(self, database, token):
        environment = {"ESSL_SETUP_TOKEN": token} if token else {}
        with patch.dict(os.environ, environment, clear=True):
            app = create_app(database=database)
        app.testing = True
        return app

    @staticmethod
    def _seed(database):
        database.upsert_employee(device_user_id="1", name="Admin", email="admin@example.com")
        admin = database.employee_by_email("admin@example.com")
        database.set_employee_role(admin["id"], "admin")

    def test_turnstile_failure_blocks_setup_even_with_right_token(self) -> None:
        database = FakeDatabase()
        environment = {
            "ESSL_SETUP_TOKEN": self.TOKEN,
            "TURNSTILE_SITE_KEY": "s",
            "TURNSTILE_SECRET_KEY": "k",
        }
        with patch.dict(os.environ, environment, clear=True):
            app = create_app(database=database)
        app.testing = True
        with patch("essl_hub.turnstile.verify", return_value=False):
            response = app.test_client().post(
                "/setup",
                data={"setup_token": self.TOKEN, "name": "Boss", "email": "boss@example.com"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(database.has_admin())

    def test_route_is_absent_without_a_setup_token(self) -> None:
        app = self._make_app_with(FakeDatabase(), None)
        self.assertEqual(app.test_client().get("/setup").status_code, 404)

    def test_route_is_absent_once_an_admin_exists(self) -> None:
        database = FakeDatabase()
        self._seed(database)
        app = self._make_app_with(database, self.TOKEN)
        client = app.test_client()
        self.assertEqual(client.get("/setup").status_code, 404)
        self.assertEqual(
            client.post(
                "/setup",
                data={
                    "setup_token": self.TOKEN,
                    "name": "Boss",
                    "email": "boss@example.com",
                },
            ).status_code,
            404,
        )

    def test_get_renders_the_form_on_an_empty_database(self) -> None:
        app = self._make_app_with(FakeDatabase(), self.TOKEN)
        response = app.test_client().get("/setup")
        self.assertEqual(response.status_code, 200)
        self.assertIn("First-time setup", response.text)
        _assert_auth_layout(self, response.text, "First-time setup")
        self.assertIn('name="setup_token"', response.text)

    def test_wrong_token_is_refused_and_creates_nobody(self) -> None:
        database = FakeDatabase()
        app = self._make_app_with(database, self.TOKEN)
        response = app.test_client().post(
            "/setup",
            data={"setup_token": "not-the-token", "name": "Boss", "email": "boss@example.com"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(database.employee_by_email("boss@example.com"))

    def test_missing_name_is_reported_without_a_write(self) -> None:
        database = FakeDatabase()
        app = self._make_app_with(database, self.TOKEN)
        response = app.test_client().post(
            "/setup",
            data={"setup_token": self.TOKEN, "name": "", "email": "boss@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Enter your name", response.text)
        self.assertIsNone(database.employee_by_email("boss@example.com"))

    def test_device_id_clash_is_reported(self) -> None:
        database = FakeDatabase()
        database.upsert_employee(device_user_id="5", name="Other", email="other@example.com")
        app = self._make_app_with(database, self.TOKEN)
        response = app.test_client().post(
            "/setup",
            data={
                "setup_token": self.TOKEN,
                "name": "Boss",
                "email": "boss@example.com",
                "device_user_id": "5",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("already belongs to", response.text)
        self.assertIsNone(database.employee_by_email("boss@example.com"))

    def test_creates_the_first_admin_then_closes_itself(self) -> None:
        database = FakeDatabase()
        app = self._make_app_with(database, self.TOKEN)
        client = app.test_client()
        response = client.post(
            "/setup",
            data={"setup_token": self.TOKEN, "name": "Boss", "email": "boss@example.com"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/signin", response.headers["Location"])
        row = database.employee_by_email("boss@example.com")
        self.assertEqual(row["role"], "admin")
        self.assertEqual(row["device_user_id"], "boss@example.com")
        self.assertEqual(client.get("/setup").status_code, 404)

    def test_promotes_an_existing_employee_without_duplicating(self) -> None:
        database = FakeDatabase()
        database.upsert_employee(device_user_id="7", name="Boss", email="boss@example.com")
        app = self._make_app_with(database, self.TOKEN)
        app.test_client().post(
            "/setup",
            data={"setup_token": self.TOKEN, "name": "Boss", "email": "BOSS@example.com"},
        )
        rows = database.employees()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "admin")
        self.assertEqual(rows[0]["device_user_id"], "7")

    def test_the_created_admin_can_sign_in_and_reach_the_console(self) -> None:
        database = FakeDatabase()
        app = self._make_app_with(database, self.TOKEN)
        client = app.test_client()
        client.post(
            "/setup",
            data={"setup_token": self.TOKEN, "name": "Boss", "email": "boss@example.com"},
        )

        captured: dict = {}
        from essl_hub import mail as mail_module

        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        client.post("/signin", data={"email": "boss@example.com"})
        client.post("/signin/verify", data={"code": captured["code"]})

        self.assertEqual(client.get("/settings").status_code, 200)


class BackupsRemovedTest(unittest.TestCase):
    """U4: manual backups, the replication sidecar and restore tooling are gone."""

    def setUp(self) -> None:
        from essl_hub import mail as mail_module

        self.app, self.database = _make_app()
        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _client_as(self, role: str):
        client = self.app.test_client()
        email = f"{role}@example.com"
        self.database.upsert_employee(device_user_id=f"9{len(role)}", name=role.title(), email=email)
        person = self.database.employee_by_email(email)
        self.database.set_employee_role(person["id"], role)
        client.post("/signin", data={"email": email})
        client.post("/signin/verify", data={"code": self.captured["code"]})
        return client

    def test_backups_route_is_404_for_admin_and_hr(self) -> None:
        for role in ("admin", "hr"):
            with self.subTest(role=role):
                client = self._client_as(role)
                self.assertEqual(client.get("/backups").status_code, 404)
                self.assertEqual(client.post("/backups", data={}).status_code, 404)

    def test_no_nav_mentions_backups(self) -> None:
        for role in ("employee", "manager", "hr", "admin"):
            with self.subTest(role=role):
                html = self._client_as(role).get("/week").text
                self.assertNotIn("Backups", html)
                self.assertNotIn("backup-badge", html)

    def test_backup_modules_are_gone(self) -> None:
        import importlib

        for name in ("essl_hub.backup", "essl_hub.restore_tool"):
            with self.subTest(module=name):
                with self.assertRaises(ModuleNotFoundError):
                    importlib.import_module(name)


class AppShellTest(unittest.TestCase):
    """U10: CSRF via header for htmx, and the role-aware sidebar."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _client_as(self, role: str):
        email = f"{role}@example.com"
        self.database.upsert_employee(device_user_id=role, name=role.title(), email=email)
        self.database.set_employee_role(self.database.employee_by_email(email)["id"], role)
        client = self.app.test_client()
        # A distinct address per role keeps the per-IP sign-in burst limit out of the way.
        client.environ_base["REMOTE_ADDR"] = f"10.0.{len(role)}.{sum(map(ord, role)) % 250 + 1}"
        client.post("/signin", data={"email": email})
        client.post("/signin/verify", data={"code": self.captured["code"]})
        return client

    @staticmethod
    def _csrf_meta(html: str) -> str:
        marker = '<meta name="csrf-token" content="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    @staticmethod
    def _sidebar_labels(html: str) -> list[str]:
        start = html.index('<nav aria-label="Main">')
        nav = html[start : html.index("</nav>", start)]
        return re.findall(r"<span>([^<]+)</span>(?:<span class=\"nav-count\"[^>]*>[^<]*</span>)?</a>", nav)

    def test_header_only_csrf_post_passes(self) -> None:
        client = self._client_as("admin")
        token = self._csrf_meta(client.get("/week").text)
        self.assertTrue(token)
        response = client.post(
            "/settings/target",
            data={"hours": "7", "minutes": "15"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.database.setting("daily_target_minutes"), "435")

    def test_post_without_header_or_field_is_rejected(self) -> None:
        client = self._client_as("admin")
        before = self.database.setting("daily_target_minutes")
        response = client.post("/settings/target", data={"hours": "7", "minutes": "15"})
        self.assertEqual(response.status_code, 400)
        response = client.post(
            "/settings/target",
            data={"hours": "7", "minutes": "15"},
            headers={"X-CSRF-Token": "bogus"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.database.setting("daily_target_minutes"), before)

    def test_shell_carries_csrf_for_htmx(self) -> None:
        html = self._client_as("employee").get("/week").text
        token = self._csrf_meta(html)
        self.assertIn(f'hx-headers=\'{{"X-CSRF-Token": "{token}"}}\'', html)
        self.assertIn('href="#main-content"', html)
        self.assertIn('<main class="main-content" id="main-content"', html)

    def test_sidebar_follows_navigation_by_role(self) -> None:
        # Entries whose pages do not exist yet (Team, Devices,
        # Settings) are left out; the admin console stands in for Settings.
        # The manager role arrives with U5.
        expected = {
            "employee": ["Today", "Week"],
            "hr": ["Today", "Week", "Everyone", "People"],
            "admin": ["Today", "Week", "Everyone", "People", "Devices", "Settings"],
        }
        for role, labels in expected.items():
            with self.subTest(role=role):
                html = self._client_as(role).get("/week").text
                self.assertEqual(self._sidebar_labels(html), labels)
                self.assertIn('aria-current="page"', html)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class BrandingRemovedTest(unittest.TestCase):
    """U2: the admin sign-in branding feature is gone; stale settings are ignored."""

    setUp = AdminViewTest.setUp
    _csrf = AdminViewTest._csrf

    def test_settings_page_has_no_branding_section(self) -> None:
        html = self.client.get("/settings").text
        self.assertNotIn("Sign-in branding", html)
        self.assertNotIn("/settings/branding", html)

    def test_branding_routes_are_gone(self) -> None:
        csrf = self._csrf()
        for path in ("/settings/branding/logo", "/settings/branding/logo/reset", "/settings/branding-background"):
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    data={"image": (io.BytesIO(PNG_BYTES), "logo.png"), "mode": "color",
                          "color": "#112233", "csrf": csrf},
                    content_type="multipart/form-data",
                )
                self.assertIn(response.status_code, (404, 405))
        self.assertEqual(self.client.get("/brand/logo").status_code, 404)

    def test_stale_branding_settings_do_not_affect_signin(self) -> None:
        self.database.set_setting("signin_background", "image")
        self.database.set_setting("signin_background_color", "#112233")
        page = self.app.test_client().get("/signin")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("auth-bg", page.text)
        self.assertNotIn('"/brand/', page.text)
        self.assertNotIn("#112233", page.text)


class OfflineTest(unittest.TestCase):
    """U9 / KTD11: reads survive a Turso outage, writes get the 503 offline page."""

    # Signed in as admin, like AdminViewTest, without re-running its tests.
    setUp = AdminViewTest.setUp
    _csrf = AdminViewTest._csrf
    _add_employee = AdminViewTest._add_employee

    def test_write_while_offline_returns_503_offline_page(self) -> None:
        employee = self._add_employee()
        csrf = self._csrf()
        self.database.offline = True
        with self.assertLogs("essl_hub.web", level="WARNING"):
            response = self.client.post(
                f"/people/{employee['id']}/role", data={"role": "hr", "csrf": csrf}
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("Temporarily offline", response.text)
        self.assertIn("Viewing attendance still works", response.text)
        self.assertEqual(self.database.employee_by_id(employee["id"])["role"], "employee")

    def test_read_while_offline_still_returns_200(self) -> None:
        self.database.offline = True
        self.assertEqual(self.client.get("/settings").status_code, 200)

    def test_signin_code_request_while_offline_shows_offline_page_and_sends_nothing(self) -> None:
        self.database.offline = True
        self.captured.clear()
        response = self.app.test_client().post("/signin", data={"email": "admin@example.com"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("Temporarily offline", response.text)
        _assert_auth_layout(self, response.text, "Temporarily offline")
        self.assertEqual(self.captured, {})


class StartupTest(unittest.TestCase):
    """U9 / KTD3, KTD4, KTD8: boot steps that need the primary never block serving."""

    def test_offline_boot_with_bootstrap_admin_logs_and_serves(self) -> None:
        database = FakeDatabase()
        database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        database.offline = True
        with patch.dict(os.environ, {"ESSL_BOOTSTRAP_ADMIN_EMAIL": "boss@example.com"}):
            with self.assertLogs("essl_hub.web", level="WARNING") as logs:
                app = create_app(database=database)
        self.assertTrue(any("bootstrap admin" in line for line in logs.output))
        self.assertTrue(any("schema migration" in line for line in logs.output))
        self.assertEqual(database.employee_by_email("boss@example.com")["role"], "employee")
        self.assertEqual(app.test_client().get("/signin").status_code, 200)

    def test_startup_syncs_before_migrating(self) -> None:
        database = FakeDatabase()
        create_app(database=database)
        self.assertEqual(database.sync_calls, 1)
        self.assertTrue(database.devices_by_id)

    def test_sync_loop_starts_only_in_replica_mode(self) -> None:
        local = FakeDatabase()
        create_app(database=local)
        self.assertIsNone(local.sync_loop_interval)

        replica = FakeDatabase()
        replica.replica = True
        with patch.dict(os.environ, {"TURSO_SYNC_SECONDS": ""}):
            create_app(database=replica)
        self.assertEqual(replica.sync_loop_interval, 60)


if __name__ == "__main__":
    unittest.main()


class RolesAndVisibilityTest(unittest.TestCase):
    """U5: four roles, manager landing, per-employee 404s, role-route rules."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _person(self, device_user_id: str, role: str = "employee") -> dict:
        email = f"{device_user_id}@example.com"
        self.database.upsert_employee(device_user_id=device_user_id, name=f"P{device_user_id}", email=email)
        person = self.database.employee_by_email(email)
        person["role"] = role
        return person

    def _sign_in(self, person: dict):
        self.client.post("/signin", data={"email": person["email"]})
        return self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def test_manager_signs_in_and_lands_without_error(self) -> None:
        boss = self._person("10", "manager")
        response = self._sign_in(boss)
        self.assertEqual(response.status_code, 302)
        landing = self.client.get(response.headers["Location"])
        self.assertEqual(landing.status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_employee_asking_for_someone_else_gets_404_like_a_missing_id(self) -> None:
        me = self._person("11")
        other = self._person("12")
        self._sign_in(me)
        for path in ("/day", "/week"):
            self.assertEqual(self.client.get(f"{path}?employee={other['id']}").status_code, 404)
            self.assertEqual(self.client.get(f"{path}?employee=99999").status_code, 404)
            self.assertEqual(self.client.get(f"{path}?employee=abc").status_code, 404)
            self.assertEqual(self.client.get(f"{path}?employee={me['id']}").status_code, 200)

    def test_manager_sees_report_but_not_their_own_manager(self) -> None:
        top = self._person("20", "manager")
        mid = self._person("21", "manager")
        low = self._person("22")
        mid["manager_id"] = top["id"]
        low["manager_id"] = mid["id"]
        self._sign_in(mid)
        self.assertEqual(self.client.get(f"/day?employee={low['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/day?employee={top['id']}").status_code, 404)

    def _team(self):
        """A (manager) -> B -> C chain plus an unrelated manager D."""
        a = self._person("40", "manager")
        b = self._person("41", "manager")
        c = self._person("42")
        d = self._person("43", "manager")
        b["manager_id"] = a["id"]
        c["manager_id"] = b["id"]
        return a, b, c, d

    def test_manager_opens_an_indirect_reports_week_with_their_name(self) -> None:
        a, _, c, _ = self._team()
        self._sign_in(a)
        page = self.client.get(f"/week?employee={c['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn('class="subject-name">P42<', page.text)
        self.assertIn('href="/team"', page.text)
        self.assertIn(f"/week?employee={c['id']}&amp;offset=-1", page.text)
        day = self.client.get(f"/day?employee={c['id']}&date=2026-10-01")
        self.assertIn(f"/day?employee={c['id']}&amp;date=2026-09-30", day.text)

    def test_own_day_has_no_person_header(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        self.assertNotIn("subject-name", self.client.get("/day").text)

    def test_other_manager_gets_404_for_someone_outside_their_team(self) -> None:
        a, _, _, d = self._team()
        self._sign_in(d)
        self.assertEqual(self.client.get(f"/day?employee={a['id']}").status_code, 404)

    def test_hr_opens_anyone_and_unknown_id_is_404(self) -> None:
        _, _, c, _ = self._team()
        hr = self._person("44", "hr")
        self._sign_in(hr)
        self.assertEqual(self.client.get(f"/day?employee={c['id']}").status_code, 200)
        self.assertEqual(self.client.get("/week?employee=99999").status_code, 404)

    def test_team_lists_only_the_managers_subtree_labelled_team(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        page = self.client.get("/team")
        self.assertEqual(page.status_code, 200)
        self.assertIn("<h1>Team</h1>", page.text)
        for name in ("P40", "P41", "P42"):
            self.assertIn(f">{name}</a>", page.text)
        self.assertNotIn(">P43</a>", page.text)

    def test_team_is_everyone_for_hr_and_admin(self) -> None:
        self._team()
        for role, uid in (("hr", "45"), ("admin", "46")):
            person = self._person(uid, role)
            self._sign_in(person)
            page = self.client.get("/team")
            self.assertIn("<h1>Everyone</h1>", page.text)
            self.assertIn(">P43</a>", page.text)

    def test_team_search_filters_and_htmx_returns_the_results_region(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        page = self.client.get("/team?q=p42", headers={"HX-Request": "true"})
        self.assertNotIn("<html", page.text)
        self.assertIn(">P42</a>", page.text)
        self.assertNotIn(">P41</a>", page.text)
        self.assertIn('id="team-count"', page.text)

    def test_team_history_restore_gets_the_full_page(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        restore = self.client.get("/team", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
        self.assertIn("<html", restore.text)
        self.assertIn('class="sidebar"', restore.text)
        self.assertNotIn('hx-swap-oob', restore.text)
        partial = self.client.get("/team", headers={"HX-Request": "true"})
        self.assertNotIn("<html", partial.text)
        self.assertIn("HX-Request", restore.headers.get("Vary", ""))
        self.assertIn("HX-Request", partial.headers.get("Vary", ""))

    def test_team_table_has_a_colgroup_matching_its_header(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        for headers in ({}, {"HX-Request": "true"}):
            page = self.client.get("/team?q=p4", headers=headers)
            table = re.search(r'<table class="[^"]*team-table.*?</table>', page.text, re.S).group(0)
            colgroup = re.search(r"<colgroup>(.*?)</colgroup>", table, re.S)
            self.assertIsNotNone(colgroup, headers)
            head = re.search(r"<thead>(.*?)</thead>", table, re.S).group(1)
            self.assertEqual(colgroup.group(1).count("<col "), head.count("<th "), headers)

    def test_team_history_restore_keeps_full_page_with_colgroup(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        restore = self.client.get("/team", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
        self.assertIn("<html", restore.text)
        self.assertIn("<colgroup>", restore.text)
        self.assertIn("HX-Request", restore.headers.get("Vary", ""))

    def test_team_puts_incomplete_and_empty_days_first(self) -> None:
        a, b, c, _ = self._team()
        # The first minutes of today's work day (default start 06:00).
        start = datetime.combine(_ist_today(), datetime.min.time()) + timedelta(hours=6, minutes=1)
        # P40: a full closed day (met or short). P41: a lone exit (incomplete).
        punches = [
            ("40", start, "entry"), ("40", start + timedelta(minutes=5), "exit"),
            ("41", start + timedelta(minutes=2), "exit"),
        ]
        self.database.insert_punches(
            [
                {"fingerprint": f"t{i}", "device_user_id": uid, "event_timestamp": ts, "trusted_direction": d}
                for i, (uid, ts, d) in enumerate(punches)
            ]
        )
        self._sign_in(a)
        html = self.client.get("/team").text
        order = [html.index(f">{n}</a>") for n in ("P41", "P42", "P40")]
        self.assertEqual(order, sorted(order))

    def test_team_marks_flagged_day_needs_review(self) -> None:
        a, _, _, _ = self._team()
        start = datetime.combine(_ist_today(), datetime.min.time()) + timedelta(hours=6, minutes=1)
        self.database.insert_punches([
            {"fingerprint": f"r{i}", "device_user_id": "40", "event_timestamp": ts, "trusted_direction": d}
            for i, (ts, d) in enumerate([
                (start, "exit"), (start + timedelta(minutes=1), "entry"), (start + timedelta(minutes=5), "exit"),
            ])
        ])
        self._sign_in(a)
        html = self.client.get("/team").text
        self.assertIn("Needs review", html)
        self.assertIn("with no entry", html)

    def test_employee_on_team_lands_on_own_today(self) -> None:
        me = self._person("47")
        self._sign_in(me)
        response = self.client.get("/team")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/day"))

    def test_hr_redirects_to_team(self) -> None:
        hr = self._person("48", "hr")
        self._sign_in(hr)
        response = self.client.get("/hr")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/team"))

    def test_navigation_matches_the_role_table(self) -> None:
        expected = {
            "employee": ["Today", "Week"],
            "manager": ["Today", "Week", "Team"],
            "hr": ["Today", "Week", "Everyone", "People"],
        }
        for i, (role, labels) in enumerate(expected.items()):
            self._sign_in(self._person(f"5{i}", role))
            html = self.client.get("/day").text
            nav = html.split('<nav aria-label="Main">')[1].split("</nav>")[0]
            self.assertEqual(re.findall(r"<span>([^<]+)</span>", nav), labels)

    def _csv_names(self, url: str) -> list[str]:
        import csv as csv_module

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text[:200])
        return sorted({row["Name"] for row in csv_module.DictReader(io.StringIO(response.text.lstrip("\ufeff")))})

    def test_manager_export_is_scoped_to_their_subtree(self) -> None:
        a, b, c, d = self._team()
        self._sign_in(a)
        self.assertEqual(self._csv_names("/export.csv?from=2026-10-01&to=2026-10-02"), ["P40", "P41", "P42"])
        url = f"/export.csv?from=2026-10-01&to=2026-10-01&employee={c['id']}&employee={d['id']}"
        self.assertEqual(self._csv_names(url), ["P42"])

    def test_hr_export_has_everyone_and_matching_xlsx_rows(self) -> None:
        self._team()
        hr = self._person("44", "hr")
        self._sign_in(hr)
        query = "from=2026-10-01&to=2026-10-03"
        self.assertEqual(self._csv_names(f"/export.csv?{query}"), ["P40", "P41", "P42", "P43", "P44"])
        import csv as csv_module

        csv_rows = list(csv_module.reader(io.StringIO(self.client.get(f"/export.csv?{query}").text.lstrip("\ufeff"))))
        response = self.client.get(f"/export.xlsx?{query}")
        self.assertEqual(response.status_code, 200)
        workbook = openpyxl.load_workbook(io.BytesIO(response.data))
        self.assertEqual(workbook.sheetnames, ["Daily Detail", "Monthly Matrix", "Person Summary"])
        daily = list(workbook["Daily Detail"].iter_rows(min_row=3, values_only=True))
        self.assertEqual([list(r) for r in daily[:1]], [csv_rows[0]])
        self.assertEqual([r[1] for r in daily[1:]], [r[1] for r in csv_rows[1:]])
        self.assertEqual(len(daily), 1 + 5 * 3)
        self.assertIn("work day runs 06:00", workbook["Daily Detail"]["A1"].value)
        matrix = list(workbook["Monthly Matrix"].iter_rows(min_row=4, values_only=True))
        self.assertEqual(sorted(r[1] for r in matrix), ["P40", "P41", "P42", "P43", "P44"])

    def test_selected_scope_without_people_and_bad_ranges_are_400_inline(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        for url, message in (
            ("/export.xlsx?from=2026-10-05&to=2026-10-01", "after the To date"),
            ("/export.csv?from=2026-01-01&to=2026-06-01", "at most 93 days"),
            ("/export.xlsx?from=2026-10-01&to=2026-10-01&scope=selected", "Select at least one person"),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 400)
            self.assertIn(message, response.text)
            self.assertIn('class="inline-error"', response.text)

    def test_employee_cannot_export(self) -> None:
        self._sign_in(self._person("49"))
        self.assertEqual(self.client.get("/export.csv").status_code, 403)
        self.assertEqual(self.client.get("/export.xlsx").status_code, 403)

    def test_team_has_the_export_panel(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        html = self.client.get("/team").text
        self.assertIn('action="/export.xlsx"', html)
        self.assertIn('formaction="/export.csv"', html)
        self.assertIn("Download Excel", html)
        self.assertIn('form="export-form"', html)

    def _csrf(self) -> str:
        html = self.client.get("/day").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def test_hr_via_route_cannot_grant_admin_or_touch_an_admin(self) -> None:
        hr = self._person("30", "hr")
        target = self._person("31")
        admin = self._person("32", "admin")
        self._sign_in(hr)
        csrf = self._csrf()
        response = self.client.post(f"/people/{target['id']}/role", data={"role": "admin", "csrf": csrf})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(target["role"], "employee")
        response = self.client.post(f"/people/{admin['id']}/role", data={"role": "hr", "csrf": csrf})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(admin["role"], "admin")
        response = self.client.post(f"/people/{target['id']}/role", data={"role": "manager", "csrf": csrf})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(target["role"], "manager")


class PeopleImportTest(unittest.TestCase):
    """U6: roster import on /people/import for HR and admin."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _person(self, device_user_id: str, role: str = "employee") -> dict:
        email = f"{device_user_id}@example.com"
        self.database.upsert_employee(device_user_id=device_user_id, name=f"P{device_user_id}", email=email)
        person = self.database.employee_by_email(email)
        person["role"] = role
        return person

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/day").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _upload(self, body: bytes, filename: str = "roster.csv"):
        return self.client.post(
            "/people/import",
            data={"roster": (io.BytesIO(body), filename), "csrf": self._csrf()},
            content_type="multipart/form-data",
        )

    def test_employee_and_manager_cannot_upload_or_view(self) -> None:
        for role in ("employee", "manager"):
            with self.subTest(role=role):
                self._sign_in(self._person(f"9{len(role)}", role))
                self.assertEqual(self._upload(b"name,work email,device user id\r\n").status_code, 403)
                self.assertEqual(self.client.get("/people/import").status_code, 403)
                self.assertEqual(self.client.get("/people/import/template.csv").status_code, 403)

    def test_hr_uploads_with_managers_and_sees_only_issue_rows(self) -> None:
        self._sign_in(self._person("1", "hr"))
        body = (
            b"name,work email,device user id,manager email,role\r\n"
            b"Ravi,ravi@n.com,10,sita@n.com,\r\n"
            b"Sita,sita@n.com,11,,manager\r\n"
            b"Ghost,ghost@n.com,12,nobody@n.com,\r\n"
            b"Broken,broken,13,,\r\n"
        )
        response = self._upload(body)
        self.assertEqual(response.status_code, 200)
        ravi = self.database.employee_by_email("ravi@n.com")
        sita = self.database.employee_by_email("sita@n.com")
        self.assertEqual(ravi["manager_id"], sita["id"])
        self.assertEqual(sita["role"], "manager")
        self.assertIn("<strong>3</strong> added", response.text)
        self.assertIn("<strong>1</strong> with warnings", response.text)
        self.assertIn("<strong>1</strong> rejected", response.text)
        self.assertIn("manager not found", response.text)
        self.assertIn("Download issues (CSV)", response.text)
        results = response.text.split('id="result-heading"')[1].split("</table>")[0]
        self.assertNotIn(">Ravi<", results)

    def test_hr_cannot_change_an_admins_device_id_via_upload(self) -> None:
        self._person("2", "admin")
        self._sign_in(self._person("1", "hr"))
        response = self._upload(b"name,work email,device user id\r\nP2,2@example.com,77\r\n")
        self.assertIn("only an admin", response.text)
        self.assertEqual(self.database.employee_by_email("2@example.com")["device_user_id"], "2")

    def test_csv_template_uploaded_unchanged_reports_zero_rows(self) -> None:
        self._sign_in(self._person("1", "hr"))
        template = self.client.get("/people/import/template.csv").data
        response = self._upload(template)
        self.assertEqual(response.status_code, 200)
        self.assertIn("<strong>0</strong> added", response.text)
        self.assertIn("<strong>0</strong> rejected", response.text)


class PeopleManagementTest(unittest.TestCase):
    """U7: /people search, inline manager and role edits, add/edit forms.
    Runs on a real SQLite database so the guarded writes are exercised."""

    def setUp(self) -> None:
        from tempfile import TemporaryDirectory

        from essl_hub import mail as mail_module
        from essl_hub.db import Database

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Database(str(Path(tmp.name) / "essl.db"))
        self.app = create_app(database=self.database)
        self.app.testing = True
        self.client = self.app.test_client()
        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.hr = self._person("1", "Hema HR", "hr")
        self.admin = self._person("2", "Arun Admin", "admin")
        self.ram = self._person("3", "Ramesh", email="ramesh@n.com")
        self.sita = self._person("4", "Sita", email="sita.ram@n.com")
        self.ravi = self._person("5", "Ravi")

    def _person(self, device_user_id, name, role="employee", email=None) -> dict:
        email = email or f"{device_user_id}@n.com"
        self.database.upsert_employee(device_user_id=device_user_id, name=name, email=email)
        person = self.database.employee_by_email(email)
        if role != "employee":
            self.database.set_employee_role(person["id"], role)
        return self.database.employee_by_id(person["id"])

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/day").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _assign(self, person: dict, htmx: bool = True, **data):
        headers = {"HX-Request": "true", "X-CSRF-Token": self._csrf()} if htmx else {}
        if not htmx:
            data["csrf"] = self._csrf()
        return self.client.post(f"/people/{person['id']}/assign", data=data, headers=headers)

    def _edit_row(self, person: dict) -> str:
        return self.client.get(f"/people/{person['id']}/row?edit=1", headers={"HX-Request": "true"}).text

    def _seed(self, count: int) -> None:
        for n in range(count):
            self._person(f"S{n:04d}", f"Seeded Person {n:04d}")

    def test_manager_and_employee_get_403(self) -> None:
        manager = self._person("9", "Mona", "manager")
        for person in (manager, self.ravi):
            with self.subTest(role=person["role"]):
                self._sign_in(person)
                self.assertEqual(self.client.get("/people").status_code, 403)
                self.assertEqual(self.client.get("/people/new").status_code, 403)

    def test_search_matches_name_or_email_case_insensitively(self) -> None:
        self._sign_in(self.hr)
        page = self.client.get("/people?q=RAM").text
        self.assertIn("Ramesh", page)
        self.assertIn("Sita", page)  # email sita.ram@
        self.assertNotIn(f'id="person-{self.ravi["id"]}"', page)
        self.assertIn("2 people", page)
        everyone = self.client.get("/people").text
        self.assertIn("5 people", everyone)

    def test_htmx_search_returns_the_results_fragment_with_an_oob_count(self) -> None:
        self._sign_in(self.hr)
        fragment = self.client.get("/people?q=ravi", headers={"HX-Request": "true"}).text
        self.assertNotIn("<html", fragment)
        self.assertIn('hx-swap-oob="true"', fragment)
        self.assertIn("1 person", fragment)

    def test_hr_changes_a_manager_and_the_fragment_shows_it(self) -> None:
        self._sign_in(self.hr)
        response = self._assign(self.ravi, manager_id=str(self.sita["id"]), role="employee")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database.employee_by_id(self.ravi["id"])["manager_id"], self.sita["id"])
        self.assertTrue(response.text.lstrip().startswith(f'<tr id="person-{self.ravi["id"]}"'))
        # Save swaps the row back to text: the new manager, no select.
        self.assertIn("Sita", response.text)
        self.assertNotIn("<select", response.text)
        self.assertIn("autofocus", response.text)

    def test_cycle_returns_the_error_fragment_and_leaves_the_database_unchanged(self) -> None:
        self.database.set_employee_manager(self.ravi["id"], self.sita["id"])
        self._sign_in(self.hr)
        response = self._assign(self.sita, manager_id=str(self.ravi["id"]), role="manager")
        self.assertIn('role="alert"', response.text)
        self.assertIn(f'aria-describedby="assign-error-{self.sita["id"]}"', response.text)
        self.assertIn("reports to this person", response.text)
        sita = self.database.employee_by_id(self.sita["id"])
        self.assertIsNone(sita["manager_id"])
        self.assertEqual(sita["role"], "employee")

    def test_no_js_save_redirects_and_errors_render_the_page(self) -> None:
        self._sign_in(self.hr)
        ok = self._assign(self.ravi, htmx=False, manager_id="", role="manager")
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(self.database.employee_by_id(self.ravi["id"])["role"], "manager")
        refused = self._assign(self.ravi, htmx=False, manager_id="", role="admin")
        self.assertEqual(refused.status_code, 403)
        self.assertIn("Only an admin", refused.text)

    def test_hr_sees_no_admin_option_and_admin_rows_are_locked(self) -> None:
        self._sign_in(self.hr)
        page = self.client.get("/people").text
        admin_row = page.split(f'id="person-{self.admin["id"]}"')[1].split("</tr>")[0]
        self.assertIn("Admin only", admin_row)
        self.assertNotIn('<option value="admin"', self._edit_row(self.ravi))
        locked = self._edit_row(self.admin)
        self.assertNotIn('name="role"', locked)
        self.assertIn("Admin only", locked)
        self._sign_in(self.admin)
        self.assertIn('<option value="admin"', self._edit_row(self.ravi))

    def test_add_with_duplicate_email_shows_inline_error_and_creates_nothing(self) -> None:
        self._sign_in(self.hr)
        before = len(self.database.employees())
        response = self.client.post(
            "/people/new",
            data={"name": "Copy", "email": "RAMESH@n.com", "device_user_id": "77", "manager_id": "", "role": "employee", "csrf": self._csrf()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already uses this work email", response.text)
        self.assertIn('aria-invalid="true"', response.text)
        self.assertEqual(len(self.database.employees()), before)

    def test_add_then_assign_manager_and_role(self) -> None:
        self._sign_in(self.hr)
        response = self.client.post(
            "/people/new",
            data={"name": "Neha", "email": "neha@n.com", "device_user_id": "60", "manager_id": str(self.sita["id"]), "role": "manager", "csrf": self._csrf()},
        )
        self.assertEqual(response.status_code, 302)
        neha = self.database.employee_by_email("neha@n.com")
        self.assertEqual((neha["manager_id"], neha["role"]), (self.sita["id"], "manager"))

    def test_hr_edit_of_an_admin_locks_identity_and_rejects_forged_changes(self) -> None:
        self._sign_in(self.hr)
        form = self.client.get(f"/people/{self.admin['id']}/edit").text
        self.assertNotIn('id="f-email"', form)
        self.assertNotIn('id="f-device_user_id"', form)
        response = self.client.post(
            f"/people/{self.admin['id']}/edit",
            data={"name": "Arun Admin", "email": "evil@n.com", "device_user_id": "2", "manager_id": "", "role": "admin", "csrf": self._csrf()},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.employee_by_id(self.admin["id"])["email"], self.admin["email"])
        response = self.client.post(
            f"/people/{self.admin['id']}/edit",
            data={"name": "Arun Admin", "email": self.admin["email"], "device_user_id": "999", "manager_id": "", "role": "admin", "csrf": self._csrf()},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.employee_by_id(self.admin["id"])["device_user_id"], "2")

    def test_admin_can_edit_another_admins_identity(self) -> None:
        other = self._person("8", "Second Admin", "admin")
        self._sign_in(self.admin)
        response = self.client.post(
            f"/people/{other['id']}/edit",
            data={"name": "Second Admin", "email": "second@n.com", "device_user_id": "80", "manager_id": "", "role": "admin", "csrf": self._csrf()},
        )
        self.assertEqual(response.status_code, 302)
        updated = self.database.employee_by_id(other["id"])
        self.assertEqual((updated["email"], updated["device_user_id"]), ("second@n.com", "80"))

    def test_unknown_person_is_404(self) -> None:
        self._sign_in(self.hr)
        self.assertEqual(self.client.get("/people/9999/edit").status_code, 404)
        self.assertEqual(self._assign({"id": 9999}, manager_id="").status_code, 404)

    def test_people_page_with_150_people_has_no_per_row_select_and_stays_small(self) -> None:
        self._seed(145)
        self._sign_in(self.hr)
        response = self.client.get("/people")
        self.assertEqual(response.status_code, 200)
        self.assertIn("150 people", response.text)
        self.assertNotIn("<select", response.text)
        self.assertLess(len(response.data), 250 * 1024)
        self.assertIn('class="col-person"', response.text)

    def test_row_edit_opens_select_saves_and_cancels(self) -> None:
        self.database.set_employee_manager(self.ravi["id"], self.sita["id"])
        self._sign_in(self.hr)
        edit = self._edit_row(self.ravi)
        self.assertTrue(edit.lstrip().startswith(f'<tr id="person-{self.ravi["id"]}" class="is-editing"'))
        self.assertEqual(edit.count("<tr"), 1)
        self.assertIn(f'<option value="{self.sita["id"]}" selected>Sita</option>', edit)
        self.assertIn('name="manager_id"', edit)
        self.assertIn(">Cancel</a>", edit)
        cancel = self.client.get(f"/people/{self.ravi['id']}/row", headers={"HX-Request": "true"}).text
        self.assertNotIn("<select", cancel)
        self.assertIn("Sita", cancel)
        self.assertEqual(self.database.employee_by_id(self.ravi["id"])["manager_id"], self.sita["id"])
        saved = self._assign(self.ravi, manager_id=str(self.ram["id"]), role="employee")
        self.assertNotIn("<select", saved.text)
        self.assertIn("Ramesh", saved.text)
        self.assertEqual(self.database.employee_by_id(self.ravi["id"])["manager_id"], self.ram["id"])
        self.assertEqual(self.client.get("/people/9999/row").status_code, 404)

    def test_row_edit_is_hr_and_admin_only(self) -> None:
        manager = self._person("9", "Mona", "manager")
        self._sign_in(manager)
        self.assertEqual(self.client.get(f"/people/{self.ravi['id']}/row?edit=1").status_code, 403)

    def test_own_row_edit_keeps_role_locked(self) -> None:
        self._sign_in(self.hr)
        own = self._edit_row(self.hr)
        self.assertNotIn('name="role"', own)
        self.assertIn('name="manager_id"', own)

    def test_history_restore_gets_the_full_page_and_htmx_gets_the_partial(self) -> None:
        self._sign_in(self.hr)
        restore = self.client.get(
            "/people", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}
        )
        self.assertIn("<html", restore.text)
        self.assertIn('class="sidebar"', restore.text)
        partial = self.client.get("/people", headers={"HX-Request": "true"})
        self.assertNotIn("<html", partial.text)
        self.assertIn('hx-swap-oob="true"', partial.text)
        for response in (restore, partial):
            self.assertIn("HX-Request", response.headers.get("Vary", ""))
        full = self.client.get("/people").text
        self.assertIn('"historyCacheSize": 0', full)
        self.assertIn('"refreshOnHistoryMiss": true', full)
        self.assertNotIn('hx-swap-oob', full)

    def test_people_query_count_does_not_grow_with_roster_size(self) -> None:
        def counted(names):
            calls = {"n": 0}
            for name in names:
                original = getattr(self.database, name)

                def wrapper(*args, _original=original, **kwargs):
                    calls["n"] += 1
                    return _original(*args, **kwargs)

                setattr(self.database, name, wrapper)
            self.addCleanup(lambda: [self.database.__dict__.pop(n, None) for n in names])
            return calls

        self._sign_in(self.hr)
        self.client.get("/people")  # warm the nav issue-count cache
        names = ("employees", "punches_for_employee_range", "punches_for_device_users_range")
        calls = counted(names)
        self.client.get("/people")
        small = calls["n"]
        for n in names:
            self.database.__dict__.pop(n, None)
        self._seed(60)
        calls = counted(names)
        self.client.get("/people")
        self.assertEqual(calls["n"], small)
        self.assertLessEqual(small, 3)


class ScreensTest(unittest.TestCase):
    """U11: devices split, settings, Today and sign-in screens."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _client_as(self, role: str):
        email = f"{role}@example.com"
        self.database.upsert_employee(device_user_id=f"d-{role}", name=role.title(), email=email)
        self.database.set_employee_role(self.database.employee_by_email(email)["id"], role)
        client = self.app.test_client()
        client.environ_base["REMOTE_ADDR"] = f"10.9.{len(role)}.{sum(map(ord, role)) % 250 + 1}"
        client.post("/signin", data={"email": email})
        client.post("/signin/verify", data={"code": self.captured["code"]})
        return client

    def test_hr_cannot_open_devices_but_gets_id_issues(self) -> None:
        client = self._client_as("hr")
        self.assertEqual(client.get("/devices").status_code, 403)
        people = client.get("/people").text
        self.assertNotIn('href="/devices"', people)
        self.assertIn('href="/people/issues"', people)
        self.assertEqual(client.get("/people/issues").status_code, 200)

    def test_manager_cannot_open_id_issues(self) -> None:
        self.assertEqual(self._client_as("manager").get("/people/issues").status_code, 403)

    def test_admin_sees_the_edit_form(self) -> None:
        page = self._client_as("admin").get("/devices")
        self.assertIn("Edit connection", page.text)
        self.assertIn('action="/devices/uppal-x990"', page.text)

    def test_manager_and_employee_cannot_open_devices(self) -> None:
        for role in ("manager", "employee"):
            with self.subTest(role=role):
                self.assertEqual(self._client_as(role).get("/devices").status_code, 403)

    def test_hr_settings_is_forbidden(self) -> None:
        self.assertEqual(self._client_as("hr").get("/settings").status_code, 403)

    def test_stale_last_checked_shows_a_warning(self) -> None:
        client = self._client_as("admin")
        device = self.database.device_by_id("uppal-x990")
        device["status"] = "connected"
        with patch.dict(os.environ, {"ESSL_HEARTBEAT_WRITE_SECONDS": "300"}):
            device["last_attempt"] = _ist_now() - timedelta(minutes=4)
            fresh = client.get("/devices").text
            device["last_attempt"] = _ist_now() - timedelta(minutes=11)
            stale = client.get("/devices").text
        self.assertNotIn("Not checked recently", fresh)
        self.assertIn("Connected", fresh)
        self.assertIn("Not checked recently", stale)
        self.assertIn("11 min ago", stale)

    def test_day_without_punches_renders_the_empty_state_not_a_chart(self) -> None:
        page = self._client_as("employee").get("/day")
        self.assertIn("No punches yet today", page.text)
        self.assertNotIn('class="day-log"', page.text)
        self.assertNotIn("GROSS HOURS", page.text)

    def test_day_with_punches_renders_the_attendance_log(self) -> None:
        client = self._client_as("employee")
        day = _ist_today() - timedelta(days=1)
        start = datetime.combine(day, datetime.min.time())
        self.database.insert_punches(
            [
                {"fingerprint": "s-in", "device_user_id": "d-employee", "event_timestamp": start + timedelta(hours=9), "trusted_direction": "entry"},
                {"fingerprint": "s-out", "device_user_id": "d-employee", "event_timestamp": start + timedelta(hours=17, minutes=30), "trusted_direction": "exit"},
            ]
        )
        page = client.get(f"/day?date={day.isoformat()}").text
        self.assertIn('<ol class="day-log">', page)
        self.assertIn('log-time num">09:00:00</span><span class="log-label">In</span><span class="log-meta">Inside <span class="num" data-log-stretch="30600">8:30:00', page)
        self.assertIn('log-time num">17:30:00</span><span class="log-label">Out', page)
        self.assertIn('role="img" aria-label="8h 30m effective, 100% of the', page)
        self.assertNotIn("data-poll=", page)  # past days never poll

    def test_week_renders_bars_and_target_line(self) -> None:
        page = self._client_as("employee").get("/week").text
        self.assertIn('class="chart chart-week"', page)
        self.assertIn('class="wk-target"', page)

    def test_code_field_is_one_time_code_numeric(self) -> None:
        page = self.app.test_client().post("/signin", data={"email": "x@example.com"}).text
        self.assertIn('inputmode="numeric"', page)
        self.assertIn('autocomplete="one-time-code"', page)

    def test_expired_code_offers_a_new_code_inline(self) -> None:
        from essl_hub import auth as auth_module

        client = self.app.test_client()
        with patch.object(auth_module, "verify_code", return_value=auth_module.VerifyOutcome(ok=False, error="expired")):
            page = client.post("/signin/verify", data={"code": "123456"}).text
        self.assertIn("That code has expired", page)
        self.assertIn("Request a new code", page)
        self.assertIn('role="alert"', page)

    def test_wrong_code_keeps_the_form_with_an_inline_error(self) -> None:
        from essl_hub import auth as auth_module

        client = self.app.test_client()
        with patch.object(auth_module, "verify_code", return_value=auth_module.VerifyOutcome(ok=False, error="invalid")):
            page = client.post("/signin/verify", data={"code": "000000"}).text
        self.assertIn("That code is not correct", page)
        self.assertIn('aria-invalid="true"', page)
        self.assertIn('autocomplete="one-time-code"', page)


class SyncHealthTest(unittest.TestCase):
    """Boundaries of the health-state table (U5)."""

    NOW = datetime(2026, 10, 10, 12, 0, 0)

    def _health(self, web=timedelta(minutes=1), collector="ok", exports="ok", local=False):
        from essl_hub.routes.common import sync_health

        if collector == "ok":
            collector = self._collector()
        if exports == "ok":
            exports = self._exports(timedelta(hours=10))
        web_at = None if web is None else self.NOW - web
        return sync_health(web_at, collector, exports, self.NOW, local_mode=local)

    def _collector(self, age=timedelta(minutes=1), depth=0, oldest=None, dead=0):
        return {
            "last_poll": (self.NOW - age).isoformat(sep=" "),
            "spool_depth": depth,
            "spool_oldest_at": None if oldest is None else (self.NOW - oldest).isoformat(sep=" "),
            "dead_count": dead,
        }

    def _exports(self, age):
        at = (self.NOW - age).isoformat(sep=" ")
        return [{"status": "failed", "started": at, "finished": at},
                {"status": "ok", "started": at, "finished": at},
                {"status": "failed", "started": at, "finished": at}]

    def test_all_green(self) -> None:
        health = self._health()
        self.assertEqual((health["tone"], health["label"]), ("ok", "Turso sync healthy"))
        self.assertIn("IST", health["title"])

    def test_web_sync_boundaries(self) -> None:
        cases = [(timedelta(minutes=5), "ok"), (timedelta(minutes=5, seconds=1), "warn"),
                 (timedelta(minutes=60), "warn"), (timedelta(minutes=60, seconds=1), "bad"), (None, "bad")]
        for age, tone in cases:
            with self.subTest(age=age):
                health = self._health(web=age)
                self.assertEqual(health["tone"], tone)
                if tone != "ok":
                    self.assertEqual(health["signal"], "web")
                    self.assertIn("Web sync", health["label"])

    def test_collector_boundaries(self) -> None:
        cases = [
            (self._collector(age=timedelta(minutes=10)), "ok"),
            (self._collector(age=timedelta(minutes=11)), "bad"),
            (self._collector(depth=1, oldest=timedelta(minutes=59)), "warn"),
            (self._collector(depth=1, oldest=timedelta(minutes=61)), "bad"),
            (self._collector(dead=1), "bad"),
            (None, "bad"),
            ({"last_poll": "garbage", "spool_depth": "x"}, "bad"),
        ]
        for status, tone in cases:
            with self.subTest(status=status):
                health = self._health(collector=status)
                self.assertEqual(health["tone"], tone)
                if tone != "ok":
                    self.assertEqual(health["signal"], "collector")
                    self.assertIn("Collector", health["label"])

    def test_export_boundaries(self) -> None:
        cases = [(timedelta(days=2), "ok"), (timedelta(days=2.5), "warn"), (timedelta(days=6.5), "warn"),
                 (timedelta(days=7), "bad")]
        for age, tone in cases:
            with self.subTest(age=age):
                health = self._health(exports=self._exports(age))
                self.assertEqual(health["tone"], tone)
                if tone != "ok":
                    self.assertEqual(health["signal"], "export")
        for empty in ([], None, [{"status": "failed", "started": "2026-10-09 02:00:00"}]):
            health = self._health(exports=empty)
            self.assertEqual((health["tone"], health["label"]), ("bad", "No export yet"))

    def test_worst_signal_wins_and_is_named(self) -> None:
        health = self._health(web=timedelta(minutes=30), collector=self._collector(dead=2))
        self.assertEqual((health["tone"], health["signal"]), ("bad", "collector"))
        self.assertEqual(health["label"], "Collector: 2 dead punches")
        health = self._health(web=timedelta(minutes=30), exports=self._exports(timedelta(days=3)))
        self.assertEqual((health["tone"], health["signal"]), ("warn", "web"))

    def test_local_mode_is_neutral(self) -> None:
        health = self._health(web=None, collector=None, exports=None, local=True)
        self.assertEqual((health["tone"], health["label"]), ("neutral", "Local database"))


class BrandIconsTest(unittest.TestCase):
    """U1: the NITCO favicon set on every page."""

    setUp = HoursRenderingTest.setUp
    _sign_in = HoursRenderingTest._sign_in

    ICON_HREF = re.compile(r'<link rel="(?:icon|apple-touch-icon)"[^>]*href="([^"]+)"')

    def _icon_hrefs(self, html: str) -> list[str]:
        return self.ICON_HREF.findall(html)

    def test_signin_and_app_pages_link_existing_brand_icons(self) -> None:
        static_root = Path(self.app.static_folder)
        signin = self.client.get("/signin").get_data(as_text=True)
        self._sign_in()
        day = self.client.get("/day").get_data(as_text=True)
        for html in (signin, day):
            hrefs = self._icon_hrefs(html)
            self.assertGreaterEqual(len(hrefs), 5)
            for href in hrefs:
                self.assertTrue(href.startswith("/static/brand/"), href)
                self.assertTrue((static_root / href.removeprefix("/static/")).is_file(), href)

    def test_head_icons_template_paths_resolve_on_disk(self) -> None:
        source = Path(self.app.template_folder) / "_head_icons.html"
        names = re.findall(r"filename='([^']+)'", source.read_text())
        self.assertTrue(names)
        for name in names:
            self.assertTrue((Path(self.app.static_folder) / name).is_file(), name)

    def test_root_favicon_is_public_and_long_cached(self) -> None:
        response = self.app.test_client().get("/favicon.ico")
        self.assertEqual(response.status_code, 200)
        self.assertIn("icon", response.mimetype)
        cache = response.headers.get("Cache-Control", "")
        self.assertIn("max-age", cache)
        self.assertNotIn("no-store", cache)
        response.close()
