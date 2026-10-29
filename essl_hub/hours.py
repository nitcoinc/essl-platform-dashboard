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
DEFAULT_TARGET_MINUTES = 480

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


def read_day_start_hour(database) -> int:
    """The stored work-day start hour (R1); a missing or corrupt value falls back."""
    try:
        hour = int(database.setting("day_start_hour", str(DEFAULT_DAY_START_HOUR)))
    except (TypeError, ValueError):
        return DEFAULT_DAY_START_HOUR
    return hour if 0 <= hour <= 23 else DEFAULT_DAY_START_HOUR


def read_target_minutes(database) -> int:
    """The stored daily target; a missing or corrupt value (outside the
    1 minute to 24 hours the settings page accepts) is the default."""
    try:
        minutes = int(database.setting("daily_target_minutes", str(DEFAULT_TARGET_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_TARGET_MINUTES
    return minutes if 1 <= minutes <= 24 * 60 else DEFAULT_TARGET_MINUTES


# A work day can be reported while it is one of the last REPORT_DAYS work
# days, today included; an emailed report link expires REPORT_DAYS after its
# work day ends (KTD7), and the form refuses the day once it is too old.
REPORT_DAYS = 7


def reportable(day: date, today: date) -> bool:
    """True while ``day`` is one of the last REPORT_DAYS work days."""
    return 0 <= (today - day).days < REPORT_DAYS


# KTD4: what an approved fix did to a punch row (its ``correction`` key).
CORRECTION_ADDED = "added"
CORRECTION_DIRECTION = "direction_fixed"
CORRECTION_IGNORED = "ignored"


def punch_from_row(row, windows: dict[str, int]) -> Punch:
    """A Punch from a punch row carrying ``device_id``, with that device's
    quick-swipe window (KTD3); an unknown device keeps the default. A punch
    HR added or re-directed (KTD4) gets a zero window: it is never collapsed."""
    if row.get("correction") in (CORRECTION_ADDED, CORRECTION_DIRECTION):
        window = 0
    else:
        window = windows.get(row.get("device_id"), DUPLICATE_WINDOW_SECONDS)
    return Punch(row["event_timestamp"], row["trusted_direction"], window)


def scored_directions(punches: list[Punch]) -> tuple[dict[datetime, Direction], set[datetime]]:
    """Each counted swipe's direction as the day scores it (KTD9): the stored
    one, or the one inferred or alternated for a direction-less swipe; and
    the swipes whose direction was filled in between their neighbours. Quick
    re-swipes that were not counted are absent."""
    resolved, _, inferred, _ = _resolve_directions(sorted(punches, key=lambda p: p.timestamp))
    return dict(resolved), set(inferred)


def counted_punches(rows, windows: dict[str, int]) -> list[Punch]:
    """The Punches ``compute_day`` scores from merged rows: a punch HR ignored
    (KTD4) stays in the raw list but never counts."""
    return [punch_from_row(row, windows) for row in rows if row.get("correction") != CORRECTION_IGNORED]


# What the v10 migration names a correction whose maker was never recorded.
NO_AUDIT_NAME = "before audit trail"


def correction_info(correction: dict) -> dict:
    """Who made a correction and why, as the day view shows it. A row from
    before the audit trail has no name of its own."""
    return {
        "id": correction.get("id"),
        "by": correction.get("created_by_name") or NO_AUDIT_NAME,
        "role": correction.get("created_by_role"),
        "at": correction.get("created_at"),
        "reason": correction.get("reason"),
        "path": correction.get("path") or "request",
    }


def apply_corrections(rows, corrections, main_ids: dict[int, str]) -> list[dict]:
    """Merge approved fixes (KTD4) into punch rows, in time order. Pure, so
    the HR preview can merge proposed lines the same way.

    ``rows`` carry ``fingerprint`` and their owner's ``employee_id`` (None for
    an unmapped ID); ``corrections`` are ``punch_corrections`` rows, oldest
    first; ``main_ids`` maps each owner to their main device user ID. Inputs
    are never mutated; every returned row has a ``correction`` key, a
    ``correction_info`` (who fixed it and why, None when untouched) and the
    ``device_direction`` the fix may have overridden:

    - ``set_direction`` overrides the direction of its fingerprint's row;
    - ``ignore`` keeps its row, marked, for the raw list;
    - ``add`` becomes a row under the owner's main ID with no device, so no
      device window, site or raw byte.

    A fingerprint correction applies only to a row its own employee owns: a
    target that no longer exists, or whose ID moved to someone else, is inert.
    """
    targeted: dict[str, list[dict]] = {}
    for correction in corrections:
        if correction["kind"] != "add" and correction.get("fingerprint"):
            targeted.setdefault(correction["fingerprint"], []).append(correction)
    merged = []
    for row in rows:
        out = {**row, "correction": None, "correction_info": None}
        if "trusted_direction" in row:
            out["device_direction"] = row["trusted_direction"]  # before any fix overrides it
        for correction in targeted.get(row.get("fingerprint"), ()):
            if correction["employee_id"] != row.get("employee_id"):
                continue
            if correction["kind"] == "ignore":
                out["correction"] = CORRECTION_IGNORED
                out["correction_info"] = correction_info(correction)
            elif correction["kind"] == "set_direction":
                out["trusted_direction"] = correction["direction"]
                if out["correction"] != CORRECTION_IGNORED:
                    out["correction"] = CORRECTION_DIRECTION
                    # An ignore decides the final state, so a later direction
                    # fix does not take over its name.
                    out["correction_info"] = correction_info(correction)
        merged.append(out)
    for correction in corrections:
        owner = correction["employee_id"]
        if correction["kind"] != "add" or correction.get("event_timestamp") is None or owner not in main_ids:
            continue
        merged.append({
            "device_user_id": main_ids[owner],
            "employee_id": owner,
            "fingerprint": f"correction:{correction.get('id')}",
            "event_timestamp": correction["event_timestamp"],
            "trusted_direction": correction["direction"],
            "raw_direction_byte": None,
            "raw_state": None,
            "site": None,
            "device_id": None,
            "correction": CORRECTION_ADDED,
            "correction_info": correction_info(correction),
        })
    merged.sort(key=lambda r: r["event_timestamp"])
    return merged


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
