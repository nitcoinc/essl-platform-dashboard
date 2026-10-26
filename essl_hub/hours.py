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

# KTD9: a direction-less swipe within the quick-swipe window of the previous
# kept swipe, or a same-direction re-swipe, is a duplicate: stored, shown
# "not counted", never paired. Known-direction swipes of a different direction
# always count (magnetic door: every In and Out is a real record).
# KTD3: the window is per device; this is the default (existing devices).
DUPLICATE_WINDOW_SECONDS = 60

GapKind = Literal["break", "unclear"]


@dataclass(frozen=True)
class Punch:
    timestamp: datetime
    trusted_direction: Direction | None = None
    # KTD3: the quick-swipe window of this punch's device; 0 counts every swipe.
    window_seconds: int = DUPLICATE_WINDOW_SECONDS


@dataclass(frozen=True)
class Collapsed:
    """Why a swipe was not counted (R6): the kept swipe it repeats, that
    swipe's resolved direction and the gap between them."""

    punch: Punch
    repeats: datetime
    repeats_direction: Direction
    gap_seconds: int


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime | None
    direction: Direction = "entry"


@dataclass(frozen=True)
class Gap:
    """Time outside between two paired stretches (KTD9). A clean Out->In gap
    is a break; a gap holding an unpaired swipe is unclear and never counted
    as break. ``number`` counts within its kind, from 1."""

    kind: GapKind
    number: int
    start: datetime
    end: datetime
    seconds: int
    unpaired: tuple[tuple[datetime, Direction], ...] = ()


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
    # Exact durations (R17); the *_minutes fields are floored views of these.
    effective_seconds: int = 0
    gross_seconds: int | None = 0
    break_seconds: int = 0
    # KTD9: gaps between paired stretches in time order (breaks and unclear).
    gaps: tuple[Gap, ...] = ()
    unclear_seconds: int = 0
    # Swipes that paired with nothing (superseded entries, orphan exits, and a
    # past day's trailing entry), with their resolved direction.
    unpaired: tuple[tuple[datetime, Direction], ...] = ()
    # Duplicate swipes dropped before pairing ("not counted" in raw lists).
    collapsed: tuple[Punch, ...] = ()
    # R6: one reason per collapsed swipe, in the same order as ``collapsed``.
    collapsed_reasons: tuple[Collapsed, ...] = ()
    # Direction-less swipes whose direction was inferred to fill a missing
    # alternation (Out, swipe, Out -> the swipe is In).
    inferred: tuple[datetime, ...] = ()


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

    resolved, collapsed, inferred, direction_source = _resolve_directions(ordered)
    segments, unmatched, open_start = _pair_segments(resolved)
    anomalies = tuple(_anomaly_text(ts, direction) for ts, direction in unmatched)
    extras = {
        "collapsed": tuple(c.punch for c in collapsed),
        "collapsed_reasons": tuple(collapsed),
        "inferred": tuple(inferred),
    }

    # R4: inside now on today's work day is open, whatever orphans came before.
    if open_start is not None and is_today and now is not None:
        segments.append(Segment(start=open_start, end=None))
        effective = sum(_seconds(s.end - s.start) for s in segments if s.end is not None)
        effective += _seconds(now - open_start)
        gross = _seconds(now - segments[0].start)
        extras["unpaired"] = tuple(unmatched)
        return _scored("open", gross, effective, segments, direction_source, anomalies, extras)

    closed = list(segments)
    if open_start is not None:
        # A trailing entry with no exit on a past day: flag it, keep it visible.
        anomalies += (_anomaly_text(open_start, "entry"),)
        unmatched.append((open_start, "entry"))
        segments.append(Segment(start=open_start, end=None))
    extras["unpaired"] = tuple(unmatched)

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
            gross_seconds=None,
            **extras,
        )

    effective = sum(_seconds(s.end - s.start) for s in closed)
    gross = _seconds(closed[-1].end - closed[0].start)
    # R17: compare exact seconds; the target is whole minutes, so the floored
    # minute display never disagrees with the status.
    status: DayStatus = "met" if effective >= target_minutes * 60 else "short"
    return _scored(status, gross, effective, segments, direction_source, anomalies, extras)


