"""The signed-in person's (or a visible subject's) day and week."""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, g, make_response, render_template, request

from .. import access, charts, day_story
from ..hours import work_day
from .common import (
    _TZ,
    _day_for_employee,
    _day_start_hour,
    _day_start_label,
    _htmx_partial,
    _login_required,
    _now_naive,
    _parse_date,
    _target_minutes,
    _today,
    _vary_htmx,
    database,
)

bp = Blueprint("me", __name__)


# --- Employee ------------------------------------------------------

def _subject() -> dict:
    """The employee whose data /day and /week show: ?employee=<id> when the
    viewer may see them, else 404 for not-visible and nonexistent alike."""
    raw = request.args.get("employee")
    if raw is None or raw == "":
        return g.employee
    try:
        target_id = int(raw)
    except ValueError:
        abort(404)
    if not access.can_view(database, g.employee, target_id):
        abort(404)
    target = database.employee_by_id(target_id)
    if target is None:
        abort(404)
    return target


@bp.get("/week")
@_login_required
def week_view() -> str:
    subject = _subject()
    offset = request.args.get("offset", 0, type=int)
    target_minutes = _target_minutes(database)
    day_start_hour = _day_start_hour(database)
    today = _today(day_start_hour)
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    days = []
    week_total = 0
    for i in range(5):
        day = monday + timedelta(days=i)
        if day > today:
            days.append({"date": day, "result": None})
            continue
        result = _day_for_employee(database, subject, day, target_minutes, day_start_hour=day_start_hour)
        week_total += result.effective_minutes
        days.append({"date": day, "result": result})
    chart = charts.render_week_bars(
        days,
        target_minutes,
        label=f"Effective hours, week of {monday.strftime('%d %B')}",
        today=today,
    )
    return render_template(
        "week.html",
        chart=chart,
        active="week",
        employee=g.employee,
        subject=subject,
        days=days,
        week_total_minutes=week_total,
        target_minutes=target_minutes,
        offset=offset,
        today=today,
        day_start=_day_start_label(day_start_hour),
    )


@bp.get("/day")
@_login_required
def day_view():
    target_minutes = _target_minutes(database)
    subject = _subject()
    day_start_hour = _day_start_hour(database)
    # One instant per request: the result, the stretch and the server epoch
    # all derive from it, so ring and stretch never disagree by a second.
    request_now = _now_naive()
    today = work_day(request_now, day_start_hour)
    day = _parse_date(request.args.get("date")) or today
    partial = _htmx_partial()
    if partial and day != today:
        # Only Today polls, so a fragment request for another day means the
        # work day rolled over: reload for the right title, date and pager.
        response = make_response("", 200)
        response.headers["HX-Refresh"] = "true"
        return _vary_htmx(response)
    result = _day_for_employee(
        database, subject, day, target_minutes, now=request_now, day_start_hour=day_start_hour
    )
    is_today = day == today
    now = request_now if is_today else None
    stretch_seconds = 0
    if result.status == "open" and now is not None:
        stretch_seconds = max(0, int((now - result.segments[-1].start).total_seconds()))
    reached = day_story.target_reached_at(result, target_minutes, now)
    last_synced = None
    stale_sync = False
    previous_entry = None
    if is_today:
        last_synced = database.min_device_last_success()
        stale_sync = last_synced is not None and request_now - last_synced > timedelta(minutes=5)
        if not result.segments:
            # Rollover while inside: say where the open stretch went.
            previous = _day_for_employee(
                database, subject, day - timedelta(days=1), target_minutes, now=request_now,
                day_start_hour=day_start_hour,
            )
            if previous.segments and previous.segments[-1].end is None:
                previous_entry = previous.segments[-1].start
    context = dict(
        stretch_seconds=stretch_seconds,
        active="day",
        employee=g.employee,
        subject=subject,
        day=day,
        prev_date=day - timedelta(days=1),
        next_date=day + timedelta(days=1),
        result=result,
        headline=day_story.headline(
            result, target_minutes, now, is_today=is_today, weekend=day.weekday() >= 5
        ),
        today_label=day_story.today_label(result, target_minutes),
        review_label=day_story.review_label(result),
        live_state=day_story.live_state(result, target_minutes),
        left_text=day_story.left_text(result.effective_seconds, target_minutes * 60),
        log=day_story.log_items(result, now),
        ring_percent=day_story.ring_percent(result, target_minutes),
        finish_at=reached if result.status == "open" else None,
        last_synced=last_synced,
        stale_sync=stale_sync,
        previous_entry=previous_entry,
        request_now=request_now,
        server_epoch_ms=int(request_now.replace(tzinfo=_TZ).timestamp() * 1000),
        target_minutes=target_minutes,
        today=today,
        day_start=_day_start_label(day_start_hour),
    )
    template = "_day_live.html" if partial else "day.html"
    return _vary_htmx(make_response(render_template(template, **context)))
