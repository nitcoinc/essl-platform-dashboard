"""Helpers, constants and decorators shared by the route blueprints."""

from __future__ import annotations

import json
import os
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import abort, current_app, g, make_response, redirect, request, url_for
from werkzeug.local import LocalProxy

from .. import config, roster
from ..config import TIMEZONE_NAME
from ..db import Database, device_ids_of
from ..hours import DEFAULT_DAY_START_HOUR, DayResult, Punch, compute_day, work_day, work_day_window


_TZ = ZoneInfo(TIMEZONE_NAME)

SESSION_COOKIE = "es_session"
PENDING_COOKIE = "es_pending"
SESSION_MAX_AGE = int(timedelta(days=30).total_seconds())
PENDING_MAX_AGE = int(timedelta(minutes=5).total_seconds())

API_RANGE_MAX_DAYS = 92
API_PAGE_SIZE_MAX = 1000

# Derived from the parser's own column map, so the downloadable template can
# never drift from what the importer actually accepts.
ROSTER_TEMPLATE_HEADER = tuple(
    aliases[0]
    for aliases in (*roster.REQUIRED_COLUMNS.values(), *roster.OPTIONAL_COLUMNS.values())
)
ROSTER_INSTRUCTIONS = (
    ("name", "Required. Full name as it should appear in the hub."),
    ("work email", "Required. The address the person signs in with."),
    ("device user id", "Required. The user ID enrolled on the attendance device."),
    ("manager email", "Optional. Work email of the person they report to. Leave blank to clear; leave the column out to keep current managers."),
    ("role", "Optional. employee, manager or hr. Leave blank to keep the current role. Admin is set on the People page."),
)
ROSTER_EXAMPLE_ROW = ("Anil Kumar", "anil.kumar@yourcompany.com", "173", "priya.rao@yourcompany.com", "employee")
THEME_COOKIE = "theme"
THEMES = ("light", "dark", "system")
THEME_MAX_AGE = int(timedelta(days=365).total_seconds())

ROLE_LABELS = {"employee": "Employee", "manager": "Manager", "hr": "HR", "admin": "Admin"}
TEAM_ROLES = ("manager", "hr", "admin")
XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _client_ip() -> str | None:
    """The address sign-in rate limiting keys on (KTD16).

    Behind the Cloudflare Tunnel every request arrives from cloudflared, so
    remote_addr is useless; Cloudflare puts the real client in
    CF-Connecting-IP. That header is trusted only when ESSL_BEHIND_TUNNEL=1,
    so a directly published port cannot be fed forged addresses.
    """
    if os.getenv("ESSL_BEHIND_TUNNEL", "").strip() == "1":
        forwarded = (request.headers.get("CF-Connecting-IP") or "").strip()
        if forwarded:
            return forwarded
    return request.remote_addr


# The app's Database, set by create_app. A proxy so route bodies read like the
# old closures; pass database._get_current_object() to anything that outlives
# the request.
database: Database = LocalProxy(lambda: current_app.extensions["essl_database"])


def _max_upload_mb() -> int:
    return current_app.extensions["essl_max_upload_mb"]


def _secure_cookies() -> bool:
    return os.getenv("ESSL_FORCE_HTTPS_COOKIES", "true").strip().lower() != "false"


def _require_csrf() -> None:
    # htmx sends the token as a header (hx-headers on <body>); plain
    # forms send it as a hidden field. Either is accepted (KTD15).
    token = request.headers.get("X-CSRF-Token") or request.form.get("csrf") or ""
    if not g.csrf_token or not secrets.compare_digest(token, g.csrf_token):
        abort(400)


def _login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.employee is None:
            if request.headers.get("HX-Request"):
                # A 302 would be followed by htmx and land the sign-in form
                # inside the swapped region; send the whole tab instead.
                response = make_response("", 200)
                response.headers["HX-Redirect"] = url_for("auth.signin")
                return response
            return redirect(url_for("auth.signin"))
        return view(*args, **kwargs)

    return wrapper


