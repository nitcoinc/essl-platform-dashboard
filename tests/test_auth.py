from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from essl_hub import auth, config
from essl_hub.mail import MailError


class FakeDatabase:
    def __init__(self) -> None:
        self.employees_by_email: dict[str, dict] = {}
        self.login_codes: list[dict] = []
        self._next_login_code_id = 1
        self.sessions: dict[str, dict] = {}
        self._next_employee_id = 1
        # Fixed "now" for session expiry; None falls back to the real clock.
        self.now: datetime | None = None

    def add_employee(self, email: str, *, role: str = "employee") -> dict:
        employee = {"id": self._next_employee_id, "email": email, "name": email, "role": role}
        self._next_employee_id += 1
        self.employees_by_email[email.lower()] = employee
        return employee

    def employee_by_email(self, email: str):
        return self.employees_by_email.get(email.strip().lower())

    def count_recent_login_codes(self, *, email: str, since: datetime) -> int:
        return sum(
            1
            for row in self.login_codes
            if row["email"].lower() == email.lower() and row["created_at"] >= since
        )

    def count_recent_login_codes_by_ip(self, *, ip: str, since: datetime) -> int:
        return sum(
            1 for row in self.login_codes if row["requested_ip"] == ip and row["created_at"] >= since
        )

    def create_login_code(self, *, email, code_hash, pending_token_hash, requested_ip, expires_at):
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
                "created_at": datetime.now(),
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
                row["consumed_at"] = datetime.now()

    def create_session(self, *, employee_id, token_hash, csrf_token, expires_at):
        self.sessions[token_hash] = {
            "employee_id": employee_id,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
            "revoked": False,
        }

    def session_employee(self, token_hash: str):
        session = self.sessions.get(token_hash)
        if session is None or session["revoked"] or session["expires_at"] < (self.now or datetime.now()):
            return None
        employee = next(e for e in self.employees_by_email.values() if e["id"] == session["employee_id"])
        return {**employee, "csrf_token": session["csrf_token"]}

    def revoke_session(self, token_hash: str) -> None:
        if token_hash in self.sessions:
            self.sessions[token_hash]["revoked"] = True


class RequestCodeTest(unittest.TestCase):
    def test_ae4_response_identical_for_unknown_email_and_sends_nothing(self) -> None:
        # Covers AE4.
        database = FakeDatabase()
        sent = []
        result_known = auth.request_code(
            database, "ghost@example.com", mailer=lambda e, c: sent.append((e, c))
        )
        database.add_employee("ravi.kumar@example.com")
        result_roster = auth.request_code(
            database, "ravi.kumar@example.com", mailer=lambda e, c: sent.append((e, c))
        )
        self.assertTrue(result_known.pending_token)
        self.assertTrue(result_roster.pending_token)
        self.assertEqual(sent, [("ravi.kumar@example.com", sent[0][1])])

    def test_exceeding_per_email_hourly_limit_sends_nothing(self) -> None:
        database = FakeDatabase()
        database.add_employee("ravi.kumar@example.com")
        sent = []
        now = datetime(2026, 9, 10, 9, 0, 0)
        for _ in range(auth.PER_EMAIL_HOURLY_LIMIT):
            auth.request_code(
                database, "ravi.kumar@example.com", now=now, mailer=lambda e, c: sent.append((e, c))
            )
        auth.request_code(
            database, "ravi.kumar@example.com", now=now, mailer=lambda e, c: sent.append((e, c))
        )
        self.assertEqual(len(sent), auth.PER_EMAIL_HOURLY_LIMIT)

    def test_mailer_failure_is_logged_not_reported_to_the_caller(self) -> None:
        # Covers R11: a send failure must not change the response, since that
        # would itself reveal the email is on the roster.
        database = FakeDatabase()
        database.add_employee("ravi.kumar@example.com")

        def failing_mailer(email, code):
            raise MailError("Resend is down")

        result = auth.request_code(database, "ravi.kumar@example.com", mailer=failing_mailer)
        self.assertTrue(result.pending_token)


class VerifyCodeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = FakeDatabase()
        self.employee = self.database.add_employee("ravi.kumar@example.com")
        self.sent_codes: list[str] = []
        self.now = datetime(2026, 9, 10, 9, 0, 0)
        self.database.now = self.now
        result = auth.request_code(
            self.database,
            "ravi.kumar@example.com",
            now=self.now,
            mailer=lambda e, c: self.sent_codes.append(c),
        )
        self.pending_token = result.pending_token
        self.code = self.sent_codes[0]

    def test_correct_code_with_matching_cookie_creates_session(self) -> None:
        outcome = auth.verify_code(
            self.database, pending_token=self.pending_token, code=self.code, now=self.now
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.employee["email"], "ravi.kumar@example.com")
        resolved = auth.resolve_session(self.database, outcome.session_token)
        self.assertEqual(resolved["employee"]["email"], "ravi.kumar@example.com")
        self.assertTrue(resolved["csrf_token"])
        self.assertEqual(resolved["csrf_token"], outcome.csrf_token)

    def test_correct_code_without_matching_cookie_is_refused(self) -> None:
        outcome = auth.verify_code(
            self.database, pending_token="not-the-real-token", code=self.code, now=self.now
        )
        self.assertFalse(outcome.ok)

    def test_correct_code_from_a_different_pending_cookie_is_refused(self) -> None:
        other = auth.request_code(
            self.database, "ravi.kumar@example.com", now=self.now, mailer=lambda e, c: None
        )
        outcome = auth.verify_code(
            self.database, pending_token=other.pending_token, code=self.code, now=self.now
        )
        self.assertFalse(outcome.ok)

    def test_code_older_than_five_minutes_is_refused(self) -> None:
        later = self.now + timedelta(minutes=5, seconds=1)
        outcome = auth.verify_code(
            self.database, pending_token=self.pending_token, code=self.code, now=later
        )
        self.assertEqual(outcome.error, "expired")

    def test_code_already_used_is_refused_on_second_attempt(self) -> None:
        auth.verify_code(self.database, pending_token=self.pending_token, code=self.code, now=self.now)
        second = auth.verify_code(
            self.database, pending_token=self.pending_token, code=self.code, now=self.now
        )
        self.assertFalse(second.ok)

    def test_sixth_wrong_attempt_is_refused_even_if_correct(self) -> None:
        for _ in range(auth.MAX_ATTEMPTS):
            outcome = auth.verify_code(
                self.database, pending_token=self.pending_token, code="000000", now=self.now
            )
            self.assertFalse(outcome.ok)
        final = auth.verify_code(
            self.database, pending_token=self.pending_token, code=self.code, now=self.now
        )
        self.assertFalse(final.ok)
        self.assertEqual(final.error, "locked")

    def test_revoked_session_is_refused_on_next_request(self) -> None:
        outcome = auth.verify_code(
            self.database, pending_token=self.pending_token, code=self.code, now=self.now
        )
        auth.revoke_session(self.database, outcome.session_token)
        self.assertIsNone(auth.resolve_session(self.database, outcome.session_token))


class ConfigTest(unittest.TestCase):
    def test_bootstrap_admin_email_defaults_to_none(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(config.bootstrap_admin_email())

    def test_bootstrap_admin_email_reads_the_env_override(self) -> None:
        with patch.dict(
            os.environ, {"ESSL_BOOTSTRAP_ADMIN_EMAIL": "boss@example.com"}, clear=True
        ):
            self.assertEqual(config.bootstrap_admin_email(), "boss@example.com")

    def test_setup_token_defaults_to_none(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(config.setup_token())

    def test_setup_token_reads_the_env_override(self) -> None:
        with patch.dict(os.environ, {"ESSL_SETUP_TOKEN": "a" * 16}, clear=True):
            self.assertEqual(config.setup_token(), "a" * 16)

    def test_short_setup_token_is_rejected_naming_the_variable(self) -> None:
        with patch.dict(os.environ, {"ESSL_SETUP_TOKEN": "short"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                config.setup_token()
        self.assertIn("ESSL_SETUP_TOKEN", str(ctx.exception))

    def test_upload_cap_defaults_to_five_megabytes(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.max_upload_mb(), 5)

    def test_upload_cap_reads_the_env_override(self) -> None:
        with patch.dict(os.environ, {"ESSL_MAX_UPLOAD_MB": "12"}, clear=True):
            self.assertEqual(config.max_upload_mb(), 12)

    def test_upload_cap_rejects_a_non_number_naming_the_variable(self) -> None:
        with patch.dict(os.environ, {"ESSL_MAX_UPLOAD_MB": "lots"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                config.max_upload_mb()
        self.assertIn("ESSL_MAX_UPLOAD_MB", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
