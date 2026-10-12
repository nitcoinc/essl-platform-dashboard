"""Attendance over a date range for a set of people: Team's today column and
the CSV/Excel exports (U8, U9).

One query loads every punch for the set and range; rows are grouped here and
each person-day goes through ``compute_day``, so the cost stays constant in
queries whatever the people x days product.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta

import openpyxl
from openpyxl.styles import Font

from .hours import DEFAULT_DAY_START_HOUR, DayResult, Punch, compute_day, work_day, work_day_window

MAX_RANGE_DAYS = 93
HEADER = (
    "date",
    "name",
    "email",
    "device_user_id",
    "manager",
    "gross_minutes",
    "effective_minutes",
    "status",
)
_NUMERIC_COLUMNS = {"gross_minutes", "effective_minutes"}
# Spreadsheet apps treat these leading characters as a formula (CSV injection).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class RangeError(ValueError):
    """A from/to pair the export refuses; the message is shown to the user."""


def parse_range(from_raw: str | None, to_raw: str | None, default: date) -> tuple[date, date]:
    def parse(raw: str | None, label: str) -> date:
        if not raw:
            return default
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise RangeError(f"The {label} date is not a valid date. Use YYYY-MM-DD.") from None

    start, end = parse(from_raw, "From"), parse(to_raw, "To")
    if start > end:
        raise RangeError("The From date is after the To date. Swap them or pick a new range.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise RangeError(
            f"That range is {(end - start).days + 1} days. Export at most {MAX_RANGE_DAYS} days at a time."
        )
    return start, end


def day_results(
    database,
    people: list[dict],
    start: date,
    end: date,
    *,
    target_minutes: int,
    today: date,
    now: datetime | None,
    day_start_hour: int = DEFAULT_DAY_START_HOUR,
) -> dict[tuple[int, date], DayResult]:
    """compute_day for every (person, work day) in the range, from one punch
    query spanning the first day's window start to the last day's window end.
    ``now`` is only used for ``today``, matching the single-day views."""
    rows = database.punches_for_device_users_range(
        [p["device_user_id"] for p in people],
        work_day_window(start, day_start_hour)[0],
        work_day_window(end, day_start_hour)[1],
    )
    grouped: dict[tuple[str, date], list[Punch]] = defaultdict(list)
    for row in rows:
        stamp = row["event_timestamp"]
        grouped[(row["device_user_id"], work_day(stamp, day_start_hour))].append(
            Punch(timestamp=stamp, trusted_direction=row["trusted_direction"])
        )
    results = {}
    for person in people:
        day = start
        while day <= end:
            is_today = day == today
            results[(person["id"], day)] = compute_day(
                grouped.get((person["device_user_id"], day), []),
                target_minutes=target_minutes,
                now=now if is_today else None,
                is_today=is_today,
            )
            day += timedelta(days=1)
    return results


def export_rows(
    database,
    people: list[dict],
    start: date,
    end: date,
    *,
    manager_names: dict[int, str],
    target_minutes: int,
    today: date,
    now: datetime | None,
    day_start_hour: int = DEFAULT_DAY_START_HOUR,
) -> list[list[object]]:
    """Rows in HEADER order, by date then name. Minutes stay ints (gross is
    None when the day cannot be computed)."""
    results = day_results(
        database, people, start, end, target_minutes=target_minutes, today=today, now=now,
        day_start_hour=day_start_hour,
    )
    ordered = sorted(people, key=lambda p: (p["name"].lower(), p["id"]))
    rows = []
    day = start
    while day <= end:
        for person in ordered:
            result = results[(person["id"], day)]
            rows.append(
                [
                    day.isoformat(),
                    person["name"],
                    person["email"],
                    person["device_user_id"],
                    manager_names.get(person.get("manager_id"), ""),
                    result.gross_minutes,
                    result.effective_minutes,
                    result.status,
                ]
            )
        day += timedelta(days=1)
    return rows


def safe_text(value: object) -> object:
    """Neutralize text a spreadsheet would run as a formula. Numbers pass."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _cells(row: list[object]) -> list[object]:
    return [
        value if HEADER[i] in _NUMERIC_COLUMNS else safe_text(value)
        for i, value in enumerate(row)
    ]


def to_csv(rows: list[list[object]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADER)
    for row in rows:
        writer.writerow(["" if v is None else v for v in _cells(row)])
    return output.getvalue()


def to_xlsx(rows: list[list[object]]) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Attendance"
    sheet.append(HEADER)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    for row in rows:
        sheet.append(_cells(row))
        # Text cells are written as strings, never parsed as formulas.
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    for letter, width in zip("ABCDEFGH", (12, 24, 30, 14, 24, 14, 17, 12)):
        sheet.column_dimensions[letter].width = width
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()
