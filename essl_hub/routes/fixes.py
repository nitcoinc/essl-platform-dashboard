"""Employee fix reports (U7): the emailed report link, the signed-in report
page for one's own recent days, and the privacy notice.

A report link (KTD7) binds one employee and one work day; it never creates a
session, and GET/HEAD only render because mail scanners pre-fetch links. Every
POST is rate-limited per client IP and per link.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time as _clock
from collections import deque
from datetime import date, datetime, time, timedelta

from flask import Blueprint, Response, abort, current_app, g, redirect, render_template, request, url_for

from .. import alert_mail, config
from ..day_story import punch_flags
from ..db import FIX_MAX_LINES, FIX_NOTE_MAX_CHARS, FixLineLimit, RuleViolation, device_ids_of
from ..hashing import sha256_hex
from ..hours import CORRECTION_ADDED, CORRECTION_DIRECTION, CORRECTION_IGNORED, work_day, work_day_window
from . import common
from .common import _client_ip, _day_start_hour, _day_start_label, _login_required, _parse_date, _require_csrf, database

bp = Blueprint("fixes", __name__)

_logger = logging.getLogger(__name__)

# A work day can be reported while it is one of the last REPORT_DAYS work
# days, today included: the same span a report link lives (KTD7).
REPORT_DAYS = 7
ADD_ROWS = 3  # blank "Add a missed swipe" rows on a fresh form

IP_POSTS_PER_MINUTE = 10
LINK_POSTS_PER_HOUR = 20

CLOSING_ACTIONS = {"correct": "closed_correct", "left": "closed_left"}
DECIDED = ("approved", "partial", "rejected")
CLOSED = ("closed_correct", "closed_left")

_CORRECTION_CHIPS = {
    CORRECTION_ADDED: "added by HR",
    CORRECTION_DIRECTION: "direction fixed",
    CORRECTION_IGNORED: "ignored",
}
_DIRECTION_LABELS = {"entry": "In", "exit": "Out"}
_EMAIL_KINDS = {
    "add_in": alert_mail.MISSED_IN,
    "add_out": alert_mail.MISSED_OUT,
    "flip": alert_mail.FLIP,
    "ignore": alert_mail.IGNORE,
}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# --- Log hygiene (KTD7) ------------------------------------------------------

_TOKEN_PATH = re.compile(r"/fix/[^/\s?\"']+")


class _RedactReportTokens(logging.Filter):
    """Access logs (werkzeug's dev server, gunicorn when an access log is
    configured) print request paths; a report token must never land there."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a malformed record is not ours to fix
            return True
        if "/fix/" in message:
            record.msg = _TOKEN_PATH.sub("/fix/[redacted]", message)
            record.args = ()
        return True


for _name in ("werkzeug", "gunicorn.access"):
    logging.getLogger(_name).addFilter(_RedactReportTokens())


# --- Rate limits -------------------------------------------------------------


