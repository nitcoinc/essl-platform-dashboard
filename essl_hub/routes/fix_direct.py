"""Admin direct fixes (v10): an admin sets a punch's direction, ignores a punch
or adds a missed one on an employee's day without a request, with a required
reason and the same confirm-then-apply preview as the HR review. Routes
register on the ``fixes`` blueprint (routes.fixes), so endpoint names stay
``fixes.direct_*``. Never edits a punch row: each change is a correction
stamped with who made it (Database.record_direct_corrections)."""

from __future__ import annotations

from datetime import date

from flask import Response, abort, g, redirect, render_template, request, url_for

from .. import access, alert_mail
from ..db import FIX_NOTE_MAX_CHARS, RuleViolation
from . import common
from .common import _day_start_hour, _day_start_label, _parse_date, _require_csrf, _role_required, database
from .fix_review import _merge_proposed, _scored_day
from .fix_views import _TO_DIRECTION, _actionable, _check_missed_time, _day_rows, _swipes, punch_set_hash
from .fixes import _logger, bp

ADD_ROWS = 3  # "add a missed punch" rows on the form
_ACTIONS = ("", "in", "out", "ignore")


def _target(employee_id: int, raw_day: str) -> tuple[dict, date]:
    """The employee and work day, or 404; 403 for the admin's own punches."""
    day = _parse_date(raw_day)
    person = database.employee_by_id(employee_id)  # active people only
    if day is None or person is None or day > common._today():
        abort(404)
    if not access.can_fix_punches_directly(g.employee, person["id"]):
        abort(403)
    return person, day


def _read_form(person: dict, day: date, rows: list[dict], form) -> tuple[list[dict], dict, dict, str]:
    """(edits for record_direct_corrections, the raw values to redisplay,
    errors, reason). Only punches on the day can be targeted."""
    now = common._now_naive()
    day_start_hour = _day_start_hour(database)
    by_fp = {row["fingerprint"]: row for row in rows if _actionable(row)}
    values: dict = {"act": {}, "add": []}
    errors: dict = {}
    edits: list[dict] = []
    for name in form:
        if name.startswith("act-") and name[4:] not in by_fp:
            errors["form"] = "A punch on this form is no longer on the day. Review the day below."
    for fingerprint, row in by_fp.items():
        action = (form.get(f"act-{fingerprint}") or "").strip()
        if action not in _ACTIONS:
            errors["form"] = "Choose In, Out or Ignore for a punch, or leave it as it is."
            action = ""
        values["act"][fingerprint] = action
        if action == "ignore":
            edits.append({"kind": "ignore", "fingerprint": fingerprint})
        elif action in ("in", "out"):
            direction = _TO_DIRECTION[action]
            if direction != row.get("trusted_direction"):
                edits.append({"kind": "set_direction", "fingerprint": fingerprint, "direction": direction})
    add_errors: dict[int, dict] = {}
    for n in range(ADD_ROWS):
        raw = {"time": (form.get(f"add-{n}-time") or "").strip(), "dir": (form.get(f"add-{n}-dir") or "").strip()}
        values["add"].append(raw)
        if not raw["time"] and not raw["dir"]:
            continue
        problems: dict[str, str] = {}
        at, problem = _check_missed_time(raw["time"], day, day_start_hour, now)
        if problem:
            problems["time"] = problem
        if raw["dir"] not in ("in", "out"):
            problems["dir"] = "Choose In or Out."
        if problems:
            add_errors[n] = problems
        else:
            edits.append({"kind": "add", "event_timestamp": at, "direction": _TO_DIRECTION[raw["dir"]]})
    if add_errors:
        errors["add"] = add_errors
        errors.setdefault("form", "Check the changes marked below.")
    reason = (form.get("reason") or "").strip()
    if not reason:
        errors["reason"] = "Say why you are changing these punches. It is kept with the change."
    elif len(reason) > FIX_NOTE_MAX_CHARS:
        errors["reason"] = f"Keep the reason to {FIX_NOTE_MAX_CHARS} characters (it has {len(reason)})."
    if not edits and "form" not in errors:
        errors["form"] = "Choose at least one change."
    return edits, values, errors, reason


def _render_form(person: dict, day: date, rows: list[dict], *, values: dict | None = None,
                 errors: dict | None = None, reason: str = "", notice: str | None = None,
                 status: int = 200) -> Response:
    day_start_hour = _day_start_hour(database)
    swipes = _swipes(rows, day)
    values = values or {"act": {}, "add": [{"time": "", "dir": ""} for _ in range(ADD_ROWS)]}
    return Response(render_template(
        "fix_direct.html",
        employee=g.employee,
        csrf_token=g.csrf_token,
        person=person,
        day=day,
        day_label=alert_mail.fmt_day(day),
        day_start=_day_start_label(day_start_hour),
        swipes=swipes,
        values=values,
        errors=errors or {},
        reason=reason,
        reason_max=FIX_NOTE_MAX_CHARS,
        add_rows=ADD_ROWS,
        notice=notice,
        form_hash=punch_set_hash(rows),
    ), status)


