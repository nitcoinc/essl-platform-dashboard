"""HRMS read-only API and health check."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, request

from .. import hashing
from .common import (
    API_PAGE_SIZE_MAX,
    API_RANGE_MAX_DAYS,
    _date_range,
    _day_for_employee,
    _day_start_hour,
    _day_start_label,
    _parse_date,
    _now_naive,
    _people_for_range,
    _target_minutes,
    database,
)

_logger = logging.getLogger(__name__)
TOUCH_INTERVAL = timedelta(hours=1)

bp = Blueprint("api", __name__)


# --- HRMS read-only API --------------------------------------------

@bp.get("/api/v1/hours")
def api_hours() -> tuple[Response, int] | Response:
    key_row = _authenticate_api_key()
    if key_row is None:
        return jsonify({"error": "A valid Authorization: Bearer <key> header is required"}), 401

    start = _parse_date(request.args.get("from"))
    end = _parse_date(request.args.get("to"))
    if start is None or end is None or end < start:
        return jsonify({"error": "from and to must be valid dates with from <= to"}), 400
    if (end - start).days + 1 > API_RANGE_MAX_DAYS:
        return jsonify({"error": f"Range may not exceed {API_RANGE_MAX_DAYS} days"}), 400

    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 200, type=int), 1), API_PAGE_SIZE_MAX)
    target_minutes = _target_minutes(database)

    now = _now_naive()
    day_start_hour = _day_start_hour(database)
    windows = database.device_windows()
    rows = []
    for employee in _people_for_range(database, start, end, day_start_hour):
        for day in _date_range(start, end):
            result = _day_for_employee(
                database, employee, day, target_minutes, now=now, day_start_hour=day_start_hour,
                windows=windows,
            )
            rows.append(
                {
                    "date": day.isoformat(),
                    "employee_name": employee["name"],
                    "employee_email": employee["email"],
                    "device_user_id": employee["device_user_id"],
                    "gross_minutes": result.gross_minutes,
                    "effective_minutes": result.effective_minutes,
                    "effective_seconds": result.effective_seconds,
                    "status": result.status,
                    "anomalies": list(result.anomalies),
                }
            )
    rows.sort(key=lambda r: (r["date"], r["employee_name"]))
    total = len(rows)
    offset = (page - 1) * per_page
    page_rows = rows[offset : offset + per_page]
    _touch_api_key(key_row)
    return jsonify(
        {
            "results": page_rows,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": max(1, (total + per_page - 1) // per_page),
            # Work days run from this local time to the same time next day (R6).
            "day_start": _day_start_label(day_start_hour),
        }
    )


CHECKINS_LIMIT_MAX = 1000
_LOG_TYPES = {"entry": "IN", "exit": "OUT"}


@bp.get("/api/v1/checkins")
def api_checkins() -> tuple[Response, int] | Response:
    """Frappe HR Employee Checkin-shaped punches for pull-style integrations.

    Each record carries exactly the add_log_based_on_employee_field arguments.
    Page with ``since=<next_since>`` (exclusive); times are local, no offset."""
    key_row = _authenticate_api_key()
    if key_row is None:
        return jsonify({"error": "A valid Authorization: Bearer <key> header is required"}), 401
    since = None
    raw_since = request.args.get("since")
    if raw_since:
        try:
            since = datetime.fromisoformat(raw_since)
        except ValueError:
            since = None
        if since is None or since.tzinfo is not None:
            return jsonify({"error": "since must be a local ISO datetime without offset"}), 400
    limit = min(max(request.args.get("limit", 500, type=int), 1), CHECKINS_LIMIT_MAX)
    rows = database.checkins_since(since=since, limit=limit)
    if len(rows) == limit and rows[0]["event_timestamp"] != rows[-1]["event_timestamp"]:
        # ``since`` is exclusive: never split a second across pages.
        last = rows[-1]["event_timestamp"]
        rows = [row for row in rows if row["event_timestamp"] != last]
    results = []
    for row in rows:
        record = {
            "employee_field_value": row["device_user_id"],
            "employee_fieldname": "attendance_device_id",
            "timestamp": row["event_timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
            "time": row["event_timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
            "device_id": row["device_id"],
            "log_type": _LOG_TYPES.get(row["trusted_direction"] or ""),
        }
        results.append(record)
    _touch_api_key(key_row)
    return jsonify(
        {
            "results": results,
            "count": len(results),
            "next_since": results[-1]["timestamp"] if results else raw_since,
        }
    )


@bp.get("/api/health")
def health() -> Response:
    return jsonify({"ok": True})
def _authenticate_api_key() -> dict | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    key_hash = hashing.sha256_hex(header[len("Bearer ") :])
    return database.api_key_by_hash(key_hash)


def _touch_api_key(key_row: dict) -> None:
    """Record key use at most once an hour, best-effort: a failed write (database
    busy, or anything else) must never fail a read-only pull (KTD11)."""
    last_used = key_row.get("last_used_at")
    if last_used is not None and _now_naive() - last_used < TOUCH_INTERVAL:
        return
    try:
        database.touch_api_key(key_row["key_hash"])
    except Exception:  # noqa: BLE001 - bookkeeping only, the response matters more
        _logger.warning("Could not record API key use for %s", key_row.get("prefix"), exc_info=True)
