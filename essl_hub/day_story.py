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
    kind: str  # "in" | "out" | "break" | "now" | "missing"
    time: datetime | None
    minutes: int | None = None  # stretch length on "in", gap on "break"


@dataclass(frozen=True)
class Headline:
    text: str
    tone: str  # "ok" | "info" | "warn" | "bad" | "neutral"


def _fmt(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _minutes(a: datetime, b: datetime) -> int:
    return max(0, int((b - a).total_seconds() // 60))


def target_reached_at(result: DayResult, target_minutes: int, now: datetime | None) -> datetime | None:
    """When effective time crossed (or, while inside, will cross) the target."""
    done = 0
    for seg in result.segments:
        end = seg.end or now
        if end is None:
            return None
        length = _minutes(seg.start, end)
        if done + length >= target_minutes:
            return seg.start + timedelta(minutes=target_minutes - done)
        done += length
    if result.status == "open" and now is not None:
        return now + timedelta(minutes=target_minutes - done)
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
    """In/out nodes with the break between stretches, ending in "now" while inside."""
    items: list[LogItem] = []
    previous_end: datetime | None = None
    for seg in result.segments:
        if previous_end is not None:
            items.append(LogItem("break", None, _minutes(previous_end, seg.start)))
        end = seg.end
        if end is None and result.status == "open" and now is not None:
            items.append(LogItem("in", seg.start, _minutes(seg.start, now)))
            items.append(LogItem("now", now))
        elif end is None:
            items.append(LogItem("in", seg.start))
            items.append(LogItem("missing", None))
        else:
            items.append(LogItem("in", seg.start, _minutes(seg.start, end)))
            items.append(LogItem("out", end))
        previous_end = end
    return items


def ring_percent(result: DayResult, target_minutes: int) -> int:
    if target_minutes <= 0:
        return 100
    return min(100, round(result.effective_minutes * 100 / target_minutes))
