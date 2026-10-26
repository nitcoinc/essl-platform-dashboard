from __future__ import annotations

import html
import json
import os
import re
import sys
import types
import unittest
from datetime import date, time
from unittest.mock import MagicMock, patch

from essl_hub import alert_mail as am
from essl_hub import mail

DAY = date(2026, 10, 26)  # a Monday
REPORT = "https://attendance.example.com/fix/tok123"
PRIVACY = "https://attendance.example.com/privacy/alerts"
REVIEW = "https://attendance.example.com/fixes/42"
EVIL = "<script>alert(1)</script>"
BANNED = (
    "violation", "anomaly", "flagged", "suspicious", "irregular",
    "non-compliance", "usual", "limit", "baseline",
)
ALL_ISSUES = [
    am.MorningIssue(am.MISSING_FINAL_OUT, start=time(18, 2)),
    am.MorningIssue(am.OUT_WITHOUT_IN, start=time(13, 10)),
    am.MorningIssue(am.DOUBLE_IN, start=time(9, 2), end=time(14, 5)),
    am.MorningIssue(am.UNCLEAR_GAP, start=time(13, 0), end=time(15, 30)),
]
LINES = [
    am.FixLine(am.MISSED_IN, at=time(13, 40)),
    am.FixLine(am.FLIP, at=time(9, 5), direction="in"),
]


def _live(**overrides) -> am.RenderedEmail:
    values = dict(
        employee_name="Ajay Kumar",
        work_day=DAY,
        out_at=time(13, 0),
        out_minutes=105,
        report_url=REPORT,
        privacy_url=PRIVACY,
    )
    values.update(overrides)
    return am.live_check(**values)


def _morning(**overrides) -> am.RenderedEmail:
    values = dict(
        employee_name="Ajay Kumar",
        work_day=DAY,
        issues=ALL_ISSUES,
        report_url=REPORT,
        privacy_url=PRIVACY,
    )
    values.update(overrides)
    return am.morning_check(**values)


def _request(**overrides) -> am.RenderedEmail:
    values = dict(
        employee_name="Ajay Kumar", work_day=DAY, lines=LINES, note="Reader beeped red"
    )
    values.update(overrides)
    return am.new_request(**values)


def _decision(**overrides) -> am.RenderedEmail:
    values = dict(
        employee_name="Ajay Kumar",
        work_day=DAY,
        outcome=am.APPROVED,
        lines=LINES,
        hr_note="Checked the CCTV log",
    )
    values.update(overrides)
    return am.decision(**values)


def _all_templates(name: str = "Ajay Kumar", note: str = "A note"):
    return {
        "live": _live(employee_name=name),
        "morning": _morning(employee_name=name),
        "hr": _request(employee_name=name, note=note, review_url=REVIEW),
        "manager": _request(employee_name=name, note=note),
        "decision": _decision(employee_name=name, hr_note=note),
    }


class FormattingTest(unittest.TestCase):
    def test_durations(self) -> None:
        self.assertEqual(am.fmt_duration(45), "45 min")
        self.assertEqual(am.fmt_duration(120), "2 h")
        self.assertEqual(am.fmt_duration(105), "1 h 45 min")

    def test_day(self) -> None:
        self.assertEqual(am.fmt_day(DAY), "Mon, 26 Oct")


class LiveCheckTest(unittest.TestCase):
    def test_subject_is_date_first_and_factual(self) -> None:
        self.assertEqual(
            _live().subject, "Attendance check for Mon, 26 Oct – a swipe may be missing"
        )

    def test_html_has_logo_times_out_stretch_and_links(self) -> None:
        email = _live()
        self.assertIn("cid:nitco-logo", email.html)
        self.assertIn("13:00", email.html)
        self.assertIn("1 h 45 min", email.html)
        self.assertIn("may have missed your swipe back in", email.html)
        self.assertIn(f'href="{REPORT}"', email.html)
        self.assertIn(PRIVACY, email.html)
        self.assertIn("No action needed if this is right", email.html)
        self.assertIn('name="color-scheme" content="light dark"', email.html)
        self.assertIn('name="supported-color-schemes"', email.html)

    def test_text_part_carries_the_same_links(self) -> None:
        email = _live()
        self.assertIn(REPORT, email.text)
        self.assertIn(PRIVACY, email.text)
        self.assertIn("13:00", email.text)
        self.assertIn("1 h 45 min", email.text)
        self.assertIn("No action needed if this is right", email.text)


class MorningCheckTest(unittest.TestCase):
    def test_each_issue_has_its_own_sentence(self) -> None:
        email = _morning()
        for issue in ALL_ISSUES:
            sentence = am.issue_sentence(issue)
            self.assertIn(sentence, email.text)
            self.assertIn(html.escape(sentence), email.html)
        self.assertEqual(email.html.count("<li"), len(ALL_ISSUES))
        for clock in ("18:02", "13:10", "09:02", "14:05", "15:30"):
            self.assertIn(clock, email.text)

    def test_subject_names_the_missing_swipe_out(self) -> None:
        email = _morning(issues=[ALL_ISSUES[0]])
        self.assertEqual(
            email.subject, "Yesterday's attendance (Mon, 26 Oct): your swipe-out wasn't recorded"
        )

    def test_mixed_issues_use_the_generic_subject(self) -> None:
        self.assertEqual(
            _morning().subject, "Yesterday's attendance (Mon, 26 Oct): a swipe may be missing"
        )

    def test_after_a_weekend_the_subject_does_not_say_yesterday(self) -> None:
        email = _morning(work_day=date(2026, 10, 23), sent_on=date(2026, 10, 26))
        self.assertTrue(email.subject.startswith("Your attendance on Fri, 23 Oct:"))

    def test_links_logo_and_no_action_line(self) -> None:
        email = _morning()
        self.assertIn("cid:nitco-logo", email.html)
        for part in (email.html, email.text):
            self.assertIn(REPORT, part)
            self.assertIn(PRIVACY, part)
            self.assertIn("No action needed if this is right", part)


