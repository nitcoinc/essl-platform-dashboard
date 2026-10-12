"""Team / Everyone view and the scoped CSV and Excel exports."""

from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, Response, g, redirect, render_template, request, url_for

from .. import access, export
from .common import (
    TEAM_ROLES,
    XLSX_MIMETYPE,
    _int_list,
    _login_required,
    _now_naive,
    _role_required,
    _target_minutes,
    _today,
    _with_query,
    database,
)

bp = Blueprint("team", __name__)


# --- Team (U8) ---------------------------------------------------------

# Attention order: days that need a look come first, then everyone by name.
_ATTENTION = {"incomplete": 0, "empty": 1}


def _team_rows(query: str) -> list[dict]:
    visible = access.visible_employee_ids(database, g.employee)
    everyone = database.employees()
    names = {person["id"]: person["name"] for person in everyone}
    lowered = query.lower()
    people = [
        person for person in everyone
        if person["id"] in visible
        and (not lowered or lowered in person["name"].lower() or lowered in person["email"].lower())
    ]
    today = _today()
    results = export.day_results(
        database, people, today, today,
        target_minutes=_target_minutes(database), today=today, now=_now_naive(),
    )
    rows = [
        {
            "person": person,
            "manager_name": names.get(person.get("manager_id")),
            "result": results[(person["id"], today)],
        }
        for person in people
    ]
    rows.sort(key=lambda r: (_ATTENTION.get(r["result"].status, 2), r["person"]["name"].lower()))
    return rows


def _export_defaults(today: date) -> dict:
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    return {
        "this_week": (today - timedelta(days=today.weekday()), today),
        "this_month": (month_start, today),
        "last_month": (last_month_end.replace(day=1), last_month_end),
    }


def _render_team(export_error: str | None = None, status: int = 200):
    query = (request.args.get("q") or "").strip()
    everyone_label = g.employee["role"] in access.EVERYONE_ROLES
    today = _today()
    presets = _export_defaults(today)
    context = {
        "employee": g.employee,
        "query": query,
        "rows": _team_rows(query),
        "title": "Everyone" if everyone_label else "Team",
        "today": today,
        "unmapped": database.unmapped_device_user_ids() if everyone_label else [],
        "presets": presets,
        "export_from": request.args.get("from") or presets["this_month"][0].isoformat(),
        "export_to": request.args.get("to") or presets["this_month"][1].isoformat(),
        "export_scope": "selected" if request.args.get("scope") == "selected" else "everyone",
        "selected_ids": set(_int_list(request.args.getlist("employee"))),
        "export_error": export_error,
        "export_open": export_error is not None or "from" in request.args,
    }
    if request.headers.get("HX-Request"):
        return render_template("_team_results.html", oob=True, **context), status
    return render_template("team.html", **context), status


@bp.get("/team")
@_login_required
def team_view():
    if g.employee["role"] not in TEAM_ROLES:
        return redirect(url_for("me.day_view"))
    return _render_team()


@bp.get("/hr")
def hr_redirect() -> Response:
    return redirect(_with_query(url_for("team.team_view")))


# --- Exports (U9) ------------------------------------------------------

def _export(kind: str):
    try:
        start, end = export.parse_range(request.args.get("from"), request.args.get("to"), _today())
    except export.RangeError as exc:
        return _render_team(export_error=str(exc), status=400)
    visible = access.visible_employee_ids(database, g.employee)
    requested = _int_list(request.args.getlist("employee"))
    scope = request.args.get("scope")
    if scope == "selected" and not requested:
        return _render_team(export_error="Select at least one person in the table, or export everyone.", status=400)
    if requested and scope != "everyone":
        wanted = visible & set(requested)  # outside ids drop silently
    else:
        wanted = visible
    everyone = database.employees()
    names = {person["id"]: person["name"] for person in everyone}
    people = [person for person in everyone if person["id"] in wanted]
    rows = export.export_rows(
        database, people, start, end,
        manager_names=names, target_minutes=_target_minutes(database),
        today=_today(), now=_now_naive(),
    )
    filename = f"attendance-{start.isoformat()}-to-{end.isoformat()}.{kind}"
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    if kind == "csv":
        return Response(export.to_csv(rows), mimetype="text/csv", headers=headers)
    return Response(export.to_xlsx(rows), mimetype=XLSX_MIMETYPE, headers=headers)


@bp.get("/export.csv")
@_role_required(*TEAM_ROLES)
def export_csv():
    return _export("csv")


@bp.get("/export.xlsx")
@_role_required(*TEAM_ROLES)
def export_xlsx():
    return _export("xlsx")


@bp.get("/hr/export.csv")
def hr_export() -> Response:
    return redirect(_with_query(url_for("team.export_csv")))
