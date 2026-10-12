from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

Direction = Literal["entry", "exit"]
DayStatus = Literal["empty", "met", "short", "incomplete", "open"]
DirectionSource = Literal["raw_byte", "alternation", "mixed", "none"]

# A work day runs from this hour to the same hour next day (R1). Night shifts
# cross calendar midnight, so a midnight cut splits one shift over two days.
DEFAULT_DAY_START_HOUR = 6


@dataclass(frozen=True)
class Punch:
    timestamp: datetime
    trusted_direction: Direction | None = None


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime | None
    direction: Direction = "entry"


@dataclass(frozen=True)
class DayResult:
    status: DayStatus
    gross_minutes: int | None
    effective_minutes: int
    break_minutes: int
    segments: tuple[Segment, ...]
    direction_source: DirectionSource
    # Review notes for unmatched swipes, e.g. "No exit after 14:33" (R3).
    anomalies: tuple[str, ...] = ()


def compute_day(
    punches: list[Punch],
    *,
    target_minutes: int,
    now: datetime | None = None,
    is_today: bool = False,
) -> DayResult:
    ordered = sorted(punches, key=lambda p: p.timestamp)
    if not ordered:
        return DayResult(
            status="empty",
            gross_minutes=0,
            effective_minutes=0,
            break_minutes=0,
            segments=(),
            direction_source="none",
        )

    resolved, direction_source = _resolve_directions(ordered)
    segments, unmatched, open_start = _pair_segments(resolved)
    anomalies = tuple(_anomaly_text(ts, direction) for ts, direction in unmatched)

    # R4: inside now on today's work day is open, whatever orphans came before.
    if open_start is not None and is_today and now is not None:
        segments.append(Segment(start=open_start, end=None))
        effective = sum(_minutes(s.end - s.start) for s in segments if s.end is not None)
        effective += _minutes(now - open_start)
        gross = _minutes(now - segments[0].start)
        return DayResult(
            status="open",
            gross_minutes=gross,
            effective_minutes=effective,
            break_minutes=gross - effective,
            segments=tuple(segments),
            direction_source=direction_source,
            anomalies=anomalies,
        )

    closed = list(segments)
    if open_start is not None:
        # A trailing entry with no exit on a past day: flag it, keep it visible.
        anomalies += (_anomaly_text(open_start, "entry"),)
        segments.append(Segment(start=open_start, end=None))

    # R3: an unmatched swipe only zeroes the day when nothing paired at all.
    if not closed:
        return DayResult(
            status="incomplete",
            gross_minutes=None,
            effective_minutes=0,
            break_minutes=0,
            segments=tuple(segments),
            direction_source=direction_source,
            anomalies=anomalies,
        )

    effective = sum(_minutes(s.end - s.start) for s in closed)
    gross = _minutes(closed[-1].end - closed[0].start)
    status: DayStatus = "met" if effective >= target_minutes else "short"
    return DayResult(
        status=status,
        gross_minutes=gross,
        effective_minutes=effective,
        break_minutes=gross - effective,
        segments=tuple(segments),
        direction_source=direction_source,
        anomalies=anomalies,
    )


def work_day(timestamp: datetime, day_start_hour: int) -> date:
    """The work day containing ``timestamp`` (naive IST): before the day-start
    hour a time still belongs to the previous calendar day's work day."""
    return (timestamp - timedelta(hours=day_start_hour)).date()


def work_day_window(day: date, day_start_hour: int) -> tuple[datetime, datetime]:
    """The half-open [start, end) window of work day ``day``."""
    start = datetime.combine(day, time(hour=day_start_hour))
    return start, start + timedelta(days=1)


def _resolve_directions(
    ordered: list[Punch],
) -> tuple[list[tuple[datetime, Direction]], DirectionSource]:
    resolved: list[tuple[datetime, Direction]] = []
    expected: Direction = "entry"
    any_trusted = False
    any_alternated = False
    for punch in ordered:
        if punch.trusted_direction is not None:
            direction = punch.trusted_direction
            any_trusted = True
        else:
            direction = expected
            any_alternated = True
        resolved.append((punch.timestamp, direction))
        expected = "exit" if direction == "entry" else "entry"

    if any_trusted and any_alternated:
        source: DirectionSource = "mixed"
    elif any_trusted:
        source = "raw_byte"
    elif any_alternated:
        source = "alternation"
    else:
        source = "none"
    return resolved, source


def _pair_segments(
    resolved: list[tuple[datetime, Direction]],
) -> tuple[list[Segment], list[tuple[datetime, Direction]], datetime | None]:
    """Pair each entry with the next exit. Returns the segments, the swipes
    left unmatched (an entry superseded by another entry, or an exit with no
    entry before it), and a trailing entry still awaiting its exit."""
    segments: list[Segment] = []
    unmatched: list[tuple[datetime, Direction]] = []
    open_start: datetime | None = None
    for timestamp, direction in resolved:
        if direction == "entry":
            if open_start is not None:
                unmatched.append((open_start, "entry"))
            open_start = timestamp
        else:
            if open_start is None:
                unmatched.append((timestamp, "exit"))
                continue
            segments.append(Segment(start=open_start, end=timestamp))
            open_start = None
    return segments, unmatched, open_start


def _anomaly_text(timestamp: datetime, direction: Direction) -> str:
    if direction == "entry":
        return f"No exit after {timestamp:%H:%M}"
    return f"Exit at {timestamp:%H:%M} with no entry"


def _minutes(delta) -> int:
    return int(delta.total_seconds() // 60)
