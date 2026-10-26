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

from flask import Blueprint, Response, abort, current_app, g, make_response, redirect, render_template, request, url_for

from .. import access, alert_mail, config
from ..day_story import CORRECTION_FLAGS, punch_flags
from ..db import (
    FIX_ADD_KINDS,
    FIX_CLOSED_STATUSES,
    FIX_DECIDED_STATUSES,
    FIX_MAX_LINES,
    FIX_NOTE_MAX_CHARS,
    OPPOSITE_DIRECTION,
    FixLineLimit,
    RuleViolation,
    device_ids_of,
)
from ..hashing import sha256_hex
from ..hours import (
    CORRECTION_ADDED,
    CORRECTION_DIRECTION,
    CORRECTION_IGNORED,
    apply_corrections,
    compute_day,
    counted_punches,
    reportable,
    work_day,
    work_day_window,
)
from . import common
from .common import (
    _client_ip,
    _day_start_hour,
    _day_start_label,
    _login_required,
    _parse_date,
    _require_csrf,
    _role_required,
    _target_minutes,
    _today,
    database,
)
from .people import _FLASH_COOKIE, _redirect_with_flash, _take_flash

bp = Blueprint("fixes", __name__)

_logger = logging.getLogger(__name__)

ADD_ROWS = 3  # blank "Add a missed swipe" rows on a fresh form

IP_POSTS_PER_MINUTE = 10
LINK_POSTS_PER_HOUR = 20

CLOSING_ACTIONS = {"correct": "closed_correct", "left": "closed_left"}

# Chip tones for a swipe's correction (day_story.CORRECTION_FLAGS labels);
# every other chip on a swipe is a device flag and reads as a warning.
_CORRECTION_TONES = {
    CORRECTION_ADDED: "info",
    CORRECTION_DIRECTION: "info",
    CORRECTION_IGNORED: "neutral",
}
_DIRECTION_LABELS = {"entry": "In", "exit": "Out"}
# Form and email values ("in"/"out") to stored directions, and back.
_TO_DIRECTION = {"in": "entry", "out": "exit"}
_FROM_DIRECTION = {value: key for key, value in _TO_DIRECTION.items()}
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
        limits = current_app.extensions.get("essl_fix_report_limits")
        if limits is None:
            limits = (_SlidingWindow(IP_POSTS_PER_MINUTE, 60), _SlidingWindow(LINK_POSTS_PER_HOUR, 3600))
            current_app.extensions["essl_fix_report_limits"] = limits
        return limits


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
    """Each swipe for the list, its chips as (label, tone) pairs."""
    swipes = []
    for row in rows:
        correction = row.get("correction")
        chips = []
        if correction in CORRECTION_FLAGS:
            chips.append((CORRECTION_FLAGS[correction], _CORRECTION_TONES[correction]))
        chips.extend((flag, "warn") for flag in punch_flags(row.get("raw_state")))
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


