"""The alerts service (U6): weekly baselines, the live and morning checks, and
the mail outbox.

    python -m essl_hub.alerts_main

A separate service on the app image (KTD3): it opens the shared database
with ``must_exist=True`` and waits for schema v8 instead of migrating. It
ticks every minute. Each tick:

- rebuilds baselines and prunes old rows once a week (first tick after
  Sunday 02:00 IST once the newest snapshot is over 6 days old, or at once
  when there is none);
- runs the live "out too long" check every 5 minutes and the morning check
  for the previous work day (every 30 minutes once a run held nobody back),
  both only in shadow or live mode, inside their hours, and never while the
  collector's data is stale (KTD9);
- drains the outbox, always, whatever the mode, hours or staleness: the web
  only enqueues, so fix-request and decision emails go out from here.

An alert is recorded before anything is sent, in shadow mode too, so a switch
to live never resends (KTD8). Every step is wrapped: an exception logs and
the tick moves on.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from . import alert_mail, alert_settings, config, export, mail
from .alert_settings import BASELINES_AT_KEY, MODE_LIVE, MODE_SHADOW, AlertSettings
from .baselines import Baseline, compute_baseline, excluded_request_days, lookback_days
from .db import (
    ALERT_LIVE,
    ALERT_MORNING,
    SCHEMA_VERSION,
    SETUP_COMMANDS,
    STATUS_BREAKER,
    STATUS_QUEUED,
    STATUS_RECORDED,
    TIMESTAMP_FORMAT,
    Database,
    DatabaseMissing,
    OfflineError,
    _schema_version,
    device_ids_of,
)
from .hours import (
    DayResult,
    compute_day,
    counted_punches,
    read_day_start_hour,
    read_target_minutes,
    work_day,
    work_day_window,
)
from .punch_alerts import (
    HOLD,
    LiveAlert,
    has_unconfirmed_c3_guess,
    in_alert_hours,
    is_door_denied,
    live_decision,
    morning_issues,
)

_logger = logging.getLogger(__name__)

# The schema this service needs (v8: baselines, alerts, outbox, fix requests).
REQUIRED_SCHEMA = 8
TICK_SECONDS = 60
LIVE_INTERVAL = timedelta(minutes=5)
# KTD9 stale guard: the collector writes last_poll every cycle (60 s default).
STALE_AFTER = timedelta(minutes=10)
# KTD9 circuit breaker: more new candidates than this in one run, or more than
# this share of the people in that day, looks like an outage, not missed swipes.
BREAKER_MAX_PEOPLE = 5
BREAKER_SHARE = 0.20
# The share rule needs at least this many candidates, so one person at a tiny
# site (one of three in at lunch) never trips it on their own.
BREAKER_MIN_FOR_SHARE = 2
# Weekly job: Sunday (weekday 6) 02:00 IST.
WEEKLY_WEEKDAY = 6
WEEKLY_HOUR = 2
BASELINE_MAX_AGE = timedelta(days=6)
# The morning job re-checks a finished day at most this often, so late data
# is still picked up without a full reload every tick.
MORNING_RECHECK = timedelta(minutes=30)
# Report links live until the end of the work day + 7 days (KTD7).
REPORT_LINK_DAYS = 7
OUTBOX_BATCH = 50

# Settings this service writes for itself (and alert_settings.BASELINES_AT_KEY).
BREAKER_WARNED_KEY = "alerts_breaker_warned_{kind}"

Clock = Callable[[], datetime]
Sender = Callable[..., object]


def ist_now() -> datetime:
    """Naive IST, like db._now (containers run in UTC)."""
    return datetime.now(ZoneInfo(config.TIMEZONE_NAME)).replace(tzinfo=None, microsecond=0)


@dataclass(frozen=True)
class Candidate:
    employee: dict
    site: str | None
    detail: dict
    # (report_url, privacy_url) -> RenderedEmail
    render: Callable[[str, str], alert_mail.RenderedEmail]


@dataclass
class RunReport:
    """What one live or morning run did; ``skipped`` names why it did nothing."""

    skipped: str | None = None
    recorded: list[int] = field(default_factory=list)
    breaker: list[int] = field(default_factory=list)
    held: list[int] = field(default_factory=list)


class AlertsService:
    def __init__(
        self,
        database: Database,
        *,
        clock: Clock = ist_now,
        send: Sender | None = None,
        status_path: str | None = None,
    ):
        self.database = database
        self.clock = clock
        self._send = send
        self.status_path = status_path or config.collector_status_path()
        self.last_live_at: datetime | None = None
        self._schema_ready = False
        self._waiting: str | None = None
        self._url_warned: date | None = None
        # (work day, when) of the last morning run that held nobody back.
        self._morning_done: tuple[date, datetime] | None = None

    # --- The loop body -----------------------------------------------------

    def tick(self) -> None:
        now = self.clock()
        if not self._ready():
            return
        self._step("weekly baseline job", self.maybe_run_weekly, now)
        settings = self._step("reading alert settings", alert_settings.read, self.database)
        if settings is not None and settings.detecting:
            # A clock that stepped back never blocks the check until it catches up.
            if self.last_live_at is None or not timedelta(0) <= now - self.last_live_at < LIVE_INTERVAL:
                self.last_live_at = now
                self._step("live check", self.run_live, now, settings)
            self._step("morning check", self.run_morning, now, settings)
        self._step("outbox drain", self.drain_outbox)

    def _step(self, name: str, fn: Callable, *args):
        try:
            return fn(*args)
        except (OfflineError, DatabaseMissing) as exc:
            _logger.warning("%s skipped, database unavailable: %s", name, exc)
        except Exception:  # noqa: BLE001 - one step must never stop the others
            _logger.exception("%s failed; continuing", name)
        return None

    def _ready(self) -> bool:
        """KTD3: wait for schema v8 (the web or collector migrates; never here)."""
        if self._schema_ready:
            return True
        try:
            with self.database.connect() as connection:
                version = _schema_version(connection)
        except DatabaseMissing as exc:
            self._wait(f"database not set up ({exc}); waiting for one of: " + "; ".join(SETUP_COMMANDS))
            return False
        except Exception as exc:  # noqa: BLE001 - offline, busy: try next tick
            self._wait(f"database unavailable; retrying: {exc}")
            return False
        if version < REQUIRED_SCHEMA:
            self._wait(
                f"database schema is v{version}; waiting for v{REQUIRED_SCHEMA} "
                "(the web or collector migrates it)"
            )
            return False
        self._schema_ready = True
        _logger.info("alerts service ready (schema v%d)", version)
        return True

    def _wait(self, message: str) -> None:
        if message != self._waiting:
            _logger.warning(message)
            self._waiting = message

    # --- Weekly baselines and pruning (R1, U6) -----------------------------

    def weekly_due(self, now: datetime) -> bool:
        """True on first start (no snapshot, no recorded run), else when the
        newest run was over 6 days old at the latest Sunday 02:00 boundary,
        so the job stays on Sunday whenever the service first started."""
        ref = _latest(self.database.latest_baseline_at(), _parse_dt(self.database.setting(BASELINES_AT_KEY)))
        if ref is None:
            return True
        return ref < last_weekly_boundary(now) - BASELINE_MAX_AGE

    def maybe_run_weekly(self, now: datetime) -> bool:
        if not self.weekly_due(now):
            return False
        self.run_weekly(now)
        return True

    def run_weekly(self, now: datetime) -> int | None:
        """One snapshot for every active employee, then the prunes. Returns
        the snapshot id, None when there was nobody to snapshot."""
        day_start_hour = read_day_start_hour(self.database)
        today = work_day(now, day_start_hour)
        first, last = lookback_days(today)
        people = self.database.employees()
        snapshot = None
        if people:
            results = export.day_results(
                self.database, people, first, last,
                target_minutes=read_target_minutes(self.database), today=today, now=now,
                day_start_hour=day_start_hour,
            )
            requests = self.database.fix_request_days([p["id"] for p in people], first, last)
            per_person: dict[int, dict[date, DayResult]] = defaultdict(dict)
            for (person_id, day), result in results.items():
                per_person[person_id][day] = result
            statuses: dict[int, list[tuple[date, str]]] = defaultdict(list)
            for (person_id, day), values in requests.items():
                statuses[person_id].extend((day, status) for status in values)
            rows = []
            for person in people:
                baseline = compute_baseline(
                    per_person.get(person["id"], {}),
                    today=today,
                    request_days=excluded_request_days(statuses.get(person["id"], [])),
                    day_start_hour=day_start_hour,
                )
                rows.append({
                    "employee_id": person["id"],
                    "punched_days": baseline.punched_days,
                    "clean_days": baseline.clean_days,
                    "metrics": baseline.metrics(),
                })
            snapshot = self.database.record_baselines(rows)
            live = sum(1 for r in rows if r["metrics"] is not None)
            _logger.info(
                "baseline snapshot %s: %d people, %d with a live limit", snapshot, len(rows), live
            )
        # Recorded before pruning, so a failed prune never re-runs the snapshot.
        # Retention periods are the db defaults (db.BASELINE_SNAPSHOTS_KEPT etc.).
        self.database.set_setting(BASELINES_AT_KEY, now.strftime(TIMESTAMP_FORMAT))
        for name, prune in (
            ("baseline snapshots", self.database.prune_baselines),
            ("alerts", self.database.prune_alerts),
            ("outbox rows", self.database.prune_mail),
            ("report links", self.database.prune_report_links),
        ):
            removed = self._step(f"pruning {name}", prune)
            if removed:
                _logger.info("pruned %d old %s", removed, name)
        return snapshot

    # --- Stale guard (KTD9) ------------------------------------------------

    def stale_reason(self, now: datetime) -> str | None:
        """Why collector data cannot be trusted now, or None. Reads only the
        collector status file; device last_success is not used (the collector
        rewrites it only every 15 minutes when idle)."""
        try:
            with open(self.status_path, encoding="utf-8") as handle:
                status = json.load(handle)
        except (OSError, ValueError):
            return "collector status file missing or unreadable"
        if not isinstance(status, dict):
            return "collector status file unreadable"
        last_poll = _parse_dt(status.get("last_poll"))
        if last_poll is None:
            return "collector has not polled yet"
        if now - last_poll > STALE_AFTER:
            return f"collector last polled at {last_poll:%H:%M}"
        try:
            spool_depth = int(status.get("spool_depth") or 0)
        except (TypeError, ValueError):
            return "collector spool depth unreadable"
        if spool_depth > 0:
            return f"{spool_depth} spooled punch batch(es) not yet written"
        return None

    # --- Live check (R4, R6) -----------------------------------------------

    def run_live(self, now: datetime, settings: AlertSettings) -> RunReport:
        if not in_alert_hours(now, settings.start_hour, settings.end_hour):
            return RunReport("outside_alert_hours")
        stale = self.stale_reason(now)
        if stale:
            _logger.info("live check skipped: %s", stale)
            return RunReport("stale")
        day_start_hour = read_day_start_hour(self.database)
        today = work_day(now, day_start_hour)
        day_start, _ = work_day_window(today, day_start_hour)
        people = {p["id"]: p for p in self.database.employees()}
        grouped, results = self._load_day(
            people, today, day_start_hour=day_start_hour, now=now, is_today=True
        )
        present = [pid for pid, result in results.items() if result.status != "empty"]
        if not present:
            return RunReport()
        baselines = self.database.latest_baselines()
        requests = self.database.fix_request_days(present, today, today)
        already = {
            a["employee_id"] for a in self.database.alerts_for_day(today) if a["kind"] == ALERT_LIVE
        }
        candidates = []
        for pid in present:
            # A request or closing reply of any status suppresses the day.
            if pid in already or requests.get((pid, today)):
                continue
            try:
                baseline = _baseline(baselines.get(pid))
                if baseline is None or not baseline.live_eligible:
                    continue
                rows = grouped[pid]
                decision = live_decision(
                    results[pid], baseline, now=now, day_start=day_start,
                    alert_start_hour=settings.start_hour, alert_end_hour=settings.end_hour,
                    door_denied=[r["event_timestamp"] for r in rows if is_door_denied(r)],
                )
            except Exception:  # noqa: BLE001 - one person's data never stops the rest
                _logger.exception("live check for employee %s failed", pid)
                continue
            if isinstance(decision, LiveAlert):
                candidates.append(
                    _live_candidate(people[pid], rows, decision, baseline, today)
                )
        return self._record(ALERT_LIVE, today, candidates, len(present), settings, day_start_hour)

    # --- Morning check (R5, R8) --------------------------------------------

    def run_morning(self, now: datetime, settings: AlertSettings) -> RunReport:
        if now.hour < settings.morning_hour:
            return RunReport("before_morning_hour")
        stale = self.stale_reason(now)
        if stale:
            _logger.info("morning check skipped: %s", stale)
            return RunReport("stale")
        day_start_hour = read_day_start_hour(self.database)
        today = work_day(now, day_start_hour)
        day = today - timedelta(days=1)
        if self._morning_done is not None:
            done_day, done_at = self._morning_done
            # A clock that stepped back re-checks at once.
            if done_day == day and timedelta(0) <= now - done_at < MORNING_RECHECK:
                return RunReport("morning_done")
        report = self._morning(now, settings, day, today, day_start_hour)
        if not report.held:
            self._morning_done = (day, now)
        return report

    def _morning(
        self, now: datetime, settings: AlertSettings, day: date, today: date, day_start_hour: int
    ) -> RunReport:
        people = {p["id"]: p for p in self.database.employees()}
        grouped, results = self._load_day(
            people, day, day_start_hour=day_start_hour, now=None, is_today=False
        )
        present = [pid for pid, result in results.items() if result.status != "empty"]
        if not present:
            return RunReport()
        baselines = self.database.latest_baselines()
        requests = self.database.fix_request_days(present, day, day)
        already = {
            a["employee_id"] for a in self.database.alerts_for_day(day) if a["kind"] == ALERT_MORNING
        }
        eligible = []
        for pid in present:
            if pid in already or requests.get((pid, day)):
                continue
            try:
                baseline = _baseline(baselines.get(pid))
            except Exception:  # noqa: BLE001
                _logger.exception("baseline for employee %s unreadable", pid)
                continue
            if baseline is not None and baseline.morning_eligible:
                eligible.append(pid)
        if not eligible:
            return RunReport()
        raw = self._raw_rows(people, eligible, day, day_start_hour)
        report = RunReport()
        candidates = []
        for pid in eligible:
            try:
                issues = morning_issues(
                    results[pid], has_unconfirmed_c3_guess=has_unconfirmed_c3_guess(raw.get(pid, []))
                )
            except Exception:  # noqa: BLE001
                _logger.exception("morning check for employee %s failed", pid)
                continue
            if issues == HOLD:
                # Retried next tick; dropped once the job moves to the next day.
                report.held.append(pid)
                continue
            if issues:
                candidates.append(_morning_candidate(people[pid], grouped[pid], issues, day, today))
        recorded = self._record(ALERT_MORNING, day, candidates, len(present), settings, day_start_hour)
        recorded.held = report.held
        return recorded

    # --- Recording (KTD8, KTD9) --------------------------------------------

    def _record(
        self,
        kind: str,
        day: date,
        candidates: list[Candidate],
        present: int,
        settings: AlertSettings,
        day_start_hour: int,
    ) -> RunReport:
        report = RunReport()
        if not candidates:
            return report
        url = self._live_url(settings, day)
        mode = MODE_LIVE if url else MODE_SHADOW
        if breaker_tripped(len(candidates), present):
            for candidate in candidates:
                if self.database.record_alert(
                    employee_id=candidate.employee["id"], work_day=day, kind=kind, mode=mode,
                    status=STATUS_BREAKER, site=candidate.site, detail=candidate.detail,
                ):
                    report.breaker.append(candidate.employee["id"])
            self._warn_breaker(kind, day, len(candidates), present)
            return report
        for candidate in candidates:
            try:
                if self._record_one(kind, day, candidate, url, mode, day_start_hour):
                    report.recorded.append(candidate.employee["id"])
            except Exception:  # noqa: BLE001 - one person's alert never stops the rest
                _logger.exception("recording %s alert for employee %s failed",
                                  kind, candidate.employee["id"])
        return report

    def _record_one(
        self, kind: str, day: date, candidate: Candidate, url: str | None, mode: str,
        day_start_hour: int,
    ) -> bool:
        """``mode`` is live exactly when ``url`` is set (``_record``)."""
        employee = candidate.employee
        email = (employee.get("email") or "").strip()
        if url is None or not email:
            if url is not None:
                _logger.warning("%s alert for employee %s not emailed: no email address",
                                kind, employee["id"])
            recorded = self.database.record_alert(
                employee_id=employee["id"], work_day=day, kind=kind,
                mode=mode, status=STATUS_RECORDED,
                site=candidate.site, detail=candidate.detail,
            )
            if recorded and url is None:
                _logger.info("shadow %s alert: employee %s (%s) on %s: %s", kind, employee["id"],
                             employee.get("name"), day.isoformat(), json.dumps(candidate.detail))
            return recorded
        expires_at = work_day_window(day, day_start_hour)[1] + timedelta(days=REPORT_LINK_DAYS)
        token = self.database.create_report_link(employee["id"], day, expires_at=expires_at)
        rendered = candidate.render(f"{url}/fix/{token}", f"{url}/privacy")
        recorded = self.database.record_alert(
            employee_id=employee["id"], work_day=day, kind=kind, mode=mode, status=STATUS_QUEUED,
            site=candidate.site, detail=candidate.detail,
            mail={
                "recipients": [email],
                "subject": rendered.subject,
                "text_body": rendered.text,
                "html_body": rendered.html,
            },
        )
        if recorded:
            _logger.info("%s alert queued for employee %s on %s", kind, employee["id"], day.isoformat())
        return recorded

    def _live_url(self, settings: AlertSettings, day: date) -> str | None:
        """The public URL when live mode can send; None means behave as shadow.
        Live mode without ESSL_PUBLIC_URL logs once per day and records only."""
        if settings.mode != MODE_LIVE:
            return None
        try:
            url = config.public_url()
        except RuntimeError as exc:
            url, why = None, str(exc)
        else:
            why = "ESSL_PUBLIC_URL is not set"
        if url is None and self._url_warned != day:
            self._url_warned = day
            _logger.warning("alerts mode is live but %s; recording as shadow, sending nothing", why)
        return url

    def _warn_breaker(self, kind: str, day: date, count: int, present: int) -> None:
        key = BREAKER_WARNED_KEY.format(kind=kind)
        if self.database.setting(key) == day.isoformat():
            return
        _logger.warning(
            "%s check: %d people would be alerted out of %d in on %s; looks like a data "
            "problem, so they were recorded as breaker and nothing was sent",
            kind, count, present, day.isoformat(),
        )
        self.database.set_setting(key, day.isoformat())

    # --- Outbox (KTD8) -----------------------------------------------------

    def drain_outbox(self) -> tuple[int, int]:
        """Send every due outbox row; returns (sent, failed attempts). One
        failure never stops the batch."""
        send = self._send or mail.send_email
        reply_to = config.hr_reply_to()
        sent = failed = 0
        for row in self.database.due_mail(limit=OUTBOX_BATCH):
            try:
                send(
                    row["subject"], list(row["recipients"]), row["text_body"] or "",
                    row["html_body"], reply_to=reply_to,
                )
            except Exception as exc:  # noqa: BLE001 - provider errors of every kind
                failed += 1
                error = str(exc).strip() or type(exc).__name__
                try:
                    status = self.database.mark_mail_failed(row["id"], error)
                except Exception:  # noqa: BLE001
                    _logger.exception("could not record failed send of outbox row %s", row["id"])
                    continue
                _logger.warning("sending outbox row %s failed (%s): %s", row["id"], status, error)
                continue
            sent += 1
            try:
                self.database.mark_mail_sent(row["id"])
            except Exception:  # noqa: BLE001
                _logger.exception("outbox row %s sent but not marked; it may resend", row["id"])
        return sent, failed

    # --- Loading -----------------------------------------------------------

    def _load_day(
        self,
        people: dict[int, dict],
        day: date,
        *,
        day_start_hour: int,
        now: datetime | None,
        is_today: bool,
    ) -> tuple[dict[int, list[dict]], dict[int, DayResult]]:
        """Every active person's merged rows and DayResult for one work day,
        from one punch query, the way export._load and the day views score
        it: rows under any of a person's IDs are theirs, ignored rows never
        count. Only people with at least one row are returned."""
        owner = {uid: pid for pid, p in people.items() for uid in device_ids_of(p)}
        start, end = work_day_window(day, day_start_hour)
        grouped: dict[int, list[dict]] = defaultdict(list)
        for row in self.database.punches_for_device_users_range(list(owner), start, end):
            pid = owner.get(str(row["device_user_id"]))
            if pid is not None:
                grouped[pid].append(row)
        windows = self.database.device_windows()
        target = read_target_minutes(self.database)
        results = {}
        for pid, rows in grouped.items():
            rows.sort(key=lambda r: r["event_timestamp"])
            results[pid] = compute_day(
                counted_punches(rows, windows), target_minutes=target, now=now, is_today=is_today
            )
        return grouped, results

    def _raw_rows(
        self, people: dict[int, dict], ids: list[int], day: date, day_start_hour: int
    ) -> dict[int, list[dict]]:
        owner = {uid: pid for pid in ids for uid in device_ids_of(people[pid])}
        start, end = work_day_window(day, day_start_hour)
        raw: dict[int, list[dict]] = defaultdict(list)
        for row in self.database.raw_punches_for_device_users_range(list(owner), start, end):
            pid = owner.get(str(row["device_user_id"]))
            if pid is not None:
                raw[pid].append(row)
        return raw


# --- Pure helpers ------------------------------------------------------------


def breaker_tripped(new_candidates: int, present: int) -> bool:
    """KTD9: more than BREAKER_MAX_PEOPLE new candidates, or more than
    BREAKER_SHARE of the people in that day (from BREAKER_MIN_FOR_SHARE up)."""
    if new_candidates > BREAKER_MAX_PEOPLE:
        return True
    return new_candidates >= BREAKER_MIN_FOR_SHARE and new_candidates > BREAKER_SHARE * present


def last_weekly_boundary(now: datetime) -> datetime:
    """The latest Sunday 02:00 at or before ``now``."""
    days_back = (now.weekday() - WEEKLY_WEEKDAY) % 7
    boundary = (now - timedelta(days=days_back)).replace(
        hour=WEEKLY_HOUR, minute=0, second=0, microsecond=0
    )
    if boundary > now:
        boundary -= timedelta(days=7)
    return boundary


def _live_candidate(
    employee: dict, rows: list[dict], alert: LiveAlert, baseline: Baseline, day: date
) -> Candidate:
    detail = {
        "reason": "out_too_long",
        "out_since": alert.out_since.strftime(TIMESTAMP_FORMAT),
        "out_minutes": alert.out_minutes,
        "limit": baseline.limit_minutes,
    }

    def render(report_url: str, privacy_url: str) -> alert_mail.RenderedEmail:
        return alert_mail.live_check(
            employee_name=str(employee.get("name") or ""), work_day=day,
            out_at=alert.out_since, out_minutes=alert.out_minutes,
            report_url=report_url, privacy_url=privacy_url,
        )

    return Candidate(employee, _latest_site(rows), detail, render)


def _morning_candidate(
    employee: dict, rows: list[dict], issues: list, day: date, sent_on: date
) -> Candidate:
    detail = {
        "reason": "sequence",
        "issues": [
            {
                "kind": issue.kind,
                "start": _fmt(issue.start),
                "end": _fmt(issue.end),
            }
            for issue in issues
        ],
    }

    def render(report_url: str, privacy_url: str) -> alert_mail.RenderedEmail:
        return alert_mail.morning_check(
            employee_name=str(employee.get("name") or ""), work_day=day, issues=issues,
            report_url=report_url, privacy_url=privacy_url, sent_on=sent_on,
        )

    return Candidate(employee, _latest_site(rows), detail, render)


def _latest_site(rows: list[dict]) -> str | None:
    """The site of the person's latest punch that day (added punches carry none)."""
    for row in sorted(rows, key=lambda r: r["event_timestamp"], reverse=True):
        if row.get("site"):
            return str(row["site"])
    return None