def _role_required(*roles: str):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.employee is None:
                return redirect(url_for("auth.signin"))
            if g.employee["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def _htmx_partial() -> bool:
    """True when htmx wants a fragment. A history-restore request (back or
    forward after a cache miss) also carries HX-Request but must get the
    full page, or the shell is lost."""
    return bool(request.headers.get("HX-Request")) and not request.headers.get(
        "HX-History-Restore-Request"
    )


def _vary_htmx(response):
    """Mark a view that answers HX-Request with a fragment, so no cache
    hands the fragment to a plain navigation."""
    response.vary.add("HX-Request")
    return response


def _home_for(employee: dict) -> str:
    """Landing page per role (plan: Navigation by role)."""
    if employee["role"] == "employee":
        return url_for("me.day_view")
    return url_for("team.team_view")


def _target_minutes(database: Database) -> int:
    return int(database.setting("daily_target_minutes", "480"))


def _day_start_hour(database: Database) -> int:
    """The work-day start hour (R1); a missing or corrupt value falls back."""
    try:
        hour = int(database.setting("day_start_hour", str(DEFAULT_DAY_START_HOUR)))
    except (TypeError, ValueError):
        return DEFAULT_DAY_START_HOUR
    return hour if 0 <= hour <= 23 else DEFAULT_DAY_START_HOUR


def _day_start_label(day_start_hour: int) -> str:
    return f"{day_start_hour:02d}:00"


def _roster_rows(filename: str, raw: bytes) -> list[roster.RosterRow]:
    """Normalize an uploaded roster file into the (row, name, email, device ID)
    rows the CSV path already produces, branching on the file extension before
    any decoding so a wrong-format upload reports a per-file error (R1, R2)."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension == "csv":
        try:
            return roster.parse_csv(raw.decode("utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise roster.RosterFileError(
                "file is not UTF-8 text; if this came from Excel, upload it as .xlsx"
            ) from exc
    if extension == "xlsx":
        return roster.parse_xlsx(raw)
    if extension == "xls":
        raise roster.RosterFileError(
            "this is the older .xls format; open it in Excel and save it as .xlsx"
        )
    raise roster.RosterFileError(
        "unsupported file type; upload a .csv or .xlsx roster"
    )


def _int_or_zero(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _sentence(message: str) -> str:
    """Parser messages are lower-case clauses; pages show them as sentences."""
    message = message.strip()
    if not message:
        return message
    message = message[0].upper() + message[1:]
    return message if message.endswith(".") else message + "."


def _with_query(path: str) -> str:
    """Keep the whole query string (repeated params too) across a redirect."""
    query = request.query_string.decode("latin-1")
    return f"{path}?{query}" if query else path


def _ago(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h ago"
    return f"{hours // 24} days ago"


def _now_naive() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


def _int_list(values: list[str]) -> list[int]:
    return [int(v) for v in values if v.strip().isdigit()]


def _today(day_start_hour: int | None = None) -> date:
    """The work day containing now (R2): at 03:00 that is still yesterday's."""
    if day_start_hour is None:
        day_start_hour = _day_start_hour(database)
    return work_day(_now_naive(), day_start_hour)


def _people_for_range(database, start: date, end: date, day_start_hour: int) -> list[dict]:
    """Active people plus archived people with punches in the range, so
    exports and the hours API never drop past hours (KTD6)."""
    window = (work_day_window(start, day_start_hour)[0], work_day_window(end, day_start_hour)[1])
    people = database.employees() + database.archived_employees_with_punches(*window)
    return sorted(people, key=lambda person: person["name"])


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _date_range(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _day_for_employee(
    database: Database,
    employee: dict,
    day: date,
    target_minutes: int,
    now: datetime | None = None,
    *,
    day_start_hour: int | None = None,
) -> DayResult:
    """Score one work day. Pass ``now`` to share one instant across a request,
    so every figure on a page is measured from the same second (R17/U11).
    Loop callers pass ``day_start_hour`` so the setting is read once."""
    if day_start_hour is None:
        day_start_hour = _day_start_hour(database)
    start, end = work_day_window(day, day_start_hour)
    rows = database.punches_for_employee_range(device_ids_of(employee), start, end)
    punches = [
        Punch(timestamp=row["event_timestamp"], trusted_direction=row["trusted_direction"])
        for row in rows
    ]
    now = now or _now_naive()
    is_today = day == work_day(now, day_start_hour)
    now = now if is_today else None
    return compute_day(punches, target_minutes=target_minutes, now=now, is_today=is_today)


def _theme() -> str:
    """The viewer's theme from the `theme` cookie; light unless set."""
    value = request.cookies.get(THEME_COOKIE, "")
    return value if value in THEMES else "light"


def _safe_next(target: str | None, fallback: str) -> str:
    """A same-site path to return to; anything with a scheme or host (or a
    protocol-relative //host, or a backslash trick) falls back."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return fallback
    return target


# --- Sync health: collector and backups (R10) ---------------------------

_TONE_RANK = {"ok": 0, "warn": 1, "bad": 2}
COLLECTOR_STALE = timedelta(minutes=10)
SPOOL_BAD = timedelta(minutes=60)
BACKUP_WARN_INTERVALS = 2  # green below this many backup intervals since last_ok_at
BACKUP_BAD_INTERVALS = 6
BACKUP_REPEATED_ERRORS = 3  # this many failed cycles in a row = red


def _parse_ts(value: object) -> datetime | None:
    """Naive IST datetime from a status-file value; None when unusable.

    Status files write naive local (IST) times; an aware value is converted."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_TZ).replace(tzinfo=None)
    return parsed


def _ist(value: datetime | None) -> str:
    return f"{value.strftime('%d %b %Y, %H:%M:%S')} IST" if value else "never"


def _collector_signal(status: object, now: datetime) -> dict:
    if not isinstance(status, dict):
        return {"tone": "bad", "summary": "Status file missing or unreadable", "label": "Collector status unknown"}
    last_poll = _parse_ts(status.get("last_poll"))
    try:
        depth = int(status.get("spool_depth") or 0)
        dead = int(status.get("dead_count") or 0)
    except (TypeError, ValueError):
        return {"tone": "bad", "summary": "Status file unreadable", "label": "Collector status unknown"}
    oldest = _parse_ts(status.get("spool_oldest_at"))
    if last_poll is None or now - last_poll > COLLECTOR_STALE:
        when = _ago(now - last_poll) if last_poll else "never"
        return {"tone": "bad", "summary": f"Last poll {when}", "label": "Collector stopped reporting"}
    if dead > 0:
        noun = "punch" if dead == 1 else "punches"
        return {"tone": "bad", "summary": f"{dead} {noun} dead-lettered", "label": f"Collector: {dead} dead {noun}"}
    if depth > 0:
        noun = "punch" if depth == 1 else "punches"
        if oldest is not None and now - oldest > SPOOL_BAD:
            return {"tone": "bad", "summary": f"{depth} {noun} spooled, oldest {_ago(now - oldest)}",
                    "label": "Collector spool stuck"}
        return {"tone": "warn", "summary": f"{depth} {noun} spooled", "label": f"Collector: {depth} spooled"}
    return {"tone": "ok", "summary": f"Last poll {_ago(now - last_poll)}", "label": "Collector OK"}


def _backup_signal(status: object, now: datetime, *, r2_configured: bool, minutes: int) -> dict:
    if not r2_configured:
        return {"tone": "neutral", "summary": "Off", "label": "Backups off",
                "error": "R2 is not configured (R2_* variables), so nothing is copied off-site."}
    if not isinstance(status, dict):
        return {"tone": "bad", "summary": "No backup yet", "label": "No backup yet", "error": None}
    error = status.get("last_error") or None
    last_ok = _parse_ts(status.get("last_ok_at"))
    entries = [e for e in status.get("entries") or [] if isinstance(e, dict)]
    tail = entries[-BACKUP_REPEATED_ERRORS:]
    when = f"Last backup {_ago(now - last_ok)}" if last_ok else "No backup yet"
    if status.get("blocked"):
        return {"tone": "bad", "summary": f"Blocked · {when}", "error": error,
                "label": "Backups blocked: different database"}
    if len(tail) == BACKUP_REPEATED_ERRORS and all(e.get("status") != "ok" for e in tail):
        return {"tone": "bad", "summary": f"Failing · {when}", "label": "Backups failing", "error": error}
    if last_ok is None:
        return {"tone": "bad", "summary": when, "label": "No backup yet", "error": error}
    age = now - last_ok
    interval = timedelta(minutes=max(1, minutes))
    tone = ("ok" if age < BACKUP_WARN_INTERVALS * interval
            else "warn" if age < BACKUP_BAD_INTERVALS * interval else "bad")
    return {"tone": tone, "summary": when, "label": f"Backup {_ago(age)}", "error": error}


def sync_health(
    collector_status: object,
    backup_status: object,
    now: datetime,
    *,
    r2_configured: bool = True,
    backup_minutes: int = 15,
) -> dict:
    """Summarise the collector and backups signals.

    Pure: callers read the status files (missing/corrupt passed as None).
    Each signal gets a tone; the worst wins and the label names it. A neutral
    signal (backups Off) never sets the top-bar tone."""
    collector = collector_status if isinstance(collector_status, dict) else None
    backup = backup_status if isinstance(backup_status, dict) else None
    collector_poll = _parse_ts(collector.get("last_poll")) if collector else None
    details = []
    details.append(
        {"key": "collector", "name": "Collector", **_collector_signal(collector, now),
         "times": [
             ("Last poll", _ist(collector_poll)),
             ("Oldest spooled punch", _ist(_parse_ts(collector.get("spool_oldest_at")) if collector else None)),
         ]})
    details.append(
        {"key": "backup", "name": "Backups",
         **_backup_signal(backup, now, r2_configured=r2_configured, minutes=backup_minutes),
         "times": [("Last backup", _ist(_parse_ts(backup.get("last_ok_at")) if backup else None)),
                   ("Last upload", _ist(_parse_ts(backup.get("last_upload_at")) if backup else None))]})
    ranked = [d for d in details if d["tone"] in _TONE_RANK]
    worst = max(ranked, key=lambda d: _TONE_RANK[d["tone"]])  # first worst wins ties
    label = "All systems healthy" if worst["tone"] == "ok" else worst["label"]
    title = " · ".join(
        f"{d['name']}: {d['summary']} ({d['times'][0][0].lower()} {d['times'][0][1]})" for d in details
    )
    return {"tone": worst["tone"], "label": label, "title": title, "signal": worst["key"], "details": details}


def _read_json(path: str) -> object:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def current_sync_health(db: object) -> tuple[dict, list[dict]]:
    """Read the status files tolerantly and summarise; also returns backup entries."""
    from ..backup import backup_status_path

    raw = _read_json(backup_status_path())
    status = raw if isinstance(raw, dict) else None
    entries = [e for e in (status or {}).get("entries") or [] if isinstance(e, dict)]
    health = sync_health(
        _read_json(config.collector_status_path()),
        status,
        _now_naive(),
        # Written by the backup service; the web container has no R2 settings.
        # No status file yet means the service hasn't run: "No backup yet", not Off.
        r2_configured=(status or {}).get("r2_configured", True) is not False,
        backup_minutes=int((status or {}).get("interval_minutes") or config.backup_minutes()),
    )
    return health, entries
