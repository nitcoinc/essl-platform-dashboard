"""What the Today page says about a day: headline, tone, ring and log.

Pure functions over a DayResult so the template stays declarative and the
wording is unit-testable without Flask.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .hours import DayResult


@dataclass(frozen=True)
class LogItem:
    kind: str  # "in" | "out" | "break" | "unclear" | "now" | "missing"
    time: datetime | None
    minutes: int | None = None  # stretch length on "in", gap on "break"
    seconds: int | None = None  # exact length; "in" stretches sum to effective_seconds


@dataclass(frozen=True)
class Headline:
    text: str
    tone: str  # "ok" | "info" | "warn" | "bad" | "neutral"


def _fmt(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _seconds(a: datetime, b: datetime) -> int:
    return max(0, int((b - a).total_seconds()))


def _stretch(kind: str, time: datetime | None, a: datetime, b: datetime) -> LogItem:
    seconds = _seconds(a, b)
    return LogItem(kind, time, seconds // 60, seconds)


def target_reached_at(result: DayResult, target_minutes: int, now: datetime | None) -> datetime | None:
    """When effective time crossed (or, while inside, will cross) the target."""
    target = target_minutes * 60
    done = 0
    for seg in result.segments:
        end = seg.end or now
        if end is None:
            return None
        length = _seconds(seg.start, end)
        if done + length >= target:
            return seg.start + timedelta(seconds=target - done)
        done += length
    if result.status == "open" and now is not None:
        return now + timedelta(seconds=target - done)
    return None


def headline(result: DayResult, target_minutes: int, now: datetime | None, *, is_today: bool, weekend: bool) -> Headline:
    remaining = target_minutes - result.effective_minutes
    reached = target_reached_at(result, target_minutes, now)
    if result.status == "open":
        if remaining <= 0:
            return Headline(f"Inside · target met at {reached:%H:%M}", "ok")
        return Headline(f"Inside · {_fmt(remaining)} to go", "info")
    # R3: a flagged day still reads as its score, with a review note after it.
    review = " · needs review" if result.anomalies else ""
    if result.status == "met":
        return Headline((f"Target met at {reached:%H:%M}" if reached else "Target met") + review, "ok")
    if result.status == "short":
        return Headline(f"Short by {_fmt(remaining)}{review}", "warn")
    if result.status == "incomplete":
        return Headline("Missing exit punch", "bad")
    if weekend:
        return Headline("Weekend", "neutral")
    return Headline("Not in yet" if is_today else "No punches", "neutral")


def log_items(result: DayResult, now: datetime | None) -> list[LogItem]:
    """In/out nodes with the gap between stretches, ending in "now" while inside.
    Gap kind (break or unclear) comes from ``result.gaps`` (KTD9); a past day's
    trailing entry gets no gap before "missing"."""
    items: list[LogItem] = []
    previous_end: datetime | None = None
    unclear_ends = {g.end for g in result.gaps if g.kind == "unclear"}
    for seg in result.segments:
        trailing_missing = seg.end is None and result.status != "open"
        if previous_end is not None and not trailing_missing:
            kind = "unclear" if seg.start in unclear_ends else "break"
            items.append(_stretch(kind, None, previous_end, seg.start))
        end = seg.end
        if end is None and result.status == "open" and now is not None:
            items.append(_stretch("in", seg.start, seg.start, now))
            items.append(LogItem("now", now))
        elif end is None:
            items.append(LogItem("in", seg.start))
            items.append(LogItem("missing", None))
        else:
            items.append(_stretch("in", seg.start, seg.start, end))
            items.append(LogItem("out", end))
        previous_end = end
    return items


def punch_flags(raw_state: str | None) -> tuple[str, ...]:
    """Chips for a stored swipe in the raw-punch list: a C3 swipe the door
    denied for punch interval still counts (KTD11) but is marked."""
    return ("interval too short",) if "interval too short" in (raw_state or "") else ()


@dataclass(frozen=True)
class BarSegment:
    kind: str  # "inside" | "break" | "unclear"
    number: int | None
    start: datetime
    end: datetime
    seconds: int
    percent: float
    open: bool = False  # the live stretch, ending at now


@dataclass(frozen=True)
class RawPunch:
    time: datetime
    direction: str  # "entry" | "exit" | ""
    flags: tuple[str, ...]  # "not counted", "unpaired", "interval too short"


@dataclass(frozen=True)
class BreakView:
    """KTD10: summary, day bar, numbered break list and every stored swipe."""

    inside_seconds: int
    break_seconds: int
    break_count: int
    unclear_seconds: int
    bar: tuple[BarSegment, ...]
    bar_start: datetime | None
    bar_end: datetime | None
    bar_label: str
    gaps: tuple  # result.gaps, time-ordered (breaks and unclear)
    punches: tuple[RawPunch, ...]


def _hm(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}"


def break_view(result: DayResult, now: datetime | None, raw_rows=()) -> BreakView:
    """The break view for one day. ``raw_rows`` are the stored swipes
    (``event_timestamp``, ``raw_state``) so the raw list shows every one."""
    open_day = result.status == "open" and now is not None
    pieces: list[tuple[str, int | None, datetime, datetime, bool]] = []
    for seg in result.segments:
        if seg.end is not None:
            pieces.append(("inside", None, seg.start, seg.end, False))
        elif open_day and seg is result.segments[-1]:
            pieces.append(("inside", None, seg.start, max(now, seg.start), True))
    for gap in result.gaps:
        pieces.append((gap.kind, gap.number, gap.start, gap.end, False))
    pieces.sort(key=lambda p: p[2])
    bar: list[BarSegment] = []
    bar_start = pieces[0][2] if pieces else None
    bar_end = max(p[3] for p in pieces) if pieces else None
    if pieces:
        total = _seconds(bar_start, bar_end) or 1
        for kind, number, a, b, is_open in pieces:
            secs = _seconds(a, b)
            bar.append(BarSegment(kind, number, a, b, secs, round(secs * 100 / total, 3), is_open))
    breaks = [g for g in result.gaps if g.kind == "break"]
    if breaks:
        label = f"Inside {_hm(result.effective_seconds)}, {len(breaks)} break{'s' if len(breaks) != 1 else ''} totalling {_hm(result.break_seconds)}"
    else:
        label = f"Inside {_hm(result.effective_seconds)}, no breaks"
    if result.unclear_seconds:
        label += f", {_hm(result.unclear_seconds)} unclear"

    starts = {s.start for s in result.segments}
    ends = {s.end for s in result.segments if s.end is not None}
    unpaired = {t: d for t, d in result.unpaired}
    for gap in result.gaps:
        unpaired.update({t: d for t, d in gap.unpaired})
    collapsed = [p.timestamp for p in result.collapsed]
    punches: list[RawPunch] = []
    for row in raw_rows:
        t = row["event_timestamp"]
        flags: list[str] = []
        if t in collapsed:
            collapsed.remove(t)
            direction = ""
            flags.append("not counted")
        elif t in starts:
            direction = "entry"
        elif t in ends:
            direction = "exit"
        elif t in unpaired:
            direction = unpaired[t]
            flags.append("unpaired")
        else:
            direction = row.get("trusted_direction") or ""
        flags.extend(punch_flags(row.get("raw_state")))
        punches.append(RawPunch(t, direction, tuple(flags)))
    return BreakView(
        inside_seconds=result.effective_seconds,
        break_seconds=result.break_seconds,
        break_count=len(breaks),
        unclear_seconds=result.unclear_seconds,
        bar=tuple(bar),
        bar_start=bar_start,
        bar_end=bar_end,
        bar_label=label,
        gaps=result.gaps,
        punches=tuple(punches),
    )


def ring_percent(result: DayResult, target_minutes: int) -> int:
    if target_minutes <= 0:
        return 100
    return min(100, round(result.effective_minutes * 100 / target_minutes))


def _short(minutes: int) -> str:
    return f"{minutes}m" if minutes < 60 else _fmt(minutes)


def left_text(effective_seconds: int, target_seconds: int) -> str:
    """The Today remainder (R18): "17m left" rounds up, "+12m over" rounds down.
    static/timer.js re-derives the same text on every tick."""
    if effective_seconds >= target_seconds:
        return f"+{_short((effective_seconds - target_seconds) // 60)} over"
    return f"{_short(-(-(target_seconds - effective_seconds) // 60))} left"


def live_state(result: DayResult, target_minutes: int) -> str:
    """The data-state timer.js compares across refreshes: inside | met | out."""
    if result.status == "open":
        return "inside"
    if result.status in ("met", "short") and result.effective_seconds >= target_minutes * 60:
        return "met"
    return "out"


def today_label(result: DayResult, target_minutes: int) -> str:
    """The Today card's state label; never colour alone (U11 state table)."""
    met = result.effective_seconds >= target_minutes * 60
    if result.status == "open":
        return "Inside · Target met" if met else "Inside"
    if result.status == "incomplete":
        return "Incomplete: no exit yet"
    if met:
        return "Out · Target met"
    last_out = next((s.end for s in reversed(result.segments) if s.end is not None), None)
    return f"Out since {last_out:%H:%M:%S}" if last_out else "Out"


def review_label(result: DayResult) -> str | None:
    if not result.anomalies:
        return None
    text = "; ".join(result.anomalies)
    return "Needs review: " + text[0].lower() + text[1:]
