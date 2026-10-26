"""The signed-in person's (or a visible subject's) day and week."""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, g, make_response, redirect, render_template, request, url_for

from .. import access, charts, config, day_story, export
from ..db import device_ids_of
from ..hours import work_day, work_day_window
from .common import (
    ROLE_LABELS,
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
from .fixes import REPORT_DAYS

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


def _day_context(subject: dict, day, today, request_now, target_minutes: int, day_start_hour: int) -> dict:
    """The context _day_live.html needs for one subject and day; shared by
    /day and the quick-view drawer so both render the same Today."""
    windows = database.device_windows()
    result = _day_for_employee(
        database, subject, day, target_minutes, now=request_now, day_start_hour=day_start_hour,
        windows=windows,
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
        stale_sync = last_synced is not None and request_now - last_synced > timedelta(
            seconds=config.heartbeat_write_seconds() + 60
        )
        if not result.segments:
            # Rollover while inside: say where the open stretch went.
            previous = _day_for_employee(
                database, subject, day - timedelta(days=1), target_minutes, now=request_now,
                day_start_hour=day_start_hour, windows=windows,
            )
            if previous.segments and previous.segments[-1].end is None:
                previous_entry = previous.segments[-1].start
    return dict(
        stretch_seconds=stretch_seconds,
        subject=subject,
        day=day,
        result=result,
        headline=day_story.headline(
            result, target_minutes, now, is_today=is_today, weekend=day.weekday() >= 5
        ),
        today_label=day_story.today_label(result, target_minutes),
        review_label=day_story.review_label(result),
        live_state=day_story.live_state(result, target_minutes),
        left_text=day_story.left_text(result.effective_seconds, target_minutes * 60),
        breaks=day_story.break_view(
            result, now,
            database.punches_for_employee_range(device_ids_of(subject), *work_day_window(day, day_start_hour)),
            database.fix_request_days([subject["id"]], day, day).get((subject["id"], day), ()),
        ),
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


STRIP_DAYS = 7


def _default_strip_end(selected, today):
    """KTD1: today while the selection is in the last STRIP_DAYS days, else
    the selection plus 3 (so it sits mid-strip), never past today."""
    if today - selected < timedelta(days=STRIP_DAYS):
        return today
    return min(selected + timedelta(days=3), today)


def _first_punch_day(subject: dict, day_start_hour: int):
    """The work day of the person's first punch across all their IDs, or None."""
    first = database.first_punch_at(device_ids_of(subject))
    return work_day(first, day_start_hour) if first is not None else None


def _day_strip(
    subject: dict, selected, today, request_now, target_minutes: int, day_start_hour: int,
    strip_end=None, first_day=None,
) -> dict:
    """STRIP_DAYS work days ending at strip_end (KTD1), oldest first, from one
    day_results call, plus the week-paging targets and the picker bounds.
    Earlier/later move strip_end by a week; the selected day stays put."""
    end = min(strip_end or _default_strip_end(selected, today), today)
    if first_day is not None and end < first_day:
        end = min(first_day + timedelta(days=STRIP_DAYS - 1), today)
    start = end - timedelta(days=STRIP_DAYS - 1)
    results = export.day_results(
        database, [subject], start, end, target_minutes=target_minutes, today=today,
        now=request_now, day_start_hour=day_start_hour,
    )
    days = []
    for i in range(STRIP_DAYS):
        day = start + timedelta(days=i)
        result = results.get((subject["id"], day))
        days.append({
            "date": day,
            "is_today": day == today,
            "punched": result is not None and result.status != "empty",
            "selected": day == selected,
        })
    earlier = None
    if first_day is not None and start > first_day:
        earlier = end - timedelta(days=STRIP_DAYS)
    later = min(end + timedelta(days=STRIP_DAYS), today) if end < today else None

    def label(last):
        first = last - timedelta(days=STRIP_DAYS - 1)
        return f"{first.strftime('%d %b')} to {last.strftime('%d %b')}"

    return {
        "days": days,
        "end": end,
        # Chip links carry strip_end unless the strip ends today, so a chip
        # click in an older week keeps the strip where it is.
        "end_param": None if end == today else end.isoformat(),
        "earlier": earlier,
        "earlier_label": label(earlier) if earlier else None,
        "later": later,
        "later_label": label(later) if later else None,
        "first_day": first_day,
        "min": first_day or today,
        "max": today,
    }


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
    windows = database.device_windows()
    for i in range(5):
        day = monday + timedelta(days=i)
        if day > today:
            days.append({"date": day, "result": None})
            continue
        result = _day_for_employee(
            database, subject, day, target_minutes, day_start_hour=day_start_hour, windows=windows
        )
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
    context = _day_context(subject, day, today, request_now, target_minutes, day_start_hour)
    strip = None
    if not partial:
        # Like date, a malformed strip_end falls back to the default.
        first_day = _first_punch_day(subject, day_start_hour)
        strip = _day_strip(
            subject, day, today, request_now, target_minutes, day_start_hour,
            strip_end=_parse_date(request.args.get("strip_end")), first_day=first_day,
        )
        context.update(first_punch_day=first_day)
    context.update(
        active="day",
        employee=g.employee,
        prev_date=day - timedelta(days=1),
        next_date=day + timedelta(days=1),
        strip=strip,
        # U7: only on your own day, and only while it can still be reported.
        can_report=subject["id"] == g.employee["id"] and 0 <= (today - day).days < REPORT_DAYS,
    )
    template = "_day_live.html" if partial else "day.html"
    return _vary_htmx(make_response(render_template(template, **context)))


def _quick_date(name: str):
    """An optional date query arg for /quick: None when absent, 400 when
    malformed or in the future (KTD2)."""
    raw = request.args.get(name)
    if not raw:
        return None
    value = _parse_date(raw)
    if value is None:
        abort(400)
    return value


@bp.get("/quick/<int:employee_id>")
@_login_required
def quick_view(employee_id: int):
    """One person's day for the quick-view drawer, any day up to today
    (KTD2). A plain navigation (no htmx, or a history restore) goes to the
    full Day page for the same day instead."""
    if not access.can_view(database, g.employee, employee_id):
        abort(404)
    subject = database.employee_by_id(employee_id)
    if subject is None:
        abort(404)
    if not _htmx_partial():
        raw = _parse_date(request.args.get("date"))
        return _vary_htmx(redirect(url_for(
            "me.day_view", employee=employee_id, date=raw.isoformat() if raw else None,
        )))
    target_minutes = _target_minutes(database)
    day_start_hour = _day_start_hour(database)
    request_now = _now_naive()
    today = work_day(request_now, day_start_hour)
    day = _quick_date("date") or today
    strip_end = _quick_date("strip_end")
    if day > today:
        abort(400)
    first_day = _first_punch_day(subject, day_start_hour)
    context = _day_context(subject, day, today, request_now, target_minutes, day_start_hour)
    from_people = request.args.get("from") == "people"
    manager = database.employee_by_id(subject["manager_id"]) if subject.get("manager_id") else None
    context.update(
        employee=g.employee,
        manager_name=manager["name"] if manager else None,
        role_label=ROLE_LABELS.get(subject["role"], subject["role"]),
        # Mirrors person_edit's guard, and only offered from People.
        can_edit=from_people and g.employee["role"] in access.EDITOR_ROLES,
        strip=_day_strip(
            subject, day, today, request_now, target_minutes, day_start_hour,
            strip_end=strip_end, first_day=first_day,
        ),
        strip_from="people" if from_people else None,
        first_punch_day=first_day,
    )
    return _vary_htmx(make_response(render_template("_quick_view.html", **context)))