class _SlidingWindow:
    """At most ``limit`` hits per key in any ``seconds`` span, in memory.

    The web runs one gunicorn worker, so one process sees every POST; a
    restart forgets the counts, which only ever loosens the limit."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit = limit
        self.seconds = seconds
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = _clock.monotonic()
        cutoff = now - self.seconds
        with self._lock:
            if len(self._hits) > 10_000:
                for stale in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
                    del self._hits[stale]
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


_limits_lock = threading.Lock()


def _limits() -> tuple[_SlidingWindow, _SlidingWindow]:
    with _limits_lock:
        return current_app.extensions.setdefault(
            "essl_fix_report_limits",
            (_SlidingWindow(IP_POSTS_PER_MINUTE, 60), _SlidingWindow(LINK_POSTS_PER_HOUR, 3600)),
        )


def _too_many() -> Response:
    response = Response(render_template("fix_link_expired.html", limited=True), 429)
    response.headers["Retry-After"] = "60"
    return response


# --- Day data ----------------------------------------------------------------


def punch_set_hash(rows) -> str:
    """KTD6: a hash of the day's device punches (fingerprint and direction),
    so review can flag "day changed since report". HR-added rows are not
    device punches and are left out."""
    pairs = sorted(
        (str(row.get("fingerprint")), str(row.get("trusted_direction") or ""))
        for row in rows
        if row.get("correction") != CORRECTION_ADDED
    )
    return sha256_hex(json.dumps(pairs))


def _day_rows(employee: dict, day: date, day_start_hour: int) -> list[dict]:
    start, end = work_day_window(day, day_start_hour)
    return database.punches_for_employee_range(device_ids_of(employee), start, end)


def _actionable(row: dict) -> bool:
    """A device punch the employee may flip or ignore; HR-added and already
    ignored rows are shown but cannot be targeted."""
    return row.get("correction") not in (CORRECTION_ADDED, CORRECTION_IGNORED)


def _swipes(rows: list[dict], day: date) -> list[dict]:
    swipes = []
    for row in rows:
        correction = row.get("correction")
        chips = [_CORRECTION_CHIPS[correction]] if correction in _CORRECTION_CHIPS else []
        chips.extend(punch_flags(row.get("raw_state")))
        at = row["event_timestamp"]
        swipes.append({
            "fingerprint": row.get("fingerprint"),
            "time": at,
            "next_day": at.date() > day,
            "direction": _DIRECTION_LABELS.get(row.get("trusted_direction"), "Swipe"),
            "chips": chips,
            "actionable": _actionable(row),
        })
    return swipes


def _reportable(day: date, today: date) -> bool:
    return 0 <= (today - day).days < REPORT_DAYS


def _line_view(line: dict, times: dict) -> dict:
    """One filed line as text plus a status chip."""
    at = line.get("hr_event_time") or line.get("event_time") or times.get(line.get("target_fingerprint"))
    direction = {"entry": "in", "exit": "out"}.get(line.get("hr_direction") or line.get("direction"))
    status = line.get("status") or "pending"
    fix = alert_mail.FixLine(
        kind=_EMAIL_KINDS.get(line["kind"], line["kind"]), at=at, direction=direction,
        approved=None if status == "pending" else status == "approved",
    )
    return {"text": alert_mail.line_sentence(fix), "status": status, "fix": fix, "note": line.get("note")}


# --- Form --------------------------------------------------------------------


def _blank_form() -> dict:
    return {
        "flips": set(), "ignores": set(), "note": "",
        "adds": [{"time": "", "dir": "", "error": None, "dir_error": None} for _ in range(ADD_ROWS)],
    }


def _read_form(form) -> dict:
    adds = []
    for i in range(FIX_MAX_LINES):
        adds.append({
            "time": (form.get(f"add-{i}-time") or "").strip(),
            "dir": (form.get(f"add-{i}-dir") or "").strip(),
            "error": None, "dir_error": None,
        })
    # Keep what was typed, drop trailing blank rows, then pad to the default.
    while adds and not adds[-1]["time"] and not adds[-1]["dir"]:
        adds.pop()
    while len(adds) < ADD_ROWS:
        adds.append({"time": "", "dir": "", "error": None, "dir_error": None})
    return {
        "flips": set(form.getlist("flip")),
        "ignores": set(form.getlist("ignore")),
        "note": form.get("note") or "",
        "adds": adds,
    }


def _missed_at(value: str, day: date, day_start_hour: int) -> datetime | None:
    """HH:MM on work day ``day``: a time before the day-start hour is after
    midnight, so it falls on the next calendar date of the same work day."""
    match = _TIME_RE.match(value)
    if not match:
        return None
    at = datetime.combine(day, time(int(match.group(1)), int(match.group(2))))
    if at.hour < day_start_hour:
        at += timedelta(days=1)
    return at


def _validate(form: dict, action: str, rows: list[dict], day: date, day_start_hour: int,
              now: datetime) -> tuple[list[dict], dict]:
    """The fix lines a submission asks for, and any form errors."""
    errors: dict[str, str] = {}
    start, end = work_day_window(day, day_start_hour)
    lines: list[dict] = []
    for row in form["adds"]:
        if not row["time"] and not row["dir"]:
            continue
        if not row["time"]:
            row["error"] = "Enter the time of the missed swipe, like 13:40."
            continue
        at = _missed_at(row["time"], day, day_start_hour)
        if at is None:
            row["error"] = "Enter a time like 13:40."
        elif not start <= at < end:
            row["error"] = f"Enter a time inside this work day ({start:%H:%M} to {end:%H:%M} next day)."
        elif at > now:
            row["error"] = "That time hasn't happened yet. Enter a time that has passed."
        if row["dir"] not in ("in", "out"):
            row["dir_error"] = "Choose In or Out."
        if row["error"] or row["dir_error"]:
            continue
        lines.append({"kind": "add_in" if row["dir"] == "in" else "add_out", "event_time": at})
    if any(row["error"] or row["dir_error"] for row in form["adds"]):
        errors["adds"] = "Check the missed swipes below."

    by_fingerprint = {row.get("fingerprint"): row for row in rows if _actionable(row)}
    picked = form["flips"] | form["ignores"]
    if any(fp not in by_fingerprint for fp in picked):
        errors["swipes"] = "One of the swipes you picked isn't on this day any more. Reload the page and try again."
    elif form["flips"] & form["ignores"]:
        errors["swipes"] = "For each swipe, pick either \"other direction\" or \"ignore\", not both."
    else:
        for fp in sorted(picked, key=lambda f: by_fingerprint[f]["event_timestamp"]):
            row = by_fingerprint[fp]
            if fp in form["flips"]:
                direction = {"entry": "exit", "exit": "entry"}.get(row.get("trusted_direction"))
                lines.append({"kind": "flip", "target_fingerprint": fp,
                              "event_time": row["event_timestamp"], "direction": direction})
            else:
                lines.append({"kind": "ignore", "target_fingerprint": fp,
                              "event_time": row["event_timestamp"]})

    note = form["note"].strip()
    if len(note) > FIX_NOTE_MAX_CHARS:
        errors["note"] = f"Keep the note to {FIX_NOTE_MAX_CHARS} characters (it has {len(note)})."

    asked = len(lines) + sum(1 for r in form["adds"] if r["error"] or r["dir_error"])
    if action in CLOSING_ACTIONS:
        if asked or picked:
            errors["form"] = (
                "You've also asked for changes. To close the day, clear the changes; "
                "to have them fixed, press Send to HR."
            )
    elif not errors and not lines:
        errors["form"] = "Add a missed swipe, or tick a swipe to change, then press Send to HR."
    elif len(lines) > FIX_MAX_LINES:
        errors["form"] = f"One report can hold at most {FIX_MAX_LINES} changes."
    if lines and note:
        lines[0]["note"] = note
    return lines, errors


# --- Notifications (R11, KTD6) ------------------------------------------------


def _review_url(request_id: int) -> str | None:
    try:
        base = config.public_url()
    except RuntimeError as exc:
        _logger.warning("Fix request %s: %s; HR email sent without a review link", request_id, exc)
        return None
    return f"{base}/fixes/review/{request_id}" if base else None


def _notify_new_request(employee: dict, day: date, request_id: int, note: str, rows: list[dict]) -> None:
    """One HR email (admins too when no other HR user exists) and one manager
    FYI for a request's first lines; later lines add no email (KTD5)."""
    filed = database.fix_request(request_id)
    times = {row.get("fingerprint"): row["event_timestamp"] for row in rows}
    lines = [_line_view(line, times)["fix"] for line in (filed or {}).get("lines", [])]
    requester = employee["id"]
    approvers = [p for p in database.active_hr_users() if p["id"] != requester]
    if not approvers:
        approvers = [p for p in database.active_admins() if p["id"] != requester]
    manager = None
    if employee.get("manager_id") and employee["manager_id"] != requester:
        manager = database.employee_by_id(employee["manager_id"])
    hr_emails, manager_emails = alert_mail.request_recipients(
        manager["email"] if manager else None, [p["email"] for p in approvers]
    )
    if hr_emails:
        email = alert_mail.new_request(
            employee_name=employee["name"], work_day=day, lines=lines, note=note or None,
            review_url=_review_url(request_id),
        )
        database.enqueue_mail(hr_emails, email.subject, email.text, email.html)
    else:
        _logger.warning("Fix request %s: no HR user or admin to notify", request_id)
    if manager_emails:
        email = alert_mail.new_request(
            employee_name=employee["name"], work_day=day, lines=lines, note=note or None,
        )
        database.enqueue_mail(manager_emails, email.subject, email.text, email.html)


