from __future__ import annotations

import json
import os
import sys
import types
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from essl_hub import config, mail


def _fake_response(payload: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    context = MagicMock()
    context.__enter__.return_value = response
    return context


class MailProviderTest(unittest.TestCase):
    def test_explicit_provider_wins(self) -> None:
        with patch.dict(os.environ, {"ESSL_MAIL_PROVIDER": "cloudflare"}, clear=True):
            self.assertEqual(config.mail_provider(), "cloudflare")
        with patch.dict(os.environ, {"ESSL_MAIL_PROVIDER": "resend"}, clear=True):
            self.assertEqual(config.mail_provider(), "resend")

    def test_console_provider_prints_code_and_sends_nothing(self) -> None:
        from essl_hub import mail

        with patch.dict(os.environ, {"ESSL_MAIL_PROVIDER": "console"}, clear=True), \
                patch.object(mail, "_send_via_resend") as resend, \
                patch.object(mail, "_send_via_cloudflare") as cloudflare, \
                patch("builtins.print") as printed:
            mail.send_login_code("a@example.com", "123456")
        resend.assert_not_called()
        cloudflare.assert_not_called()
        self.assertIn("123456", printed.call_args.args[0])

    def test_auto_detects_cloudflare_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {"CLOUDFLARE_API_TOKEN": "t", "CLOUDFLARE_ACCOUNT_ID": "a"},
            clear=True,
        ):
            self.assertEqual(config.mail_provider(), "cloudflare")

    def test_defaults_to_resend(self) -> None:
        with patch.dict(os.environ, {"RESEND_API_KEY": "k"}, clear=True):
            self.assertEqual(config.mail_provider(), "resend")

    def test_unknown_provider_is_rejected(self) -> None:
        with patch.dict(os.environ, {"ESSL_MAIL_PROVIDER": "sendgrid"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                config.mail_provider()
        self.assertIn("ESSL_MAIL_PROVIDER", str(ctx.exception))


class CloudflareSendTest(unittest.TestCase):
    ENV = {
        "ESSL_MAIL_PROVIDER": "cloudflare",
        "CLOUDFLARE_API_TOKEN": "cf-token",
        "CLOUDFLARE_ACCOUNT_ID": "acct-123",
        "CLOUDFLARE_FROM_ADDRESS": "attendance@example.com",
    }

    def setUp(self) -> None:
        environment = patch.dict(os.environ, self.ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_posts_the_expected_request(self) -> None:
        with patch(
            "urllib.request.urlopen", return_value=_fake_response({"success": True})
        ) as opener:
            mail.send_login_code("ajay@example.com", "123456")
        request = opener.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://api.cloudflare.com/client/v4/accounts/acct-123/email/sending/send",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer cf-token")
        body = json.loads(request.data)
        self.assertEqual(body["to"], "ajay@example.com")
        self.assertEqual(body["from"], "attendance@example.com")
        self.assertIn("123456", body["text"])

    def test_http_error_is_reported_as_mailerror(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.cloudflare.com", 401, "Unauthorized", {}, None
        )
        error.read = lambda: b'{"success": false, "errors": [{"code": 10101}]}'
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(mail.MailError) as ctx:
                mail.send_login_code("ajay@example.com", "123456")
        self.assertIn("401", str(ctx.exception))

    def test_api_level_failure_is_reported(self) -> None:
        payload = {"success": False, "errors": [{"code": 10102, "message": "forbidden"}]}
        with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            with self.assertRaises(mail.MailError) as ctx:
                mail.send_login_code("ajay@example.com", "123456")
        self.assertIn("10102", str(ctx.exception))

    def test_missing_credentials_name_the_variables(self) -> None:
        with patch.dict(os.environ, {"ESSL_MAIL_PROVIDER": "cloudflare"}, clear=True):
            with self.assertRaises(mail.MailError) as ctx:
                mail.send_login_code("ajay@example.com", "123456")
        self.assertIn("CLOUDFLARE_API_TOKEN", str(ctx.exception))


class ResendSendTest(unittest.TestCase):
    def test_resend_provider_uses_the_sdk(self) -> None:
        fake = types.SimpleNamespace(
            api_key=None, Emails=types.SimpleNamespace(send=MagicMock())
        )
        environment = {
            "ESSL_MAIL_PROVIDER": "resend",
            "RESEND_API_KEY": "k",
            "RESEND_FROM_ADDRESS": "a@b.c",
        }
        with patch.dict(os.environ, environment, clear=True), patch.dict(
            sys.modules, {"resend": fake}
        ):
            mail.send_login_code("ajay@example.com", "123456")
        fake.Emails.send.assert_called_once()
        payload = fake.Emails.send.call_args[0][0]
        self.assertEqual(payload["to"], "ajay@example.com")
        self.assertIn("123456", payload["text"])
        self.assertIn("123456", payload["html"])
        self.assertIn(f"cid:{mail.LOGO_CID}", payload["html"])
        logo = payload["attachments"][0]
        self.assertEqual(logo["content_id"], mail.LOGO_CID)
        self.assertEqual(bytes(logo["content"][:8]), b"\x89PNG\r\n\x1a\n")

    def test_html_escapes_code(self) -> None:
        self.assertNotIn("<b>", mail.render_html("<b>"))

    def test_resend_without_credentials_names_the_variables(self) -> None:
        with patch.dict(os.environ, {"ESSL_MAIL_PROVIDER": "resend"}, clear=True):
            with self.assertRaises(mail.MailError) as ctx:
                mail.send_login_code("ajay@example.com", "123456")
        self.assertIn("RESEND_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
