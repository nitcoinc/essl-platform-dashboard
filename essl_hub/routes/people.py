"""People management, the person form and roster import."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import time

import openpyxl
from flask import Blueprint, Response, abort, g, make_response, redirect, render_template, request, url_for

from .. import access, export, id_issues, roster
from ..db import IntegrityError, RuleViolation
from .common import (
    ROLE_LABELS,
    ROSTER_EXAMPLE_ROW,
    ROSTER_INSTRUCTIONS,
    ROSTER_TEMPLATE_HEADER,
    XLSX_MIMETYPE,
    _day_for_employee,
    _day_start_hour,
    _home_for,
    _htmx_partial,
    _int_or_zero,
    _max_upload_mb,
    _now_naive,
    _require_csrf,
    _role_required,
    _roster_rows,
    _sentence,
    _target_minutes,
    _today,
    _vary_htmx,
    database,
)

bp = Blueprint("people", __name__)


@bp.app_errorhandler(413)
def upload_too_large(error):
    # MAX_CONTENT_LENGTH fires for any view that parses the request body,
    # including the public /signin POSTs. Only HR and admin may see the
    # import page, so everyone else keeps the default 413 response.
    if g.employee is None or g.employee["role"] not in access.EDITOR_ROLES:
        return error.get_response()
    return _render_import(
        error=(
            f"That file is larger than the {_max_upload_mb()} MB upload limit. "
            "Split the roster into smaller files and upload again."
        ),
        status=413,
    )
# --- People (U7) -------------------------------------------------------

def _people_rows(query: str, everyone: list[dict]) -> list[dict]:
    names = {person["id"]: person["name"] for person in everyone}
    lowered = query.lower()
    people = [
        person for person in everyone
        if not lowered or lowered in person["name"].lower() or lowered in person["email"].lower()
    ]
    # Today's status for every row from one punch query, not one per person.
    day_start_hour = _day_start_hour(database)
    today = _today(day_start_hour)
    results = export.day_results(
        database, people, today, today,
        target_minutes=_target_minutes(database), today=today, now=_now_naive(),
        day_start_hour=day_start_hour,
    )
    return [_person_row(person, names, results[(person["id"], today)]) for person in people]


def _person_row(person: dict, names: dict, result) -> dict:
    actor = g.employee
    return {
        "person": person,
        "manager_name": names.get(person.get("manager_id")),
        "result": result,
        # Templates never decide access (KTD8): every flag comes from here.
        "role_locked": person["id"] == actor["id"]
        or not access.can_modify_employee(actor, person, "role"),
        "role_options": [
            role for role in access.ROLES
            if role == person["role"] or access.can_assign_role(actor, person, role)
        ],
        "identity_locked": not access.can_modify_employee(actor, person, "email"),
    }


def _render_people(row_error: tuple[int, str] | None = None, status: int = 200):
    query = (request.args.get("q") or "").strip()
    everyone = database.employees()
    context = {
        "employee": g.employee,
        "csrf_token": g.csrf_token,
        "query": query,
        "rows": _people_rows(query, everyone),
        "row_error": row_error,
        "role_labels": ROLE_LABELS,
    }
    if row_error:
        # The no-JS error page opens the failing row in edit mode.
        context["edit_id"] = row_error[0]
        context["everyone"] = everyone
    partial = _htmx_partial() and not row_error
    template = "_people_results.html" if partial else "people.html"
    return _vary_htmx(make_response(render_template(template, oob=partial, **context), status))


def _render_row(target_id: int, *, edit: bool, row_error: tuple[int, str] | None = None, focus: bool = False):
    """One People row, as text or (edit=True) with the manager and role selects."""
    person = database.employee_by_id(target_id)
    if person is None:
        abort(404)
    everyone = database.employees()
    names = {p["id"]: p["name"] for p in everyone}
    day_start_hour = _day_start_hour(database)
    result = _day_for_employee(
        database, person, _today(day_start_hour), _target_minutes(database), day_start_hour=day_start_hour
    )
    return render_template(
        "_person_row.html",
        row=_person_row(person, names, result),
        everyone=everyone if edit else None,
        edit_id=target_id if edit else None,
        employee=g.employee,
        csrf_token=g.csrf_token,
        row_error=row_error,
        role_labels=ROLE_LABELS,
        focus_row=focus,
    )


def _change_assignment(target: dict, *, manager_raw: str | None, role_raw: str | None):
    """Apply a manager and/or role change. Returns (error, status) or None.
    Every check runs before the first write, so a refused change leaves
    the record untouched; the guarded Database methods re-check the
    concurrency-sensitive rules (last admin, cycles) under the lock."""
    actor = g.employee
    manager_id = target.get("manager_id")
    if manager_raw is not None:
        if manager_raw.strip() == "":
            manager_id = None
        else:
            try:
                manager_id = int(manager_raw)
            except ValueError:
                return "Choose a manager from the list.", 400
    manager_changed = manager_id != target.get("manager_id")
    role = (role_raw or "").strip() or target["role"]
    role_changed = role != target["role"]

    if manager_changed:
        if not access.can_modify_employee(actor, target, "manager_id"):
            return "You cannot change this person's manager.", 403
        if manager_id is not None and database.employee_by_id(manager_id) is None:
            return "Choose a manager from the list.", 400
        if manager_id == target["id"]:
            return "A person cannot be their own manager.", 400
        if access.would_create_cycle(database, target["id"], manager_id):
            return "That manager reports to this person, directly or through others. Pick someone outside their team.", 400
    if role_changed:
        if role not in access.ROLES:
            return "Choose employee, manager, hr or admin.", 200
        if target["id"] == actor["id"]:
            return "You cannot change your own role; ask another admin to do it.", 200
        if not access.can_assign_role(actor, target, role):
            return "Only an admin can grant, revoke or change the admin role.", 403
    try:
        if role_changed:
            database.set_employee_role(target["id"], role)
        if manager_changed:
            database.set_employee_manager(target["id"], manager_id)
    except RuleViolation as exc:
        return str(exc), 409
    return None


def _assignment_response(target_id: int, result):
    if request.headers.get("HX-Request"):
        # htmx swaps only 2xx responses, so the error fragment goes out as
        # 200 and carries the message in a role="alert" element. Success
        # swaps the row back to text; an error keeps it in edit mode.
        if result:
            return _render_row(target_id, edit=True, row_error=(target_id, result[0]), focus=True)
        return _render_row(target_id, edit=False, focus=True)
    if result is None:
        return redirect(url_for("people.people_view", q=request.args.get("q") or None))
    error, status = result
    return _render_people(row_error=(target_id, error), status=status)


@bp.get("/people")
@_role_required("hr", "admin")
def people_view():
    return _render_people()


@bp.get("/people/<int:employee_id>/row")
@_role_required("hr", "admin")
def people_row(employee_id: int):
    """htmx row swap: ?edit=1 opens the manager and role selects, plain
    GET (Cancel) returns the text row unchanged."""
    return _render_row(employee_id, edit=bool(request.args.get("edit")), focus=True)


@bp.post("/people/<int:employee_id>/assign")
@_role_required("hr", "admin")
def people_assign(employee_id: int):
    _require_csrf()
    target = database.employee_by_id(employee_id)
    if target is None:
        abort(404)
    result = _change_assignment(
        target, manager_raw=request.form.get("manager_id"), role_raw=request.form.get("role")
    )
    return _assignment_response(employee_id, result)


@bp.post("/people/<int:employee_id>/role")
@_role_required("hr", "admin")
def people_set_role(employee_id: int):
    _require_csrf()
    target = database.employee_by_id(employee_id)
    if target is None:
        abort(404)
    role = (request.form.get("role") or "").strip()
    if role not in access.ROLES:
        result = ("Choose employee, manager, hr or admin.", 200)
    else:
        result = _change_assignment(target, manager_raw=None, role_raw=role)
    if result is None and not request.headers.get("HX-Request"):
        return redirect(_home_for(g.employee))
    return _assignment_response(employee_id, result)


def _render_person_form(target: dict | None, values: dict, errors: dict, status: int = 200):
    actor = g.employee
    locked = target is not None and not access.can_modify_employee(actor, target, "email")
    if target is None:
        role_options = [r for r in access.ROLES if access.can_assign_role(actor, {"role": "employee"}, r)]
    else:
        role_options = [r for r in access.ROLES if r == target["role"] or access.can_assign_role(actor, target, r)]
    return (
        render_template(
            "person_form.html",
            employee=g.employee,
            csrf_token=g.csrf_token,
            target=target,
            values=values,
            errors=errors,
            identity_locked=locked,
            role_locked=target is not None and (target["id"] == actor["id"] or not access.can_modify_employee(actor, target, "role")),
            role_options=role_options,
            role_labels=ROLE_LABELS,
            managers=[p for p in database.employees() if target is None or p["id"] != target["id"]],
        ),
        status,
    )


def _person_form_values() -> dict:
    return {
        "name": (request.form.get("name") or "").strip(),
        "email": (request.form.get("email") or "").strip().lower(),
        "device_user_id": (request.form.get("device_user_id") or "").strip(),
        "manager_id": (request.form.get("manager_id") or "").strip(),
        "role": (request.form.get("role") or "").strip(),
    }


def _identity_errors(values: dict, target_id: int | None) -> dict:
    """Same shape rules as a roster row, plus uniqueness against others."""
    message = roster.identity_error(values["name"], values["email"], values["device_user_id"])
    if message:
        field = {"name": "name", "email": "email", "device": "device_user_id"}[message.split()[0]]
        return {field: _sentence(message)}
    errors = {}
    clash = database.employee_by_email(values["email"])
    if clash is not None and clash["id"] != target_id:
        errors["email"] = f"{clash['name']} already uses this work email."
    clash = database.employee_by_device_user_id(values["device_user_id"])
    if clash is not None and clash["id"] != target_id:
        errors["device_user_id"] = f"{clash['name']} already uses this device user ID."
    return errors


@bp.get("/people/new")
@_role_required("hr", "admin")
def person_new():
    # Prefill from the ID issues page ("Add person" for an unknown device ID).
    values = {"role": "employee"}
    for field in ("name", "device_user_id"):
        if request.args.get(field):
            values[field] = request.args[field].strip()
    return _render_person_form(None, values, {})


@bp.post("/people/new")
@_role_required("hr", "admin")
def person_create():
    _require_csrf()
    values = _person_form_values()
    errors = _identity_errors(values, None)
    role = values["role"] or "employee"
    if not errors and not access.can_assign_role(g.employee, {"role": "employee"}, role):
        errors["role"] = "Only an admin can grant the admin role."
    if not errors and values["manager_id"] and database.employee_by_id(_int_or_zero(values["manager_id"])) is None:
        errors["manager_id"] = "Choose a manager from the list."
    if errors:
        return _render_person_form(None, values, errors, status=400)
    try:
        database.upsert_employee(
            device_user_id=values["device_user_id"], name=values["name"], email=values["email"]
        )
    except IntegrityError:
        return _render_person_form(
            None, values, {"email": "This work email or device user ID is already in use."}, status=400
        )
    person = database.employee_by_device_user_id(values["device_user_id"])
    result = _change_assignment(person, manager_raw=values["manager_id"], role_raw=role)
    if result is not None:
        return _render_person_form(person, values, {"form": result[0]}, status=result[1] if result[1] != 200 else 400)
    forget_issue_count()
    return redirect(url_for("people.people_view", q=values["name"]))


@bp.get("/people/<int:employee_id>/edit")
@_role_required("hr", "admin")
def person_edit(employee_id: int):
    target = database.employee_by_id(employee_id)
    if target is None:
        abort(404)
    values = {
        "name": target["name"],
        "email": target["email"],
        "device_user_id": target["device_user_id"],
        "manager_id": str(target.get("manager_id") or ""),
        "role": target["role"],
    }
    return _render_person_form(target, values, {})


@bp.post("/people/<int:employee_id>/edit")
@_role_required("hr", "admin")
def person_update(employee_id: int):
    _require_csrf()
    target = database.employee_by_id(employee_id)
    if target is None:
        abort(404)
    values = _person_form_values()
    for field in ("email", "device_user_id"):
        changed = (
            values[field].lower() != str(target[field]).lower()
            if field == "email"
            else values[field] != str(target[field])
        )
        if changed and not access.can_modify_employee(g.employee, target, field):
            values[field] = target[field]
            return _render_person_form(
                target,
                values,
                {"form": "Only an admin can change an admin's work email or device user ID."},
                status=403,
            )
    errors = _identity_errors(values, target["id"])
    if errors:
        return _render_person_form(target, values, errors, status=400)
    result = _change_assignment(target, manager_raw=values["manager_id"], role_raw=values["role"])
    if result is not None:
        return _render_person_form(target, values, {"form": result[0]}, status=result[1] if result[1] != 200 else 400)
    try:
        database.update_employee(
            target["id"], name=values["name"], email=values["email"], device_user_id=values["device_user_id"]
        )
    except IntegrityError:
        return _render_person_form(
            target, values, {"email": "This work email or device user ID is already in use."}, status=400
        )
    forget_issue_count()
    return redirect(url_for("people.people_view", q=values["name"]))


# --- Roster import (U6) ------------------------------------------------

def _render_import(outcomes=None, error=None, status: int = 200):
    issues = [o for o in outcomes or [] if o.result == "rejected" or o.warnings]
    return (
        render_template(
            "people_import.html",
            employee=g.employee,
            csrf_token=g.csrf_token,
            outcomes=outcomes,
            summary=roster.summarize(outcomes) if outcomes is not None else None,
            issues=issues,
            issues_csv=roster.issues_csv(outcomes) if issues else None,
            error=error,
            max_upload_mb=_max_upload_mb(),
        ),
        status,
    )


@bp.get("/people/import")
@_role_required("hr", "admin")
def people_import():
    return _render_import()


@bp.post("/people/import")
@_role_required("hr", "admin")
def people_import_upload():
    _require_csrf()
    file = request.files.get("roster")
    if file is None or not file.filename:
        return _render_import(error="Choose a .csv or .xlsx file to upload.", status=400)
    try:
        outcomes = roster.apply_rows(
            database, _roster_rows(file.filename, file.read()), actor=g.employee
        )
    except roster.RosterFileError as exc:
        return _render_import(error=_sentence(str(exc)), status=400)
    forget_issue_count()
    return _render_import(outcomes=outcomes)


@bp.get("/people/import/template.csv")
@_role_required("hr", "admin")
def people_import_template_csv() -> Response:
    output = io.StringIO(newline="")
    csv.writer(output).writerow(ROSTER_TEMPLATE_HEADER)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=roster-template.csv"},
    )


@bp.get("/people/import/template.xlsx")
@_role_required("hr", "admin")
def people_import_template_xlsx() -> Response:
    # Headers only on the first sheet, so uploading it unchanged adds
    # nobody; the example row lives on the Instructions sheet.
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Roster"
    sheet.append(list(ROSTER_TEMPLATE_HEADER))
    guide = workbook.create_sheet("Instructions")
    guide.append(["Column", "Rule"])
    for row in ROSTER_INSTRUCTIONS:
        guide.append(list(row))
    guide.append([])
    guide.append(["Example row (copy to the Roster sheet)"])
    guide.append(list(ROSTER_TEMPLATE_HEADER))
    guide.append(list(ROSTER_EXAMPLE_ROW))
    stream = io.BytesIO()
    workbook.save(stream)
    return Response(
        stream.getvalue(),
        mimetype=XLSX_MIMETYPE,
        headers={"Content-Disposition": "attachment; filename=roster-template.xlsx"},
    )


# --- ID issues ---------------------------------------------------------
# Computed live from the roster and the punches, so fixing the roster (add,
# edit, import) clears an issue on the next load; nothing is stored except
# the "same person" dismissals.

_COUNT_TTL_SECONDS = 60
_count_cache: dict[int, tuple[float, int]] = {}


def _dismissed() -> set[str]:
    try:
        return set(json.loads(database.setting(id_issues.DISMISSED_SETTING) or "[]"))
    except ValueError:
        return set()


def _issues(dismissed: set[str] | None = None) -> list[id_issues.Issue]:
    return id_issues.find_issues(
        database.device_id_summary(), database.employees(), _dismissed() if dismissed is None else dismissed
    )


def issue_count() -> int:
    """Actionable issues for the nav badge; cached briefly since every HR
    and admin page shows it and the summary scans all punches."""
    key = id(database._get_current_object()) if hasattr(database, "_get_current_object") else id(database)
    hit = _count_cache.get(key)
    if hit and time.monotonic() - hit[0] < _COUNT_TTL_SECONDS:
        return hit[1]
    count = id_issues.actionable(_issues())
    _count_cache[key] = (time.monotonic(), count)
    return count


def forget_issue_count() -> None:
    _count_cache.clear()


# Feedback after a fix rides in a short-lived cookie signed with the
# session's own CSRF secret, so no URL parameter (crafted or stale) is ever
# rendered as a message.
_FLASH_COOKIE = "es_flash"


def _flash_sign(payload: str) -> str:
    return hmac.new((g.csrf_token or "").encode(), payload.encode(), hashlib.sha256).hexdigest()


def _redirect_with_flash(endpoint: str, **message):
    response = redirect(url_for(endpoint))
    payload = base64.urlsafe_b64encode(json.dumps(message).encode()).decode()
    response.set_cookie(_FLASH_COOKIE, f"{payload}.{_flash_sign(payload)}", max_age=120,
                        httponly=True, samesite="Lax", secure=request.is_secure)
    return response


def _take_flash() -> dict:
    raw = request.cookies.get(_FLASH_COOKIE) or ""
    payload, _, signature = raw.partition(".")
    if not payload or not g.csrf_token or not hmac.compare_digest(signature, _flash_sign(payload)):
        return {}
    try:
        message = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return {}
    return message if isinstance(message, dict) else {}


def _issue_error(message: str):
    return _redirect_with_flash("people.id_issues_view", error=message)


def _resolve_person(raw: str) -> dict | None:
    """A roster person from the link picker: an id, an email or a device ID."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit() and database.employee_by_id(int(raw)):
        return database.employee_by_id(int(raw))
    return database.employee_by_email(raw) or database.employee_by_device_user_id(raw)


