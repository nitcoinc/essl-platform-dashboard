"""Database methods for missed-punch alerts (v8): weekly attendance
baselines (KTD2), alert records and the mail outbox (KTD8), and report
links (KTD7). ``Database`` (essl_hub.db) inherits AlertsMixin; the mixin
uses only ``self.connect`` / ``self.transaction`` from it.

Every name here stays importable from essl_hub.db.
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Iterable

from .hashing import sha256_hex
from .mail import dedupe_recipients

if TYPE_CHECKING:
    from .db import _Connection

ALERT_LIVE = "live"
ALERT_MORNING = "morning"
ALERT_KINDS = frozenset({ALERT_LIVE, ALERT_MORNING})
ALERT_MODES = frozenset({"shadow", "live"})
# Not a CHECK: the alerts service may grow statuses without a table rebuild.
# "recorded" is a shadow-mode row, "breaker" a candidate held by the circuit
# breaker (KTD9); "queued" / "sent" / "failed" follow its outbox row.
STATUS_RECORDED = "recorded"
STATUS_QUEUED = "queued"
STATUS_BREAKER = "breaker"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
ALERT_STATUSES = frozenset({STATUS_RECORDED, STATUS_QUEUED, STATUS_BREAKER, STATUS_SENT, STATUS_FAILED})
# Up to 8 attempts per email; retries wait base x 2^(attempts - 1), capped at
# MAIL_RETRY_MAX (5, 10, 20, 40, 60, 60, 60 min), so a mail outage of about
# four hours still delivers (KTD8).
MAIL_MAX_ATTEMPTS = 8
MAIL_RETRY_BASE = timedelta(minutes=5)
MAIL_RETRY_MAX = timedelta(minutes=60)
# Retention, enforced by the alerts service's weekly job.
BASELINE_SNAPSHOTS_KEPT = 4
ALERT_RETENTION_DAYS = 90
MAIL_RETENTION_DAYS = 30


def _core():
    """essl_hub.db, imported at call time only: db.py imports this module to
    build Database, so a top-level import would cycle."""
    from . import db

    return db


def _now() -> datetime:
    # Through essl_hub.db at call time, so tests that patch db._now reach
    # these methods as they reach the core ones.
    return _core()._now()


class AlertsMixin:
    """Baselines, alerts, the mail outbox and report links, mixed into Database."""

    # --- Attendance baselines (v8, KTD2) -----------------------------------

    def record_baselines(self, rows: Iterable[dict]) -> int:
        """Write one weekly snapshot for every employee in one transaction, so
        a running tick never sees a half-built set (KTD2). Each row carries
        employee_id, punched_days, clean_days and metrics (None while there
        are too few clean days). Returns the new snapshot id."""
        rows = list(rows)
        now = _now()
        with self.transaction() as connection:
            snapshot_id = connection.execute(
                "SELECT COALESCE(MAX(snapshot_id), 0) + 1 AS n FROM attendance_baselines"
            ).fetchone()["n"]
            for row in rows:
                connection.execute(
                    "INSERT INTO attendance_baselines (snapshot_id, employee_id, punched_days, "
                    "clean_days, metrics, computed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id, row["employee_id"], int(row["punched_days"]),
                        int(row["clean_days"]), row.get("metrics"), now,
                    ),
                )
        return snapshot_id

    def latest_baselines(self) -> dict[int, dict[str, object]]:
        """The newest snapshot's row per employee; someone missing from it
        has no baseline yet."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM attendance_baselines WHERE snapshot_id = "
                "(SELECT MAX(snapshot_id) FROM attendance_baselines)"
            ).fetchall()
        return {row["employee_id"]: row for row in rows}

    def latest_baseline_at(self) -> datetime | None:
        """When the newest snapshot was computed; None before the first run."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT computed_at FROM attendance_baselines "
                "ORDER BY snapshot_id DESC LIMIT 1"
            ).fetchone()
        return row["computed_at"] if row else None

    def prune_baselines(self, keep: int = BASELINE_SNAPSHOTS_KEPT) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM attendance_baselines WHERE snapshot_id NOT IN ("
                "SELECT DISTINCT snapshot_id FROM attendance_baselines "
                "ORDER BY snapshot_id DESC LIMIT ?)",
                (keep,),
            )
        return cursor.rowcount

    # --- Attendance alerts and mail outbox (v8, KTD8) ----------------------

    def record_alert(
        self,
        *,
        employee_id: int,
        work_day: date,
        kind: str,
        mode: str,
        status: str,
        site: str | None = None,
        detail: dict | None = None,
        mail: dict | None = None,
    ) -> bool:
        """Record one alert before anything is sent, in shadow mode too, so a
        switch to live never resends (KTD8). False, writing nothing, when the
        employee already has an alert of this kind that day. ``mail``
        (recipients, subject, text_body, html_body) is enqueued in the same
        transaction and linked, so the alert's status follows its outbox row."""
        if kind not in ALERT_KINDS or mode not in ALERT_MODES or status not in ALERT_STATUSES:
            raise ValueError(f"bad alert kind/mode/status: {kind}/{mode}/{status}")
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO attendance_alerts (employee_id, work_day, kind, mode, status, site, "
                "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (employee_id, work_day, kind) DO NOTHING",
                (employee_id, work_day, kind, mode, status, site, detail, _now()),
            )
            if not cursor.rowcount:
                return False
            if mail is not None:
                alert_id = cursor.lastrowid
                outbox_id = _insert_mail(connection, **mail)
                connection.execute(
                    "UPDATE attendance_alerts SET outbox_id = ? WHERE id = ?", (outbox_id, alert_id)
                )
        return True

    def alerts_for_day(self, work_day: date) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT a.*, e.name AS employee_name FROM attendance_alerts a "
                "JOIN employees e ON e.id = a.employee_id "
                "WHERE a.work_day = ? ORDER BY a.created_at, a.id",
                (work_day,),
            ).fetchall()
        return list(rows)

    def alert_counts(self, since: date) -> list[dict[str, object]]:
        """Alerts per site, kind, mode and status for work days from
        ``since`` (the would-have-sent counts, KTD11). The site is the one
        recorded with the alert; None when unknown."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT site, kind, mode, status, COUNT(*) AS n FROM attendance_alerts "
                "WHERE work_day >= ? GROUP BY site, kind, mode, status "
                "ORDER BY site, kind, mode, status",
                (since,),
            ).fetchall()
        return list(rows)

    def prune_alerts(self, days: int = ALERT_RETENTION_DAYS) -> int:
        cutoff = _now().date() - timedelta(days=days)
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM attendance_alerts WHERE work_day < ?", (cutoff,)
            )
        return cursor.rowcount

    def enqueue_mail(
        self,
        recipients: Iterable[str],
        subject: str,
        text_body: str,
        html_body: str | None = None,
    ) -> int:
        """Queue one email for the alerts service to send (the web only
        enqueues). Returns the outbox row id."""
        with self.transaction() as connection:
            return _insert_mail(connection, recipients, subject, text_body, html_body)

    def due_mail(self, limit: int = 20) -> list[dict[str, object]]:
        """Pending outbox rows whose next attempt is due, oldest first."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mail_outbox WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, id LIMIT ?",
                (_now(), limit),
            ).fetchall()
        return list(rows)

    def mark_mail_sent(self, mail_id: int) -> None:
        """Sent: the bodies are nulled so raw report tokens do not persist (KTD8)."""
        with self.transaction() as connection:
            connection.execute(
                "UPDATE mail_outbox SET status = 'sent', sent_at = ?, text_body = NULL, "
                "html_body = NULL, last_error = NULL WHERE id = ? AND status = 'pending'",
                (_now(), mail_id),
            )
            connection.execute(
                "UPDATE attendance_alerts SET status = 'sent' WHERE outbox_id = ?", (mail_id,)
            )

    def mark_mail_failed(self, mail_id: int, error: str) -> str | None:
        """One failed attempt: retry after a backoff, or after MAIL_MAX_ATTEMPTS
        mark the row failed with its bodies nulled. The alert stays recorded,
        so nothing re-creates it (KTD8). Returns the row's status, None when
        there is no such row."""
        now = _now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status, attempts FROM mail_outbox WHERE id = ?", (mail_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                return row["status"] if row else None
            attempts = int(row["attempts"]) + 1
            error = (error or "")[:500] or None
            if attempts >= MAIL_MAX_ATTEMPTS:
                connection.execute(
                    "UPDATE mail_outbox SET status = 'failed', attempts = ?, last_error = ?, "
                    "text_body = NULL, html_body = NULL WHERE id = ?",
                    (attempts, error, mail_id),
                )
                connection.execute(
                    "UPDATE attendance_alerts SET status = 'failed' WHERE outbox_id = ?", (mail_id,)
                )
                return "failed"
            connection.execute(
                "UPDATE mail_outbox SET attempts = ?, last_error = ?, next_attempt_at = ? "
                "WHERE id = ?",
                (attempts, error, now + mail_retry_delay(attempts), mail_id),
            )
            return "pending"

    def prune_mail(self, days: int = MAIL_RETENTION_DAYS) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM mail_outbox WHERE created_at < ?", (_now() - timedelta(days=days),)
            )
        return cursor.rowcount

    # --- Report links (v8, KTD7) -------------------------------------------

    def create_report_link(self, employee_id: int, work_day: date, *, expires_at: datetime) -> str:
        """A fresh report-form token bound to one employee and work day
        (KTD7). Only its SHA-256 is stored; the raw token is returned once,
        for the email."""
        token = secrets.token_urlsafe(32)
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO fix_report_links (token_hash, employee_id, work_day, expires_at, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (sha256_hex(token), employee_id, work_day, expires_at, _now()),
            )
        return token

    def report_link(self, token: str) -> dict[str, object] | None:
        """The link a raw token opens, matched by hash; None when unknown or expired."""
        if not token:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM fix_report_links WHERE token_hash = ? AND expires_at > ?",
                (sha256_hex(token), _now()),
            ).fetchone()
        return row

    def prune_report_links(self) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM fix_report_links WHERE expires_at <= ?", (_now(),)
            )
        return cursor.rowcount


def mail_retry_delay(attempts: int) -> timedelta:
    """The wait after ``attempts`` failed sends (KTD8)."""
    return min(MAIL_RETRY_BASE * 2 ** (attempts - 1), MAIL_RETRY_MAX)


def _insert_mail(
    connection: _Connection,
    recipients: Iterable[str],
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> int:
    """Queue one outbox row inside the caller's transaction (KTD8)."""
    unique = dedupe_recipients(recipients)
    if not unique:
        raise ValueError("an email needs at least one recipient")
    now = _now()
    return connection.execute(
        "INSERT INTO mail_outbox (recipients, subject, text_body, html_body, next_attempt_at, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (json.dumps(unique), subject, text_body, html_body, now, now),
    ).lastrowid
