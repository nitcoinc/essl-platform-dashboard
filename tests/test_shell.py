"""App shell: sidebar sign-out, light/dark theme switch."""

from __future__ import annotations

import re
import unittest

from tests.test_web import _make_app


class ShellTest(unittest.TestCase):
    def setUp(self) -> None:
        from essl_hub import mail as mail_module

        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _sign_in_as(self, role: str) -> None:
        email = f"{role}@example.com"
        self.database.upsert_employee(device_user_id=f"9{len(role)}", name=role.title(), email=email)
        person = self.database.employee_by_email(email)
        self.database.set_employee_role(person["id"], role)
        self.client.post("/signin", data={"email": email})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _home(self) -> str:
        return self.client.get("/week").text

    def _csrf(self, html: str) -> str:
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    # theme -----------------------------------------------------------

    def test_default_theme_is_light(self) -> None:
        self.assertIn('data-theme="light"', self.client.get("/signin").text)
        self._sign_in_as("employee")
        self.assertIn('<html lang="en" data-theme="light">', self._home())

    def test_cookie_dark_renders_dark(self) -> None:
        self._sign_in_as("employee")
        self.client.set_cookie("theme", "dark")
        self.assertIn('<html lang="en" data-theme="dark">', self._home())
        self.client.set_cookie("theme", "bogus")
        self.assertIn('data-theme="light"', self._home())

    def test_auth_pages_honour_theme(self) -> None:
        self.client.set_cookie("theme", "system")
        self.assertIn('data-theme="system"', self.client.get("/signin").text)

    def test_theme_endpoint_sets_cookie_and_redirects_back(self) -> None:
        self._sign_in_as("employee")
        csrf = self._csrf(self._home())
        response = self.client.post(
            "/preferences/theme", data={"csrf": csrf, "theme": "dark", "next": "/week?d=1"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/week?d=1")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("theme=dark", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Max-Age=31536000", cookie)

    def test_theme_endpoint_rejects_external_redirect(self) -> None:
        self._sign_in_as("employee")
        csrf = self._csrf(self._home())
        for bad in ("https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)"):
            with self.subTest(next=bad):
                response = self.client.post(
                    "/preferences/theme", data={"csrf": csrf, "theme": "dark", "next": bad}
                )
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("evil", response.headers["Location"])

    def test_theme_endpoint_requires_csrf_and_valid_value(self) -> None:
        self._sign_in_as("employee")
        csrf = self._csrf(self._home())
        self.assertEqual(self.client.post("/preferences/theme", data={"theme": "dark"}).status_code, 400)
        self.assertEqual(
            self.client.post("/preferences/theme", data={"csrf": csrf, "theme": "pink"}).status_code, 400
        )

    # sign-out ----------------------------------------------------------

    def test_sign_out_in_sidebar_for_every_role_and_not_in_top_bar(self) -> None:
        for role in ("employee", "manager", "hr", "admin"):
            with self.subTest(role=role):
                self.setUp()  # fresh app: sign-in is rate limited per address
                self._sign_in_as(role)
                html = self._home()
                sidebar = html.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]
                self.assertIn("Sign out", sidebar)
                bar = html.split('<div class="shell-bar">', 1)[1].split("</div>\n", 1)[0]
                self.assertNotIn("Sign out", bar)
                self.assertNotIn("account-menu", html)


if __name__ == "__main__":
    unittest.main()


class SyncHealthShellTest(ShellTest):
    """Admin-only Turso sync health indicator and /health page (U5)."""

    def setUp(self) -> None:
        import json
        import os
        import tempfile
        from unittest.mock import patch

        from essl_hub.routes.common import _now_naive

        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.collector_path = os.path.join(tmp.name, "collector-status.json")
        self.export_path = os.path.join(tmp.name, "export-status.json")
        env = patch.dict(os.environ, {"ESSL_COLLECTOR_STATUS_PATH": self.collector_path,
                                      "ESSL_EXPORT_STATUS_PATH": self.export_path})
        env.start()
        self.addCleanup(env.stop)
        now = _now_naive().replace(microsecond=0)
        self.now = now
        self.database.replica = True
        self.database.last_sync_ok_at = now
        with open(self.collector_path, "w") as f:
            json.dump({"last_poll": now.isoformat(sep=" "), "spool_depth": 0, "dead_count": 0}, f)
        with open(self.export_path, "w") as f:
            json.dump([{"status": "ok", "started": now.isoformat(sep=" "), "finished": now.isoformat(sep=" "),
                        "key": f"exports/run-{i}.db.gz", "size": 2048} for i in range(20)], f)

    def test_admin_sees_green_indicator_with_icon_text_and_ist_title(self) -> None:
        self._sign_in_as("admin")
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                self.client.set_cookie("theme", theme)
                html = self._home()
                self.assertIn(f'data-theme="{theme}"', html)
                badge = re.search(r'<a class="sync-badge tone-ok" href="/health" title="([^"]+)"[^>]*>(.*?)</a>', html)
                self.assertIsNotNone(badge)
                self.assertIn("IST", badge.group(1))
                self.assertIn("#i-check", badge.group(2))
                self.assertIn("Turso sync healthy", badge.group(2))

    def test_other_roles_see_no_indicator_and_health_404s(self) -> None:
        for role in ("employee", "manager", "hr"):
            with self.subTest(role=role):
                self.client.delete_cookie("es_session")
                self._sign_in_as(role)
                self.assertNotIn("sync-badge", self._home())
                self.assertEqual(self.client.get("/health").status_code, 404)

    def test_health_page_lists_signals_and_last_14_exports(self) -> None:
        self._sign_in_as("admin")
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        html = response.text
        for name in ("Web replica sync", "Collector", "Nightly export", "Recovery", "IST"):
            self.assertIn(name, html)
        self.assertIn("run-19.db.gz", html)
        self.assertIn("run-6.db.gz", html)
        self.assertNotIn("run-5.db.gz", html)

    def test_corrupt_status_files_go_red_without_crashing(self) -> None:
        for path in (self.collector_path, self.export_path):
            with open(path, "w") as f:
                f.write("{not json")
        self._sign_in_as("admin")
        self.assertIn('class="sync-badge tone-bad"', self._home())
        self.assertIn("Collector status unknown", self._home())
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_missing_collector_status_goes_red(self) -> None:
        import os

        os.unlink(self.collector_path)
        self._sign_in_as("admin")
        self.assertIn('class="sync-badge tone-bad"', self._home())

    def test_local_mode_shows_neutral_chip(self) -> None:
        self.database.replica = False
        self._sign_in_as("admin")
        html = self._home()
        self.assertIn('class="sync-badge tone-neutral"', html)
        self.assertIn("Local database", html)
        self.assertIn("Local database", self.client.get("/health").text)