def _punch_count(device_user_id: str) -> int:
    return next((int(r["punch_count"] or 0) for r in database.device_id_summary() if str(r["device_user_id"]) == device_user_id), 0)


@bp.get("/people/issues")
@_role_required(*access.EDITOR_ROLES)
def id_issues_view():
    dismissed = _dismissed()
    issues = _issues(dismissed)
    order = {"bad": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: (order[i.severity], i.kind, i.device_user_id))
    counts = {kind: sum(1 for i in issues if i.kind == kind) for kind in ("unknown", "placeholder", "never", "name", "shared")}
    pairs = id_issues.suggestions(issues)
    twins = id_issues.id_twins(issues)
    flash = _take_flash()
    confirm = None
    if request.args.get("confirm"):
        confirm = _link_preview(request.args.get("device_user_id") or "", request.args.get("person") or "")
        if isinstance(confirm, str):
            flash, confirm = {"error": confirm}, None
    response = make_response(render_template(
        "id_issues.html",
        employee=g.employee,
        issues=issues,
        counts=counts,
        issue_count=id_issues.actionable(issues),
        suggestions=pairs,
        twins=twins,
        fixes={i.key: id_issues.primary_fix(i, pairs, twins) for i in issues},
        lost_punches=sum(i.punch_count for i in issues if i.kind == "unknown"),
        roster=database.employees(),
        dismissed_count=len(dismissed),
        csrf_token=g.csrf_token,
        flash=flash,
        confirm=confirm,
        checked_at=_now_naive(),
    ))
    if request.cookies.get(_FLASH_COOKIE):
        response.delete_cookie(_FLASH_COOKIE)
    return response