class RequestTest(unittest.TestCase):
    def test_subject(self) -> None:
        self.assertEqual(_request().subject, "Fix request from Ajay Kumar for Mon, 26 Oct")

    def test_hr_variant_has_review_button_and_link(self) -> None:
        email = _request(review_url=REVIEW)
        self.assertIn("Review request", email.html)
        self.assertIn(f'href="{REVIEW}"', email.html)
        self.assertIn(REVIEW, email.text)
        self.assertIn("Missed swipe-in at 13:40", email.html)
        self.assertIn("The 09:05 swipe was a swipe-in", email.text)
        self.assertIn("Reader beeped red", email.html)

    def test_manager_variant_is_fyi_without_review_link(self) -> None:
        email = _request()
        self.assertNotIn("Review request", email.html)
        self.assertNotIn("href=", email.html)
        self.assertNotIn("http", email.text)
        self.assertIn("HR will review", email.text)
        self.assertIn("Missed swipe-in at 13:40", email.text)

    def test_manager_who_is_also_hr_gets_one_copy_the_hr_variant(self) -> None:
        hr, manager = am.request_recipients(
            "Boss@Example.com", ["hr1@example.com", "boss@example.com"]
        )
        self.assertEqual(hr, ["hr1@example.com", "boss@example.com"])
        self.assertEqual(manager, [])

    def test_manager_outside_hr_gets_the_fyi(self) -> None:
        hr, manager = am.request_recipients("boss@example.com", ["hr1@example.com"])
        self.assertEqual(hr, ["hr1@example.com"])
        self.assertEqual(manager, ["boss@example.com"])
        self.assertEqual(am.request_recipients(None, ["hr1@example.com"])[1], [])


class DecisionTest(unittest.TestCase):
    def test_subjects_per_outcome(self) -> None:
        self.assertEqual(
            _decision().subject, "Your attendance fix for Mon, 26 Oct was approved"
        )
        self.assertEqual(
            _decision(outcome=am.PARTIAL).subject,
            "Your attendance fix for Mon, 26 Oct was partly approved",
        )
        self.assertEqual(
            _decision(outcome=am.REJECTED).subject,
            "Your attendance fix for Mon, 26 Oct wasn't approved",
        )

    def test_includes_hr_note_and_line_outcomes(self) -> None:
        lines = [
            am.FixLine(am.MISSED_IN, at=time(13, 40), approved=True),
            am.FixLine(am.IGNORE, at=time(9, 5), approved=False),
        ]
        email = _decision(outcome=am.PARTIAL, lines=lines)
        for part in (email.html, email.text):
            self.assertIn("Checked the CCTV log", part)
            self.assertIn("Missed swipe-in at 13:40: approved", part)
            self.assertIn("Ignore the swipe at 09:05: not approved", part)

    def test_unknown_outcome_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _decision(outcome="maybe")


class CopyAndEscapingTest(unittest.TestCase):
    def test_no_banned_words_in_any_template(self) -> None:
        for kind, email in _all_templates().items():
            blob = " ".join(email).lower()
            for word in BANNED:
                self.assertIsNone(
                    re.search(re.escape(word), blob), f"{kind} contains {word!r}"
                )

    def test_every_template_escapes_name_and_note(self) -> None:
        for kind, email in _all_templates(name=f"Ajay {EVIL}", note=EVIL).items():
            self.assertNotIn("<script>", email.html, kind)
            self.assertIn("&lt;script&gt;", email.html, kind)

    def test_subject_strips_newlines_from_names(self) -> None:
        email = _request(employee_name="Ajay\r\nBcc: x@example.com")
        self.assertNotIn("\n", email.subject)
        self.assertNotIn("\r", email.subject)


class SendTemplateTest(unittest.TestCase):
    def test_cloudflare_sends_a_template_as_text_only(self) -> None:
        env = {
            "ESSL_MAIL_PROVIDER": "cloudflare",
            "CLOUDFLARE_API_TOKEN": "cf-token",
            "CLOUDFLARE_ACCOUNT_ID": "acct-123",
            "CLOUDFLARE_FROM_ADDRESS": "attendance@example.com",
        }
        response = MagicMock()
        response.read.return_value = b'{"success": true}'
        context = MagicMock()
        context.__enter__.return_value = response
        email = _live()
        with patch.dict(os.environ, env, clear=True), patch(
            "urllib.request.urlopen", return_value=context
        ) as opener:
            mail.send_email(email.subject, ["a@example.com"], email.text, email.html)
        body = json.loads(opener.call_args[0][0].data)
        self.assertNotIn("html", body)
        self.assertIn(REPORT, body["text"])

    def test_resend_sends_a_template_with_the_logo(self) -> None:
        fake = types.SimpleNamespace(
            api_key=None, Emails=types.SimpleNamespace(send=MagicMock())
        )
        env = {
            "ESSL_MAIL_PROVIDER": "resend",
            "RESEND_API_KEY": "k",
            "RESEND_FROM_ADDRESS": "a@b.c",
        }
        email = _morning()
        with patch.dict(os.environ, env, clear=True), patch.dict(
            sys.modules, {"resend": fake}
        ):
            mail.send_email(email.subject, ["a@example.com"], email.text, email.html)
        payload = fake.Emails.send.call_args[0][0]
        self.assertIn("cid:nitco-logo", payload["html"])
        self.assertEqual(payload["headers"], {"Auto-Submitted": "auto-generated"})


if __name__ == "__main__":
    unittest.main()
