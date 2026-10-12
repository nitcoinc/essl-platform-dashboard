from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from . import mail
from .config import TIMEZONE_NAME
from .hashing import sha256_hex as _hash
from .mail import MailError

_TZ = ZoneInfo(TIMEZONE_NAME)
_logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(days=30)
MAX_ATTEMPTS = 5
PER_EMAIL_HOURLY_LIMIT = 5
PER_IP_HOURLY_LIMIT = 20
PER_IP_BURST_LIMIT = 3
PER_IP_BURST_WINDOW = timedelta(minutes=1)


@dataclass(frozen=True)
class RequestCodeResult:
    pending_token: str


@dataclass(frozen=True)
class VerifyOutcome:
    ok: bool
    session_token: str | None = None
    employee: dict | None = None
    csrf_token: str | None = None
    error: str | None = None  # "invalid" | "expired" | "locked"


def request_code(
    database,
    email: str,
    *,
    requested_ip: str | None = None,
    now: datetime | None = None,
    mailer: Callable[[str, str], None] | None = None,
) -> RequestCodeResult:
    """Always returns the same shape and does the same DB work, whether or not
    the email is on the roster, so the response cannot be used to enumerate
    valid addresses (R11). A send failure is logged for an operator, never
    surfaced to the caller here -- reporting it differently would itself be
    the enumeration signal R11 exists to prevent.

    ponytail: the mailer call below still runs inline, so a roster match
    that has to wait on the email provider takes measurably longer to respond
    than a non-match. Closing that fully needs a background task queue; this app
    has none. Upgrade path: dispatch the send through one if the timing gap
    is ever demonstrated to matter in practice.

    The login-code row is written before any mail goes out, so an OfflineError
    (Turso unreachable, KTD11) propagates to the app's 503 offline page and no
    code is ever emailed that the verify step could not find.
    """
    mailer = mailer or mail.send_login_code
    now = now or _now()
    email = email.strip()
    pending_token = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(1_000_000):06d}"

    rate_limited = database.count_recent_login_codes(
        email=email, since=now - timedelta(hours=1)
    ) >= PER_EMAIL_HOURLY_LIMIT
    rate_limited = rate_limited or _ip_rate_limited(
        database.count_recent_login_codes_by_ip, requested_ip, now
    )

    database.create_login_code(
        email=email,
        code_hash=_hash(code),
        pending_token_hash=_hash(pending_token),
        requested_ip=requested_ip,
        expires_at=now + CODE_TTL,
    )

    employee = database.employee_by_email(email)
    if employee is not None and not rate_limited:
        try:
            mailer(email, code)
        except MailError:
            _logger.error("Failed to send a sign-in code to %s", email, exc_info=True)

    return RequestCodeResult(pending_token=pending_token)


def verify_code(
    database, *, pending_token: str, code: str, now: datetime | None = None
) -> VerifyOutcome:
    now = now or _now()
    login_code = database.active_login_code(pending_token_hash=_hash(pending_token))
    state = _challenge_state(login_code, now)
    if state is not None:
        return VerifyOutcome(ok=False, error=state)
    if _hash(code) != login_code["code_hash"]:
        database.increment_login_code_attempts(login_code["id"])
        return VerifyOutcome(ok=False, error="invalid")

    database.consume_login_code(login_code["id"])
    employee = database.employee_by_email(login_code["email"])
    if employee is None:
        return VerifyOutcome(ok=False, error="invalid")

    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    database.create_session(
        employee_id=employee["id"],
        token_hash=_hash(session_token),
        csrf_token=csrf_token,
        expires_at=now + SESSION_TTL,
    )
    return VerifyOutcome(
        ok=True,
        session_token=session_token,
        employee=employee,
        csrf_token=csrf_token,
    )


def resolve_session(database, session_token: str | None) -> dict | None:
    """Resolve a session cookie to `{"employee": ..., "csrf_token": ...}`.

    The CSRF token rides along on the session row rather than in the cookie, so
    a page can embed it and a POST can compare the two without a shared
    signing key.
    """
    if not session_token:
        return None
    row = database.session_employee(_hash(session_token))
    if row is None:
        return None
    return {
        "employee": {key: value for key, value in row.items() if key != "csrf_token"},
        "csrf_token": row["csrf_token"],
    }


def revoke_session(database, session_token: str) -> None:
    database.revoke_session(_hash(session_token))


def _challenge_state(row, now: datetime) -> str | None:
    if row is None:
        return "invalid"
    if row["attempts"] >= MAX_ATTEMPTS:
        return "locked"
    if row["expires_at"] < now:
        return "expired"
    return None


def _ip_rate_limited(count_by_ip: Callable[..., int], ip: str | None, now: datetime) -> bool:
    if not ip:
        return False
    if count_by_ip(ip=ip, since=now - PER_IP_BURST_WINDOW) >= PER_IP_BURST_LIMIT:
        return True
    return count_by_ip(ip=ip, since=now - timedelta(hours=1)) >= PER_IP_HOURLY_LIMIT


def _now() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)