def _link_preview(device_user_id: str, person: str) -> dict | str:
    """What linking would do, for the confirm step; an error string when it
    cannot be done."""
    device_user_id = device_user_id.strip()
    target = _resolve_person(person)
    if target is None or not device_user_id:
        return "Choose a person and a device ID."
    clash = database.employee_by_device_user_id(device_user_id)
    if clash is not None and clash["id"] != target["id"]:
        return f"{clash['name']} already uses device ID {device_user_id}."
    return {"device_user_id": device_user_id, "employee": target, "punches": _punch_count(device_user_id)}


@bp.post("/people/issues/dismiss")
@_role_required(*access.EDITOR_ROLES)
def id_issues_dismiss():
    _require_csrf()
    key = (request.form.get("key") or "").strip()
    dismissed = _dismissed()
    if request.form.get("restore"):
        dismissed = set()
    elif key.startswith(("name:", "never:")):
        dismissed.add(key)
    database.set_setting(id_issues.DISMISSED_SETTING, json.dumps(sorted(dismissed)))
    forget_issue_count()
    return redirect(url_for("people.id_issues_view"))


@bp.post("/people/issues/link")
@_role_required(*access.EDITOR_ROLES)
def id_issues_link():
    """Give an existing roster person a device ID that already has punches,
    so those past punches start counting for them."""
    _require_csrf()
    device_user_id = (request.form.get("device_user_id") or "").strip()
    target = database.employee_by_id(_int_or_zero(request.form.get("employee_id")))
    if target is None or not device_user_id:
        return _issue_error("Choose a person and a device ID.")
    if not access.can_modify_employee(g.employee, target, "device_user_id"):
        return _issue_error("Only an admin can change an admin's device ID.")
    clash = database.employee_by_device_user_id(device_user_id)
    if clash is not None and clash["id"] != target["id"]:
        return _issue_error(f"{clash['name']} already uses device ID {device_user_id}.")
    message = roster.identity_error(target["name"], target["email"], device_user_id)
    if message:
        return _issue_error(_sentence(message))
    try:
        database.update_employee(target["id"], name=target["name"], email=target["email"], device_user_id=device_user_id)
    except IntegrityError:
        return _issue_error(f"Device ID {device_user_id} is already in use.")
    forget_issue_count()
    return _redirect_with_flash("people.id_issues_view", linked=device_user_id, who=target["name"],
                                punches=_punch_count(device_user_id))