# --- Rendering ---------------------------------------------------------------


def _render(employee: dict, day: date, *, token: str | None = None, form: dict | None = None,
            errors: dict | None = None, status: int = 200, template: str = "fix_report.html") -> Response:
    day_start_hour = _day_start_hour(database)
    rows = _day_rows(employee, day, day_start_hour)
    times = {row.get("fingerprint"): row["event_timestamp"] for row in rows}
    latest = database.latest_fix_request(employee["id"], day)
    filed = database.fix_request(latest["id"]) if latest else None
    state = None
    if filed is not None:
        state = ("pending" if filed["status"] == "pending"
                 else "closed" if filed["status"] in CLOSED else "decided")
    if token is not None:
        page_url = url_for("fixes.link_view", token=token)
        action_url = url_for("fixes.link_submit", token=token)
    else:
        page_url = url_for("fixes.report_view", day=day.isoformat())
        action_url = url_for("fixes.report_submit", day=day.isoformat())
    show_form = form is not None or state in (None, "pending") or request.args.get("report") == "1"
    response = Response(render_template(
        template,
        layout="_auth_base.html" if token is not None else "base.html",
        signed_in=token is None,
        employee=g.employee if token is None else None,
        active="day",
        person=employee,
        day=day,
        day_label=alert_mail.fmt_day(day),
        day_start=_day_start_label(day_start_hour),
        swipes=_swipes(rows, day),
        filed=filed,
        state=state,
        lines=[_line_view(line, times) for line in (filed or {}).get("lines", [])],
        show_form=show_form,
        allow_close=state in (None, "closed"),
        form=form or _blank_form(),
        errors=errors or {},
        page_url=page_url,
        action_url=action_url,
        csrf_token=g.csrf_token if token is None else None,
        max_lines=FIX_MAX_LINES,
        note_max=FIX_NOTE_MAX_CHARS,
    ), status)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _submit(employee: dict, day: date, *, source: str, token: str | None, done_url: str) -> Response:
    """Validate and file one POST; the caller has checked who may file."""
    action = request.form.get("action") or "send"
    if action not in ("send", *CLOSING_ACTIONS):
        abort(400)
    day_start_hour = _day_start_hour(database)
    rows = _day_rows(employee, day, day_start_hour)
    form = _read_form(request.form)
    lines, errors = _validate(form, action, rows, day, day_start_hour, common._now_naive())
    if errors:
        return _render(employee, day, token=token, form=form, errors=errors, status=400)
    if action in CLOSING_ACTIONS:
        try:
            database.close_fix_day(employee["id"], day, CLOSING_ACTIONS[action], source=source)
        except RuleViolation:
            errors["form"] = ("Your report for this day is waiting for HR, so the day can't be "
                              "closed. You can still add changes to it.")
            return _render(employee, day, token=token, form=form, errors=errors, status=400)
        return redirect(done_url)
    try:
        result = database.file_fix_lines(
            employee["id"], day, lines, source=source, punch_set_hash=punch_set_hash(rows)
        )
    except FixLineLimit:
        filed = database.pending_fix_request(employee["id"], day)
        have = len(database.fix_request(filed["id"])["lines"]) if filed else 0
        errors["form"] = (f"One report can hold at most {FIX_MAX_LINES} changes, and this day's "
                          f"report already has {have}. Add the most important ones, or tell HR in the note.")
        return _render(employee, day, token=token, form=form, errors=errors, status=400)
    if result["first_lines"]:
        _notify_new_request(employee, day, result["request_id"], form["note"].strip(), rows)
    return redirect(done_url)


