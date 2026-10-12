import io
import json
import os
import unittest
import urllib.parse
from unittest import mock

from essl_hub import turnstile

CFG = turnstile.TurnstileConfig("site", "secret")


def _reply(body: dict):
    return mock.MagicMock(__enter__=lambda s: io.BytesIO(json.dumps(body).encode()), __exit__=lambda *a: False)


class ConfigTest(unittest.TestCase):
    def test_needs_both_keys(self) -> None:
        with mock.patch.dict(os.environ, {"TURNSTILE_SITE_KEY": "s", "TURNSTILE_SECRET_KEY": ""}):
            self.assertIsNone(turnstile.config_from_env())
        with mock.patch.dict(os.environ, {"TURNSTILE_SITE_KEY": "s", "TURNSTILE_SECRET_KEY": "k"}):
            self.assertEqual(turnstile.config_from_env(), turnstile.TurnstileConfig("s", "k"))


class VerifyTest(unittest.TestCase):
    def test_success_posts_secret_token_and_ip(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_reply({"success": True})) as urlopen:
            self.assertTrue(turnstile.verify(CFG, "tok", "203.0.113.9"))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, turnstile.SITEVERIFY_URL)
        self.assertEqual(
            urllib.parse.parse_qs(request.data.decode()),
            {"secret": ["secret"], "response": ["tok"], "remoteip": ["203.0.113.9"]},
        )

    def test_rejected_token_fails(self) -> None:
        reply = _reply({"success": False, "error-codes": ["invalid-input-response"]})
        with mock.patch("urllib.request.urlopen", return_value=reply):
            self.assertFalse(turnstile.verify(CFG, "tok"))

    def test_unreachable_fails_closed(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertFalse(turnstile.verify(CFG, "tok"))

    def test_empty_token_never_calls_cloudflare(self) -> None:
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertFalse(turnstile.verify(CFG, ""))
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
