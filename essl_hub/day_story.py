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
