"""U4: the live "out too long" check and the next-morning sequence check.

Pure functions over one employee-day's DayResult (KTD1). The alerts service
loads the day, the baseline and the settings, and logs the reason code a
check returns in shadow mode. Nothing here reads a clock or the database.
Named apart from alert_mail.live_check / morning_check, which render mail.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal

from .alert_mail import DOUBLE_IN, MISSING_FINAL_OUT, OUT_WITHOUT_IN, UNCLEAR_GAP, MorningIssue
from .baselines import Baseline
from .db import C3_UNDECODED_BYTE
from .hours import DayResult

# R4: the live email goes out once the out-stretch reaches limit + grace, so
# no state is kept between ticks.
GRACE_MINUTES = 15
# R4 / R15: default alert hours, [start, end) in IST wall-clock hours.
DEFAULT_ALERT_START_HOUR = 9
DEFAULT_ALERT_END_HOUR = 20
# The raw_json source of a C3 realtime row whose in/out byte was 15 (KTD9).
C3_REALTIME_SOURCE = "realtime_fallback"
# The raw_state note of a C3 row the door denied (KTD5).
DOOR_DENIED_NOTE = "door denied:"

LiveReason = Literal[
    "outside_alert_hours",
    "no_punch_today",
    "no_baseline",
    "no_entry_today",
    "not_out",
    "under_limit",
    "after_usual_last_out",
]
HOLD = "hold"


@dataclass(frozen=True)
class LiveAlert:
    out_since: datetime
    out_minutes: int


def live_decision(
    day: DayResult,
    baseline: Baseline | None,
    *,
    now: datetime,
    day_start: datetime,
    alert_start_hour: int = DEFAULT_ALERT_START_HOUR,
    alert_end_hour: int = DEFAULT_ALERT_END_HOUR,
    door_denied: Collection[datetime] = (),
) -> LiveAlert | LiveReason:
    """R4, in the order of the live decision diagram. ``day`` is today's
    result computed with ``is_today=True`` and ``now``; ``day_start`` is
    today's work-day start; ``door_denied`` holds the timestamps of today's
    door-denied rows (``is_door_denied``), which never count as out (KTD9).

    Currently out: the latest resolved swipe (segment ends and starts plus
    unpaired swipes; collapsed duplicates never count) is an exit, and the day
    has at least one swipe-in. The out-stretch runs from that exit. A C3
    realtime exit (byte 15) already reaches compute_day as a trusted exit."""
    if not _in_hours(now, alert_start_hour, alert_end_hour):
        return "outside_alert_hours"
    if day.status == "empty":
        return "no_punch_today"
    if baseline is None or not baseline.live_eligible:
        return "no_baseline"
    if not day.segments:
        # Only orphan exits: R4 alerts people who swiped in today.
        return "no_entry_today"
    out_since = _out_since(day)
    if out_since is None or out_since in set(door_denied):
        return "not_out"
    out_seconds = int((now - out_since).total_seconds())
    if out_seconds < (baseline.limit_minutes + GRACE_MINUTES) * 60:
        return "under_limit"
    if (now - day_start).total_seconds() / 60 >= baseline.last_out.p10:
        return "after_usual_last_out"
    return LiveAlert(out_since, out_seconds // 60)


def morning_issues(day: DayResult, *, has_unconfirmed_c3_guess: bool) -> list[MorningIssue] | str:
    """R5 / R8: yesterday's sequence problems in time order, [] for a clean
    or empty day, or HOLD while a C3 direction guess awaits a history read.

    Mapping from compute_day (``day`` computed with ``is_today=False``):
    - the trailing entry (last segment with no end; also in anomalies and
      ``unpaired``) is one MISSING_FINAL_OUT;
    - every unpaired swipe sitting inside an unclear gap is covered by that
      gap's one UNCLEAR_GAP (start, end), never reported again on its own;
    - any other unpaired exit is OUT_WITHOUT_IN;
    - any other unpaired entry was superseded by the next entry, so it is
      DOUBLE_IN (that entry, the next one)."""
    if has_unconfirmed_c3_guess:
        return HOLD
    issues: list[MorningIssue] = []
    trailing = None
    if day.segments and day.segments[-1].end is None:
        trailing = day.segments[-1].start
        issues.append(MorningIssue(MISSING_FINAL_OUT, trailing))

    in_gaps: set[tuple[datetime, str]] = set()
    for gap in day.gaps:
        if gap.kind == "unclear":
            issues.append(MorningIssue(UNCLEAR_GAP, gap.start, gap.end))
            in_gaps.update(gap.unpaired)

    entries = sorted({s.start for s in day.segments} | {ts for ts, d in day.unpaired if d == "entry"})
    for swipe in day.unpaired:
        timestamp, direction = swipe
        if swipe in in_gaps or (direction == "entry" and timestamp == trailing):
            continue
        if direction == "exit":
            issues.append(MorningIssue(OUT_WITHOUT_IN, timestamp))
        else:
            following = next((e for e in entries if e > timestamp), None)
            issues.append(MorningIssue(DOUBLE_IN, timestamp, following))
    return sorted(issues, key=lambda issue: issue.start)


def is_door_denied(row: Mapping) -> bool:
    """A C3 row the door denied (anti-passback or interlock), by its note."""
    return DOOR_DENIED_NOTE in str(row.get("raw_state") or "")


def is_unconfirmed_c3_guess(row: Mapping) -> bool:
    """db._FRAPPE_HELD_SQL in Python, minus its 24 h Frappe give-up: a C3
    realtime row still carrying the undecoded byte. A history read replaces
    the byte (db.fill_punch_directions), which confirms it. ``raw_json`` may be
    stored text or already parsed; a row with neither byte nor source is not
    a guess (the SQL's COALESCE)."""
    if row.get("raw_direction_byte") is None or int(row["raw_direction_byte"]) != C3_UNDECODED_BYTE:
        return False
    raw = row.get("raw_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return False
    return isinstance(raw, Mapping) and raw.get("source") == C3_REALTIME_SOURCE


def has_unconfirmed_c3_guess(rows: Iterable[Mapping]) -> bool:
    return any(is_unconfirmed_c3_guess(row) for row in rows)


def _out_since(day: DayResult) -> datetime | None:
    """The exit that began the current out-stretch, or None when the latest
    resolved swipe is an entry (or the day is open)."""
    if day.status == "open" or day.segments[-1].end is None:
        return None
    swipes = [(s.start, "entry") for s in day.segments]
    swipes += [(s.end, "exit") for s in day.segments]
    swipes += list(day.unpaired)
    # On a tie the entry sorts last, reading the person as inside.
    timestamp, direction = max(swipes, key=lambda sw: (sw[0], sw[1] == "entry"))
    return timestamp if direction == "exit" else None


def _in_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """[start, end) on the wall clock; a window that wraps midnight works."""
    start, end, current = time(start_hour), time(end_hour), now.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end
