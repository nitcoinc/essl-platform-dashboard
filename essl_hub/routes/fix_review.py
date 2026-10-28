"""HR review of fix requests (U8, R12-R14): the review list, a request's
detail page, the confirm-then-apply preview and the decision. Routes register
on the ``fixes`` blueprint (routes.fixes), so endpoint names stay
``fixes.review_*``, and log to that module's logger."""

from __future__ import annotations

from datetime import date

from flask import Response, abort, g, make_response, render_template, request, url_for

from .. import access, alert_mail, config
from ..db import FIX_ADD_KINDS, FIX_DECIDED_STATUSES, FIX_NOTE_MAX_CHARS, OPPOSITE_DIRECTION, RuleViolation
from ..hours import CORRECTION_ADDED, CORRECTION_IGNORED, apply_corrections, compute_day, counted_punches, work_day
from . import common
from .common import _day_start_hour, _day_start_label, _require_csrf, _role_required, _target_minutes, database
from .fix_views import (
    _DIRECTION_LABELS,
    _FROM_DIRECTION,
    _TO_DIRECTION,
    _check_missed_time,
    _day_rows,
    _line_view,
    _swipes,
    punch_set_hash,
)
from .fixes import _logger, bp
from .people import _FLASH_COOKIE, _redirect_with_flash, _take_flash

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
    """Why the viewer may look but not decide, or None."""
    if filed["employee_id"] == g.employee["id"]:
        return "This is your own request. Another HR user or an admin decides it; nobody approves their own fixes."
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
    can_approve = access.can_approve_fix(g.employee, filed)
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
    return _merge_proposed(rows, proposed, employee_id, person)


def _merge_proposed(rows: list[dict], proposed: list[dict], employee_id: int, person: dict) -> list[dict]:
    """``rows`` with ``proposed`` corrections merged by apply_corrections;
    markers the rows already carried are kept where a proposal adds none."""
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
    if not access.can_approve_fix(g.employee, filed):
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
    if not access.can_approve_fix(g.employee, filed):
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