def _line_view(line: dict, times: dict) -> dict:
    """One filed line as text plus a status chip."""
    at = line.get("hr_event_time") or line.get("event_time") or times.get(line.get("target_fingerprint"))
    stored = line.get("hr_direction") or line.get("direction")
    direction = _FROM_DIRECTION.get(stored)
    kind = line["kind"]
    if kind in FIX_ADD_KINDS and stored in ("entry", "exit"):
        # HR may turn a missed IN into a missed OUT before approving.
        kind = "add_in" if stored == "entry" else "add_out"
    status = line.get("status") or "pending"
    fix = alert_mail.FixLine(
        kind=_EMAIL_KINDS.get(kind, kind), at=at, direction=direction,
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


def _check_missed_time(raw: str, day: date, day_start_hour: int,
                       now: datetime) -> tuple[datetime | None, str | None]:
    """A typed missed-swipe time on work day ``day``: (time, None) when it is
    usable, else (None, the message to show)."""
    if not raw:
        return None, "Enter the time of the missed swipe, like 13:40."
    at = _missed_at(raw, day, day_start_hour)
    if at is None:
        return None, "Enter a time like 13:40."
    start, end = work_day_window(day, day_start_hour)
    if not start <= at < end:
        return None, f"Enter a time inside this work day ({start:%H:%M} to {end:%H:%M} next day)."
    if at > now:
        return None, "That time hasn't happened yet. Enter a time that has passed."
    return at, None


def _validate(form: dict, action: str, rows: list[dict], day: date, day_start_hour: int,
              now: datetime) -> tuple[list[dict], dict]:
    """The fix lines a submission asks for, and any form errors."""
    errors: dict[str, str] = {}
    lines: list[dict] = []
    for row in form["adds"]:
        if not row["time"] and not row["dir"]:
            continue
        at, row["error"] = _check_missed_time(row["time"], day, day_start_hour, now)
        if not row["time"]:
            continue  # the direction is checked once there is a time
        if row["dir"] not in _TO_DIRECTION:
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
                direction = OPPOSITE_DIRECTION.get(row.get("trusted_direction"))
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
    base = config.public_url_or_none(f"Fix request {request_id}: HR email sent without a review link")
    return f"{base}/fixes/review/{request_id}" if base else None


def _notify_new_request(employee: dict, day: date, request_id: int, note: str, rows: list[dict]) -> None:
    """One HR email (admins too when no other HR user exists) and one manager
    FYI for a request's first lines; later lines add no email (KTD5)."""
    filed = database.fix_request(request_id)
    times = {row.get("fingerprint"): row["event_timestamp"] for row in rows}
    lines = [_line_view(line, times)["fix"] for line in (filed or {}).get("lines", [])]
    requester = employee["id"]
    approvers = access.fix_approvers(requester, database)
    manager = None
    if employee.get("manager_id") and employee["manager_id"] != requester:
        manager = database.employee_by_id(employee["manager_id"])
    hr_emails, manager_emails = alert_mail.request_recipients(
        manager["email"] if manager else None, [p["email"] for p in approvers]
    )
    if hr_emails:
        email = alert_mail.new_request(
            employee_name=employee["name"], work_day=day, lines=lines, note=note or None,
            review_url=_review_url(request_id), for_hr=True,
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
                 else "closed" if filed["status"] in FIX_CLOSED_STATUSES else "decided")
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
        try:
            _notify_new_request(employee, day, result["request_id"], form["note"].strip(), rows)
        except Exception:  # noqa: BLE001 - the request is saved; a lost email must not 500 it
            _logger.exception("Fix request %s saved but its notification failed", result["request_id"])
    return redirect(done_url)


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
    if employee is None or not reportable(day, _today()):
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
    if day is None or not reportable(day, _today()):
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


# --- HR review (U8, R12-R14) ----------------------------------------------------

_REVIEW_ROLES = access.FIX_REVIEW_ROLES
_TARGET_KINDS = ("flip", "ignore")


def _review_request(request_id: int) -> tuple[dict, dict, date]:
    """The request, its employee (archived too: past days stay reviewable)
    and its work day, or 404."""
    filed = database.fix_request(request_id)
    if filed is None:
        abort(404)
    person = database.employee_by_id(filed["employee_id"], include_archived=True)
    if person is None:
        abort(404)
    return filed, person, filed["work_day"]


def _readonly_reason(filed: dict) -> str | None:
    """Why the viewer may look but not decide (KTD6), or None."""
    if filed["employee_id"] == g.employee["id"]:
        return "This is your own request. Another HR user decides it; nobody approves their own fixes."
    if g.employee["role"] == "admin":
        return ("An HR user decides fix requests. Admins approve only when no HR user "
                "other than the requester exists.")
    return None


def _default_direction(line: dict, rows_by_fp: dict) -> str | None:
    """The direction a line asks for, as "in"/"out": an added swipe's own, or
    the one a flip turns its swipe into."""
    if line.get("direction"):
        return _FROM_DIRECTION.get(line["direction"])
    if line["kind"] == "flip":
        row = rows_by_fp.get(line.get("target_fingerprint"))
        current = row.get("trusted_direction") if row else None
        return _FROM_DIRECTION.get(OPPOSITE_DIRECTION.get(current))
    return None


def _review_lines(filed: dict, rows: list[dict], values: dict | None = None,
                  line_errors: dict | None = None) -> list[dict]:
    times = {row.get("fingerprint"): row["event_timestamp"] for row in rows}
    rows_by_fp = {row.get("fingerprint"): row for row in rows}
    device_fps = {row.get("fingerprint") for row in rows if row.get("correction") != CORRECTION_ADDED}
    out = []
    for line in filed["lines"]:
        kind = line["kind"]
        pending = (line.get("status") or "pending") == "pending"
        asked_at = line.get("event_time") or times.get(line.get("target_fingerprint"))
        asked_dir = _default_direction(line, rows_by_fp)
        defaults = {
            "decision": "approved",
            "time": line["event_time"].strftime("%H:%M") if kind in FIX_ADD_KINDS and line.get("event_time") else "",
            "dir": asked_dir or "",
        }
        hr_edits = []
        if line.get("hr_event_time") and line.get("event_time") and line["hr_event_time"] != line["event_time"]:
            hr_edits.append(f"HR changed the time from {line['event_time']:%H:%M} to {line['hr_event_time']:%H:%M}")
        if line.get("hr_direction") and line.get("direction") and line["hr_direction"] != line["direction"]:
            hr_edits.append(f"HR changed the direction to {_DIRECTION_LABELS[line['hr_direction']]}")
        out.append({
            "id": line["id"],
            "kind": kind,
            "text": _line_view(line, times)["text"],
            "status": line.get("status") or "pending",
            "pending": pending,
            "note": line.get("note"),
            "decided_by_name": line.get("decided_by_name"),
            "asked_at": asked_at,
            "asked_dir": asked_dir,
            "edit_time": kind in FIX_ADD_KINDS,
            "edit_dir": kind != "ignore",
            "target_missing": kind in _TARGET_KINDS and line.get("target_fingerprint") not in device_fps,
            "hr_edits": hr_edits,
            "values": (values or {}).get(line["id"], defaults),
            "errors": (line_errors or {}).get(line["id"], {}),
        })
    return out


def _render_review(filed: dict, person: dict, day: date, *, values: dict | None = None,
                   errors: dict | None = None, note: str | None = None, status: int = 200) -> Response:
    errors = errors or {}
    day_start_hour = _day_start_hour(database)
    rows = _day_rows(person, day, day_start_hour)
    lines = _review_lines(filed, rows, values, errors.get("lines"))
    can_approve = access.can_approve_fix(g.employee, filed, database)
    day_changed = (filed["status"] == "pending" and bool(filed.get("punch_set_hash"))
                   and punch_set_hash(rows) != filed["punch_set_hash"])
    return Response(render_template(
        "fix_review_detail.html",
        employee=g.employee,
        csrf_token=g.csrf_token,
        filed=filed,
        person=person,
        day=day,
        day_label=alert_mail.fmt_day(day),
        day_start=_day_start_label(day_start_hour),
        swipes=_swipes(rows, day),
        lines=lines,
        pending_lines=[line for line in lines if line["pending"]],
        decided_lines=[line for line in lines if not line["pending"]],
        can_decide=can_approve and filed["status"] == "pending",
        readonly_reason=None if can_approve else _readonly_reason(filed),
        day_changed=day_changed,
        events=database.fix_request_events(filed["id"]),
        note=filed.get("decision_note") if note is None else note,
        errors=errors,
        note_max=FIX_NOTE_MAX_CHARS,
    ), status)


def _read_review(filed: dict, day: date, form) -> tuple[list[dict], dict, dict, str]:
    """HR's decisions from the detail form: (decisions for decide_fix_lines,
    the raw values per line, errors, note). Edits are passed only when they
    differ from what the employee asked, so the audit shows real changes."""
    day_start_hour = _day_start_hour(database)
    now = common._now_naive()
    decisions: list[dict] = []
    values: dict[int, dict] = {}
    line_errors: dict[int, dict] = {}
    errors: dict = {}
    missing = False
    for line in filed["lines"]:
        key = f"line-{line['id']}"
        decision = form.get(f"{key}-decision")
        if decision is None:
            missing = missing or line.get("status") == "pending"
            continue
        raw = {
            "decision": decision,
            "time": (form.get(f"{key}-time") or "").strip(),
            "dir": (form.get(f"{key}-dir") or "").strip(),
        }
        values[line["id"]] = raw
        problems: dict[str, str] = {}
        entry = {"line_id": line["id"], "status": decision}
        if decision not in ("approved", "rejected"):
            problems["decision"] = "Choose Approve or Reject."
        approved = decision == "approved"
        if approved and line["kind"] in FIX_ADD_KINDS:
            at, problem = _check_missed_time(raw["time"], day, day_start_hour, now)
            if problem:
                problems["time"] = problem
            elif at != line.get("event_time"):
                entry["event_time"] = at
        if approved and line["kind"] != "ignore":
            direction = _TO_DIRECTION.get(raw["dir"])
            if direction is None:
                problems["dir"] = "Choose In or Out."
            elif direction != line.get("direction"):
                entry["direction"] = direction
        if problems:
            line_errors[line["id"]] = problems
        decisions.append(entry)
    note = (form.get("note") or "").strip()
    if line_errors:
        errors["lines"] = line_errors
        errors["form"] = "Check the changes marked below."
    if any(d["status"] == "rejected" for d in decisions) and not note:
        errors["note"] = "Add a note for the employee saying why a change wasn't approved."
    elif len(note) > FIX_NOTE_MAX_CHARS:
        errors["note"] = f"Keep the note to {FIX_NOTE_MAX_CHARS} characters (it has {len(note)})."
    if missing:
        errors["form"] = ("A change was added to this request after you opened it. "
                          "Review it below, then continue.")
    elif not decisions:
        errors["form"] = "There is nothing left to decide on this request."
    return decisions, values, errors, note


def _hidden_fields(values: dict, note: str) -> list[tuple[str, str]]:
    fields = []
    for line_id, raw in values.items():
        for part in ("decision", "time", "dir"):
            if raw.get(part):
                fields.append((f"line-{line_id}-{part}", raw[part]))
    fields.append(("note", note))
    return fields


def _scored_day(rows: list[dict], day: date, day_start_hour: int, windows: dict):
    now = common._now_naive()
    is_today = day == work_day(now, day_start_hour)
    return compute_day(counted_punches(rows, windows), target_minutes=_target_minutes(database),
                       now=now if is_today else None, is_today=is_today)


def _proposed_rows(rows: list[dict], filed: dict, person: dict, decisions: list[dict]) -> list[dict]:
    """The day's rows with the approved lines merged by the same pure
    apply_corrections the read paths use (KTD4). ``rows`` already carry the
    approved overlay; its markers are kept where a new line adds none."""
    lines = {line["id"]: line for line in filed["lines"]}
    employee_id = filed["employee_id"]
    proposed = []
    for n, decision in enumerate(decisions, start=1):
        line = lines[decision["line_id"]]
        if decision["status"] != "approved" or line.get("status") != "pending":
            continue
        if line["kind"] in FIX_ADD_KINDS:
            # A unique id per proposed add, so their "correction:<id>" rows
            # never collide with each other or with stored ones.
            proposed.append({
                "id": f"proposed-{n}", "kind": "add", "employee_id": employee_id, "fingerprint": None,
                "event_timestamp": decision.get("event_time") or line["event_time"],
                "direction": decision.get("direction") or line["direction"],
            })
        elif line["kind"] == "ignore":
            proposed.append({"kind": "ignore", "employee_id": employee_id,
                             "fingerprint": line["target_fingerprint"], "direction": None})
        else:
            direction = decision.get("direction") or line.get("direction")
            if direction:
                proposed.append({"kind": "set_direction", "employee_id": employee_id,
                                 "fingerprint": line["target_fingerprint"], "direction": direction})
    before = {row.get("fingerprint"): row.get("correction") for row in rows}
    merged = apply_corrections(rows, proposed, {employee_id: str(person["device_user_id"])})
    for row in merged:
        earlier = before.get(row.get("fingerprint"))
        if earlier == CORRECTION_IGNORED or (earlier and row["correction"] is None):
            row["correction"] = earlier
    return merged


def _summary(filed: dict, decisions: list[dict], rows: list[dict]) -> list[dict]:
    """Each decision as the employee will read it, with HR's edits applied."""
    times = {row.get("fingerprint"): row["event_timestamp"] for row in rows}
    lines = {line["id"]: line for line in filed["lines"]}
    out = []
    for decision in decisions:
        line = lines[decision["line_id"]]
        edited = {**line,
                  "hr_event_time": decision.get("event_time"),
                  "hr_direction": decision.get("direction"),
                  "status": "pending"}
        asked = _line_view({**line, "status": "pending"}, times)["text"]
        text = _line_view(edited, times)["text"]
        out.append({
            "text": text,
            "asked": asked if asked != text else None,
            "status": decision["status"],
            "already": None if line.get("status") == "pending" else {
                "status": line.get("status"), "by": line.get("decided_by_name") or "someone else"},
        })
    return out


@bp.get("/fixes/review")
@_role_required(*_REVIEW_ROLES)
def review_list() -> Response:
    rows = database.fix_requests_for_review()
    for row in rows:
        row["day_label"] = alert_mail.fmt_day(row["work_day"])
        row["own"] = row["employee_id"] == g.employee["id"]
    response = make_response(render_template(
        "fix_review.html",
        employee=g.employee,
        pending=[r for r in rows if r["status"] == "pending"],
        decided=[r for r in rows if r["status"] != "pending"],
        flash=_take_flash(),
    ))
    if request.cookies.get(_FLASH_COOKIE):
        response.delete_cookie(_FLASH_COOKIE)
    return response


@bp.get("/fixes/review/<int:request_id>")
@_role_required(*_REVIEW_ROLES)
def review_detail(request_id: int) -> Response:
    filed, person, day = _review_request(request_id)
    return _render_review(filed, person, day)


@bp.post("/fixes/review/<int:request_id>")
@_role_required(*_REVIEW_ROLES)
def review_preview(request_id: int) -> Response:
    """Step one of confirm-then-apply: validate and show what the decision
    does to the day's hours. Nothing is written here."""
    _require_csrf()
    filed, person, day = _review_request(request_id)
    if not access.can_approve_fix(g.employee, filed, database):
        abort(403)
    decisions, values, errors, note = _read_review(filed, day, request.form)
    if request.form.get("action") == "edit":
        return _render_review(filed, person, day, values=values, note=note)
    if errors:
        return _render_review(filed, person, day, values=values, errors=errors, note=note, status=400)
    day_start_hour = _day_start_hour(database)
    windows = database.device_windows()
    rows = _day_rows(person, day, day_start_hour)
    before = _scored_day(rows, day, day_start_hour, windows)
    after = _scored_day(_proposed_rows(rows, filed, person, decisions), day, day_start_hour, windows)
    return Response(render_template(
        "fix_review_confirm.html",
        employee=g.employee,
        csrf_token=g.csrf_token,
        filed=filed,
        person=person,
        day=day,
        day_label=alert_mail.fmt_day(day),
        summary=_summary(filed, decisions, rows),
        note=note,
        before=before,
        after=after,
        fields=_hidden_fields(values, note),
    ))


def _notify_decision(filed: dict, person: dict, day: date, outcome: str, note: str | None) -> None:
    """The employee's decision email (R13); nobody to send to when they have
    no address or have left."""
    employee = database.employee_by_id(person["id"])
    if employee is None or employee.get("archived_at") or not (employee.get("email") or "").strip():
        _logger.info("Fix request %s decided; employee has no active email, none sent", filed["id"])
        return
    decided = database.fix_request(filed["id"]) or filed
    rows = _day_rows(person, day, _day_start_hour(database))
    times = {row.get("fingerprint"): row["event_timestamp"] for row in rows}
    lines = [_line_view(line, times)["fix"] for line in decided["lines"]]
    day_url = None
    base = config.public_url_or_none(f"Fix request {filed['id']}: decision email sent without a link")
    if base:
        day_url = base + url_for("me.day_view", date=day.isoformat())
    email = alert_mail.decision(employee_name=employee["name"], work_day=day, outcome=outcome,
                                lines=lines, hr_note=note or None, day_url=day_url)
    database.enqueue_mail([employee["email"]], email.subject, email.text, email.html)


def _saved_message(decisions: list[dict], result: dict) -> dict:
    status_of = {d["line_id"]: d["status"] for d in decisions}
    approved = sum(1 for line_id in result["decided"] if status_of.get(line_id) == "approved")
    rejected = len(result["decided"]) - approved
    message = {}
    if result["decided"]:
        noun = "line" if approved == 1 else "lines"
        message["saved"] = f"Saved. {approved} {noun} approved, {rejected} rejected."
    else:
        message["saved"] = "Nothing was saved."
    if result["skipped"]:
        names = sorted({s.get("decided_by_name") or "someone else" for s in result["skipped"]})
        count = len(result["skipped"])
        message["skipped"] = (f"{count} {'line was' if count == 1 else 'lines were'} skipped: "
                              f"already decided by {', '.join(names)}.")
    return message


@bp.post("/fixes/review/<int:request_id>/confirm")
@_role_required(*_REVIEW_ROLES)
def review_confirm(request_id: int) -> Response:
    """Apply HR's decisions in one transaction, then email the employee."""
    _require_csrf()
    filed, person, day = _review_request(request_id)
    if not access.can_approve_fix(g.employee, filed, database):
        abort(403)
    decisions, values, errors, note = _read_review(filed, day, request.form)
    if errors:
        return _render_review(filed, person, day, values=values, errors=errors, note=note, status=400)
    try:
        result = database.decide_fix_lines(request_id, decisions, actor_id=g.employee["id"],
                                           note=note or None)
    except RuleViolation:
        abort(403)
    except ValueError as exc:
        errors = {"form": common._sentence(str(exc))}
        return _render_review(filed, person, day, values=values, errors=errors, note=note, status=400)
    if result["decided"] and result["status"] in FIX_DECIDED_STATUSES:
        try:
            _notify_decision(filed, person, day, result["status"], note)
        except Exception:  # noqa: BLE001 - the decision is saved; a lost email must not 500 it
            _logger.exception("Fix request %s decided but its decision email failed", request_id)
    return _redirect_with_flash("fixes.review_list", **_saved_message(decisions, result))


# --- Privacy notice ------------------------------------------------------------


@bp.get("/privacy")
def privacy() -> str:
    return render_template("privacy.html")