def _scored(
    status: DayStatus,
    gross: int,
    effective: int,
    segments: list[Segment],
    direction_source: DirectionSource,
    anomalies: tuple[str, ...],
    extras: dict,
) -> DayResult:
    """Build a scored result from exact seconds. Gaps come from the paired
    stretches (a past day's trailing open entry has none before it), so
    gross = effective + breaks + unclear. With no unclear gap, break minutes
    are gross minus effective minutes so the minute view adds up (flooring
    break alone drifts)."""
    gaps = _gaps([s for s in segments if s.end is not None or status == "open"], extras["unpaired"])
    break_seconds = sum(g.seconds for g in gaps if g.kind == "break")
    unclear_seconds = sum(g.seconds for g in gaps if g.kind == "unclear")
    gross_minutes = gross // 60
    effective_minutes = effective // 60
    break_minutes = gross_minutes - effective_minutes if not unclear_seconds else break_seconds // 60
    return DayResult(
        status=status,
        gross_minutes=gross_minutes,
        effective_minutes=effective_minutes,
        break_minutes=break_minutes,
        segments=tuple(segments),
        direction_source=direction_source,
        anomalies=anomalies,
        effective_seconds=effective,
        gross_seconds=gross,
        break_seconds=break_seconds,
        gaps=gaps,
        unclear_seconds=unclear_seconds,
        **extras,
    )


def _gaps(
    segments: list[Segment], unpaired: tuple[tuple[datetime, Direction], ...]
) -> tuple[Gap, ...]:
    gaps: list[Gap] = []
    counts = {"break": 0, "unclear": 0}
    for before, after in zip(segments, segments[1:]):
        start, end = before.end, after.start
        inside = tuple(u for u in unpaired if start <= u[0] <= end)
        kind: GapKind = "unclear" if inside else "break"
        counts[kind] += 1
        gaps.append(Gap(kind, counts[kind], start, end, _seconds(end - start), inside))
    return tuple(gaps)


def work_day(timestamp: datetime, day_start_hour: int) -> date:
    """The work day containing ``timestamp`` (naive IST): before the day-start
    hour a time still belongs to the previous calendar day's work day."""
    return (timestamp - timedelta(hours=day_start_hour)).date()


def work_day_window(day: date, day_start_hour: int) -> tuple[datetime, datetime]:
    """The half-open [start, end) window of work day ``day``."""
    start = datetime.combine(day, time(hour=day_start_hour))
    return start, start + timedelta(days=1)


def punch_from_row(row, windows: dict[str, int]) -> Punch:
    """A Punch from a punch row carrying ``device_id``, with that device's
    quick-swipe window (KTD3); an unknown device keeps the default."""
    window = windows.get(row.get("device_id"), DUPLICATE_WINDOW_SECONDS)
    return Punch(row["event_timestamp"], row["trusted_direction"], window)


def _resolve_directions(
    ordered: list[Punch],
) -> tuple[list[tuple[datetime, Direction]], list[Collapsed], list[datetime], DirectionSource]:
    """Give every swipe a direction or mark it a duplicate (KTD9).

    A known-direction swipe always counts, unless it repeats the previous kept
    swipe's direction within its own device's window (a re-swipe, KTD3). A
    direction-less swipe first fills a missing alternation: between a kept
    swipe and the next known-direction swipe of the same direction it takes
    the opposite one. Otherwise it is a duplicate within the window of the
    previous kept swipe, or alternates from it."""
    resolved: list[tuple[datetime, Direction]] = []
    collapsed: list[Collapsed] = []
    inferred: list[datetime] = []
    any_trusted = False
    any_alternated = False
    for i, punch in enumerate(ordered):
        last = resolved[-1] if resolved else None
        # KTD3: strictly less than this punch's window, so 0 counts every swipe.
        near = last is not None and punch.timestamp - last[0] < timedelta(seconds=punch.window_seconds)
        if punch.trusted_direction is not None:
            if near and last[1] == punch.trusted_direction:
                collapsed.append(_collapsed(punch, last))
                continue
            direction = punch.trusted_direction
            any_trusted = True
        else:
            following = next((p.trusted_direction for p in ordered[i + 1:]
                              if p.trusted_direction is not None), None)
            if last is not None and following == last[1]:
                direction = _flip(last[1])
                inferred.append(punch.timestamp)
            elif near:
                collapsed.append(_collapsed(punch, last))
                continue
            else:
                direction = _flip(last[1]) if last is not None else "entry"
            any_alternated = True
        resolved.append((punch.timestamp, direction))

    if any_trusted and any_alternated:
        source: DirectionSource = "mixed"
    elif any_trusted:
        source = "raw_byte"
    elif any_alternated:
        source = "alternation"
    else:
        source = "none"
    return resolved, collapsed, inferred, source


def _collapsed(punch: Punch, kept: tuple[datetime, Direction]) -> Collapsed:
    return Collapsed(punch, kept[0], kept[1], _seconds(punch.timestamp - kept[0]))


def _flip(direction: Direction) -> Direction:
    return "exit" if direction == "entry" else "entry"


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


def _seconds(delta) -> int:
    return int(delta.total_seconds())