def _baseline(stored: dict | None) -> Baseline | None:
    if stored is None:
        return None
    return Baseline.from_stored(stored["punched_days"], stored["clean_days"], stored.get("metrics"))


def _fmt(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.strftime(TIMESTAMP_FORMAT)
    return None if value is None else str(value)


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo(config.TIMEZONE_NAME)).replace(tzinfo=None)
    return parsed


def _latest(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


# --- Entry point -------------------------------------------------------------


def run_loop(
    service: AlertsService,
    *,
    sleep: Callable[[float], None] = time.sleep,
    tick_seconds: int = TICK_SECONDS,
    ticks: int | None = None,
) -> None:
    """Tick forever (or ``ticks`` times, for tests), like backup.run_loop."""
    done = 0
    while ticks is None or done < ticks:
        started = time.monotonic()
        try:
            service.tick()
        except Exception:  # noqa: BLE001 - the loop must survive anything
            _logger.exception("alerts tick crashed")
        done += 1
        sleep(max(1.0, tick_seconds - (time.monotonic() - started)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if SCHEMA_VERSION < REQUIRED_SCHEMA:  # pragma: no cover - a packaging error
        raise SystemExit(f"this build knows schema v{SCHEMA_VERSION}; alerts need v{REQUIRED_SCHEMA}")
    # KTD3/KTD5: never create or migrate the shared database from here.
    run_loop(AlertsService(Database(must_exist=True)))


if __name__ == "__main__":
    main()
