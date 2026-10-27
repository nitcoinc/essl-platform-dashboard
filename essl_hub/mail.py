from __future__ import annotations

import html
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path

from .config import mail_provider

SUBJECT = "Your eSSL Attendance Hub sign-in code"
_TIMEOUT_SECONDS = 10
CLOUDFLARE_ENDPOINT = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/email/sending/send"
)
# White transparent logo; the email header cell supplies the dark-blue backdrop.
LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "email-logo.png"
LOGO_CID = "nitco-logo"
BRAND_BLUE = "#0b1f4b"
# RFC 3834: tells out-of-office responders and mail rules not to auto-reply.
AUTO_SUBMITTED_HEADERS = {"Auto-Submitted": "auto-generated"}
_SIGN_IN = "the sign-in code"

log = logging.getLogger(__name__)


class MailError(Exception):
    pass


def send_login_code(email: str, code: str) -> None:
    text = (
        f"Your sign-in code is {code}. It expires in 5 minutes.\n\n"
        "Never share this code with anyone."
    )
    provider = mail_provider()
    if provider == "console":
        # Local development only; never set ESSL_MAIL_PROVIDER=console in production.
        log.warning("console mail: sign-in code for %s is %s", email, code)
        print(f"[console mail] sign-in code for {email}: {code}", flush=True)
    elif provider == "cloudflare":
        _send_via_cloudflare(email, SUBJECT, text, what=_SIGN_IN)
    else:
        _send_via_resend(email, SUBJECT, text, render_html(code), what=_SIGN_IN)


def dedupe_recipients(recipients: Iterable[str]) -> list[str]:
    """Drop blanks and case-insensitive repeats, keeping the first spelling and order."""
    seen: set[str] = set()
    unique: list[str] = []
    for address in recipients:
        address = (address or "").strip()
        if address and address.casefold() not in seen:
            seen.add(address.casefold())
            unique.append(address)
    return unique


def send_email(
    subject: str,
    recipients: Iterable[str],
    text: str,
    html_body: str | None = None,
    *,
    reply_to: str | None = None,
) -> list[str]:
    """Send one automated email to every recipient; returns who it went to.

    Resend gets the HTML part with the inline logo; Cloudflare gets text only,
    so the logo shows only via Resend.
    """
    to = dedupe_recipients(recipients)
    if not to:
        raise MailError("No recipients for the email")
    # A CR/LF in a subject would let a name smuggle in extra header lines.
    subject = " ".join(subject.split())
    reply_to = (reply_to or "").strip() or None
    provider = mail_provider()
    if provider == "console":
        # Local development only: bodies can hold report links, so never use in production.
        log.warning("console mail: %r to %s", subject, ", ".join(to))
        print(f"[console mail] to {', '.join(to)}: {subject}\n{text}", flush=True)
    elif provider == "cloudflare":
        _send_via_cloudflare(
            to, subject, text, reply_to=reply_to, headers=AUTO_SUBMITTED_HEADERS
        )
    else:
        _send_via_resend(
            to,
            subject,
            text,
            html_body,
            reply_to=reply_to,
            headers=AUTO_SUBMITTED_HEADERS,
        )
    return to


def render_html(code: str) -> str:
    """Table-based layout with inline styles so Outlook and Gmail render it."""
    code = html.escape(code)
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f1f3f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f3f8;padding:32px 12px;">
<tr><td align="center">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="max-width:480px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;font-family:Segoe UI,Helvetica,Arial,sans-serif;">
<tr><td align="center" bgcolor="{BRAND_BLUE}" style="background:{BRAND_BLUE};padding:28px 24px;">
<img src="cid:{LOGO_CID}" width="180" alt="NITCO" style="display:block;width:180px;max-width:100%;height:auto;border:0;">
</td></tr>
<tr><td style="padding:32px 32px 8px;color:#17171c;">
<h1 style="margin:0 0 12px;font-size:20px;font-weight:600;">Your sign-in code</h1>
<p style="margin:0;font-size:15px;line-height:1.5;color:#4a4a55;">Enter this code to sign in to eSSL Attendance Hub.</p>
</td></tr>
<tr><td align="center" style="padding:24px 32px;">
<div style="display:inline-block;padding:16px 28px;background:#eef1fa;border:1px solid #d5dbef;border-radius:10px;font-family:Consolas,Menlo,monospace;font-size:34px;font-weight:700;letter-spacing:10px;color:{BRAND_BLUE};">{code}</div>
</td></tr>
<tr><td style="padding:0 32px 32px;font-size:13px;line-height:1.5;color:#6b6b76;">
This code expires in 5 minutes. Never share it with anyone. If you did not try to sign in, ignore this email.
</td></tr>
</table>
</td></tr></table>
</body></html>"""


def _send_via_resend(
    to: str | list[str],
    subject: str,
    text: str,
    html_body: str | None,
    *,
    reply_to: str | None = None,
    headers: dict[str, str] | None = None,
    what: str = "the email",
) -> None:
    api_key = os.getenv("RESEND_API_KEY")
    from_address = os.getenv("RESEND_FROM_ADDRESS")
    if not api_key or not from_address:
        raise MailError("RESEND_API_KEY and RESEND_FROM_ADDRESS must be set")

    import resend

    params: dict = {"from": from_address, "to": to, "subject": subject, "text": text}
    if html_body is not None:
        params["html"] = html_body
        params["attachments"] = [
            {
                "filename": "nitco-logo.png",
                "content": list(LOGO_PATH.read_bytes()),
                "content_type": "image/png",
                "content_id": LOGO_CID,
            }
        ]
    if reply_to:
        params["reply_to"] = reply_to
    if headers:
        params["headers"] = dict(headers)

    resend.api_key = api_key
    try:
        resend.Emails.send(params)
    except Exception as exc:  # noqa: BLE001 - surface any Resend failure uniformly
        raise MailError(f"Resend could not send {what}: {exc}") from exc


def _send_via_cloudflare(
    to: str | list[str],
    subject: str,
    text: str,
    *,
    reply_to: str | None = None,
    headers: dict[str, str] | None = None,
    what: str = "the email",
) -> None:
    """Text only: the API takes HTML, but the CID logo is wired up for Resend alone."""
    api_token = os.getenv("CLOUDFLARE_API_TOKEN")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    from_address = os.getenv("CLOUDFLARE_FROM_ADDRESS")
    if not api_token or not account_id or not from_address:
        raise MailError(
            "CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID and "
            "CLOUDFLARE_FROM_ADDRESS must be set"
        )

    fields: dict = {"to": to, "from": from_address, "subject": subject, "text": text}
    # REST field names per developers.cloudflare.com/email-service; Auto-Submitted
    # is on its header allowlist, and Reply-To must use the field, not a header.
    if reply_to:
        fields["reply_to"] = reply_to
    if headers:
        fields["headers"] = dict(headers)
    payload = json.dumps(fields).encode("utf-8")
    request = urllib.request.Request(
        CLOUDFLARE_ENDPOINT.format(account_id=account_id),
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()[:300]
        raise MailError(
            f"Cloudflare could not send {what} (HTTP {exc.code}): {detail}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - any transport failure is uniformly fatal here
        raise MailError(f"Cloudflare could not send {what}: {exc}") from exc

    if not body.get("success", False):
        raise MailError(f"Cloudflare could not send {what}: {body.get('errors')}")
