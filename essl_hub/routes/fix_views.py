"""View helpers shared by the employee report (routes.fixes) and the HR
review (routes.fix_review): the day's rows and swipe list, a filed line as
text, the punch-set hash, and missed-swipe time parsing."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta

from .. import alert_mail
from ..day_story import CORRECTION_FLAGS, punch_basis, punch_flags
from ..db import C3_UNDECODED_BYTE, FIX_ADD_KINDS, device_ids_of
from ..hashing import sha256_hex
from ..hours import CORRECTION_ADDED, CORRECTION_DIRECTION, CORRECTION_IGNORED, work_day_window
from .common import database

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
            "basis": punch_basis(row, row.get("trusted_direction") or ""),
            # A live-log exit the device history has not confirmed yet.
            "unconfirmed": (not correction and row.get("raw_direction_byte") == C3_UNDECODED_BYTE
                            and row.get("source") == "realtime_fallback"),
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