def _summary(edits: list[dict], rows: list[dict]) -> list[str]:
    times = {row["fingerprint"]: row["event_timestamp"] for row in rows}
    label = {"entry": "In", "exit": "Out"}
    out = []
    for edit in edits:
        if edit["kind"] == "add":
            out.append(f"Add a missed punch: {label[edit['direction']]} at {edit['event_timestamp']:%H:%M}")
        else:
            at = times.get(edit["fingerprint"])
            when = f"the {at:%H:%M:%S} punch" if at else "a punch"
            out.append(f"Ignore {when}" if edit["kind"] == "ignore"
                       else f"Count {when} as {label[edit['direction']]}")
    return out


def _proposed(edits: list[dict], employee_id: int) -> list[dict]:
    proposed = []
    for n, edit in enumerate(edits, start=1):
        item = {**edit, "employee_id": employee_id, "fingerprint": edit.get("fingerprint")}
        if edit["kind"] == "add":
            item.update(id=f"proposed-{n}")
        proposed.append(item)
    return proposed


@bp.get("/fixes/direct/<int:employee_id>/<day>")
@_role_required("admin")
def direct_form(employee_id: int, day: str) -> Response:
    person, work = _target(employee_id, day)
    return _render_form(person, work, _day_rows(person, work, _day_start_hour(database)))


@bp.post("/fixes/direct/<int:employee_id>/<day>")
@_role_required("admin")
def direct_preview(employee_id: int, day: str) -> Response:
    """Step one of confirm-then-apply: validate and show what the changes do
    to the day's hours. Nothing is written here."""
    _require_csrf()
    person, work = _target(employee_id, day)
    day_start_hour = _day_start_hour(database)
    rows = _day_rows(person, work, day_start_hour)
    edits, values, errors, reason = _read_form(person, work, rows, request.form)
    if errors:
        return _render_form(person, work, rows, values=values, errors=errors, reason=reason, status=400)
    windows = database.device_windows()
    before = _scored_day(rows, work, day_start_hour, windows)
    after = _scored_day(_merge_proposed(rows, _proposed(edits, person["id"]), person["id"], person),
                        work, day_start_hour, windows)
    return Response(render_template(
        "fix_direct_confirm.html",
        employee=g.employee,
        csrf_token=g.csrf_token,
        person=person,
        day=work,
        day_label=alert_mail.fmt_day(work),
        summary=_summary(edits, rows),
        reason=reason,
        before=before,
        after=after,
        fields=_hidden_fields(request.form, values, rows),
        form_hash=punch_set_hash(rows),
    ))


def _hidden_fields(form, values: dict, rows: list[dict]) -> list[tuple[str, str]]:
    fields = [(f"act-{fp}", action) for fp, action in values["act"].items() if action]
    for n, raw in enumerate(values["add"]):
        if raw["time"] or raw["dir"]:
            fields += [(f"add-{n}-time", raw["time"]), (f"add-{n}-dir", raw["dir"])]
    fields.append(("reason", (form.get("reason") or "").strip()))
    return fields


@bp.post("/fixes/direct/<int:employee_id>/<day>/confirm")
@_role_required("admin")
def direct_confirm(employee_id: int, day: str) -> Response:
    """Apply the changes in one transaction. Refused when the day's punches
    changed since the preview, so a stale preview never lands."""
    _require_csrf()
    person, work = _target(employee_id, day)
    rows = _day_rows(person, work, _day_start_hour(database))
    edits, values, errors, reason = _read_form(person, work, rows, request.form)
    if errors:
        return _render_form(person, work, rows, values=values, errors=errors, reason=reason, status=400)
    if request.form.get("punch_set_hash") != punch_set_hash(rows):
        return _render_form(
            person, work, rows, values=values, reason=reason, status=409,
            notice="The day's punches changed after you opened this page. Check them below, then review again.")
    try:
        database.record_direct_corrections(person["id"], work, edits, actor_id=g.employee["id"], reason=reason)
    except RuleViolation:
        abort(403)
    except ValueError as exc:
        return _render_form(person, work, rows, values=values, reason=reason, status=400,
                            errors={"form": common._sentence(str(exc))})
    _logger.info("Direct fix: %s changed %d punch record(s) for employee %s on %s",
                 g.employee["id"], len(edits), person["id"], work)
    return redirect(url_for("me.day_view", employee=person["id"], date=work.isoformat()))