def _today() -> date:
    return work_day(common._now_naive(), _day_start_hour(database))


# --- Report link (no session) ------------------------------------------------


def _expired() -> Response:
    response = Response(render_template("fix_link_expired.html", limited=False), 404)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _link_target(token: str) -> tuple[dict, date] | None:
    """The employee and work day a live link opens, or None. A link whose
    day has aged out, or whose employee is archived, is as dead as an
    expired one."""
    link = database.report_link(token)
    if link is None:
        return None
    employee = database.employee_by_id(link["employee_id"])
    day = link["work_day"]
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if employee is None or not _reportable(day, _today()):
        return None
    return employee, day


@bp.get("/fix/<token>")
def link_view(token: str) -> Response:
    target = _link_target(token)
    if target is None:
        return _expired()
    employee, day = target
    if request.args.get("sent") == "1":
        return _render(employee, day, token=token, template="fix_report_done.html")
    return _render(employee, day, token=token)


@bp.post("/fix/<token>")
def link_submit(token: str) -> Response:
    ip_limit, link_limit = _limits()
    if not ip_limit.allow(f"ip:{_client_ip()}"):
        return _too_many()
    target = _link_target(token)
    if target is None:
        return _expired()
    if not link_limit.allow(f"link:{sha256_hex(token)}"):
        return _too_many()
    employee, day = target
    return _submit(employee, day, source="link", token=token,
                   done_url=url_for("fixes.link_view", token=token, sent=1))


# --- Signed-in report (own recent days only) -----------------------------------


def _own_day(raw: str) -> date:
    """The signed-in person's reportable day, or 404. An ``employee`` in the
    request naming someone else is refused outright (403); nobody reports for
    another person here, whatever their role."""
    other = request.values.get("employee")
    if other not in (None, "") and other != str(g.employee["id"]):
        abort(403)
    day = _parse_date(raw)
    if day is None or not _reportable(day, _today()):
        abort(404)
    return day


@bp.get("/day/<day>/report")
@_login_required
def report_view(day: str) -> Response:
    selected = _own_day(day)
    if request.args.get("sent") == "1":
        return _render(g.employee, selected, template="fix_report_done.html")
    return _render(g.employee, selected)


@bp.post("/day/<day>/report")
@_login_required
def report_submit(day: str) -> Response:
    _require_csrf()
    selected = _own_day(day)
    ip_limit, _ = _limits()
    if not ip_limit.allow(f"ip:{_client_ip()}"):
        return _too_many()
    return _submit(g.employee, selected, source="session", token=None,
                   done_url=url_for("fixes.report_view", day=selected.isoformat(), sent=1))


# --- Privacy notice ------------------------------------------------------------


@bp.get("/privacy")
def privacy() -> str:
    return render_template("privacy.html")
