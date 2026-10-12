"""Cloudflare Turnstile (bot check) for the public sign-in and setup forms.

Enabled when both TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY are set; unset
means local or test use and the check is skipped. When enabled it fails
closed: a missing token, a rejected token or an unreachable siteverify all
refuse the request (the site sits behind Cloudflare anyway, so Cloudflare
being down already means the site is down).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# Name of the hidden form field the widget adds.
RESPONSE_FIELD = "cf-turnstile-response"
_TIMEOUT_SECONDS = 5

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnstileConfig:
    site_key: str
    secret_key: str


def config_from_env() -> TurnstileConfig | None:
    site_key = (os.getenv("TURNSTILE_SITE_KEY") or "").strip()
    secret_key = (os.getenv("TURNSTILE_SECRET_KEY") or "").strip()
    if not site_key or not secret_key:
        return None
    return TurnstileConfig(site_key, secret_key)


def verify(cfg: TurnstileConfig, token: str, remote_ip: str | None = None) -> bool:
    """True only when Cloudflare confirms the token. Never raises."""
    if not token:
        return False
    fields = {"secret": cfg.secret_key, "response": token}
    if remote_ip:
        fields["remoteip"] = remote_ip
    request = urllib.request.Request(
        SITEVERIFY_URL,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read() or b"{}")
    except Exception as exc:  # noqa: BLE001 - fail closed on any transport error
        _logger.warning("Turnstile siteverify unreachable: %s", exc)
        return False
    if not body.get("success", False):
        _logger.info("Turnstile rejected a token: %s", body.get("error-codes"))
        return False
    return True
