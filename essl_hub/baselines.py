"""U3: an employee's normal day, learned from their own recent history.

Pure functions over DayResults (KTD1): medians, percentiles and MAD give the
same answer every run and are explainable to HR. The alerts service loads the
day results and stores ``Baseline.metrics()`` as JSON; nothing here reads a
clock, a file or the database.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .hours import DEFAULT_DAY_START_HOUR, DayResult, work_day_window

# R1 / KTD2: 8 weeks rolling, all workdays pooled, ending yesterday.
LOOKBACK_WEEKS = 8
LOOKBACK_DAYS = LOOKBACK_WEEKS * 7
# R2: morning alerts need this many punched days, live alerts this many clean.
MIN_PUNCHED_DAYS = 10
MIN_CLEAN_DAYS = 10

# KTD2 limit: clamp(max(FLOOR, P90_FACTOR x p90, median + MAD_SIGMAS x
# MAD_SCALE x MAD), FLOOR, CAP), in minutes. MAD_SCALE makes MAD estimate a
# normal standard deviation; the floor covers MAD = 0.
LIMIT_FLOOR_MINUTES = 90
LIMIT_CAP_MINUTES = 240
P90_FACTOR = 1.5
MAD_SIGMAS = 4
MAD_SCALE = 1.4826

P10, P50, P90 = 0.10, 0.50, 0.90
# Stored stats are rounded once at compute time, so a baseline read back from
# its JSON equals the one that was written.
ROUND_DIGITS = 1

# Fix-request statuses whose day is kept out of training (R3): open, or with
# at least one approved line (partial). Closed and rejected days train as-is.
EXCLUDING_REQUEST_STATUSES = frozenset({"pending", "approved", "partial"})


@dataclass(frozen=True)
class Spread:
    """p10, median and p90 of one daily time, in minutes since the work-day
    start (so an evening shift's 01:30 last-out sorts after 23:00)."""

    p10: float
    median: float
    p90: float

    def as_dict(self) -> dict[str, float]:
        return {"p10": self.p10, "median": self.median, "p90": self.p90}


@dataclass(frozen=True)
class BreakStats:
    """The longest Out->In break per clean day, in minutes, and the KTD2
    live limit derived from it."""

    median: float
    mad: float
    p90: float
    limit: float

    def as_dict(self) -> dict[str, float]:
        return {"median": self.median, "mad": self.mad, "p90": self.p90, "limit": self.limit}


@dataclass(frozen=True)
class Baseline:
    punched_days: int
    clean_days: int
    # All three are None until MIN_CLEAN_DAYS clean days exist.
    breaks: BreakStats | None = None
    last_out: Spread | None = None
    first_in: Spread | None = None

    @property
    def morning_eligible(self) -> bool:
        return self.punched_days >= MIN_PUNCHED_DAYS

    @property
    def live_eligible(self) -> bool:
        return self.breaks is not None and self.last_out is not None

    @property
    def limit_minutes(self) -> float | None:
        return self.breaks.limit if self.breaks is not None else None

    def metrics(self) -> dict | None:
        """The JSON-ready stats for attendance_baselines.metrics; None when
        there are too few clean days for any."""
        if self.breaks is None or self.last_out is None or self.first_in is None:
            return None
        return {
            "break": self.breaks.as_dict(),
            "last_out": self.last_out.as_dict(),
            "first_in": self.first_in.as_dict(),
        }

    @classmethod
    def from_stored(
        cls, punched_days: int, clean_days: int, metrics: str | Mapping | None
    ) -> Baseline:
        """Rebuild from an attendance_baselines row (metrics as stored JSON
        text or already parsed)."""
        data = json.loads(metrics) if isinstance(metrics, str) else metrics
        if not data:
            return cls(int(punched_days), int(clean_days))
        return cls(
            int(punched_days),
            int(clean_days),
            BreakStats(**data["break"]),
            Spread(**data["last_out"]),
            Spread(**data["first_in"]),
        )


def lookback_days(today: date) -> tuple[date, date]:
    """The inclusive work-day range a baseline built on ``today`` uses:
    LOOKBACK_DAYS ending yesterday."""
    return today - timedelta(days=LOOKBACK_DAYS), today - timedelta(days=1)


def is_punched(result: DayResult) -> bool:
    """At least one swipe that day; compute_day gives "empty" only to none."""
    return result.status != "empty"


def is_clean(result: DayResult) -> bool:
    """R3: a fully paired day with nothing to review. Every segment closed,
    no anomaly or unpaired swipe, and no unclear gap (so every gap is a
    clean Out->In break). Request days are excluded by the caller's set."""
    return (
        result.status in ("met", "short")
        and bool(result.segments)
        and all(s.end is not None for s in result.segments)
        and not result.anomalies
        and not result.unpaired
        and not any(g.kind == "unclear" for g in result.gaps)
    )


def compute_baseline(
    days: Mapping[date, DayResult],
    *,
    today: date,
    request_days: Collection[date] = (),
    day_start_hour: int = DEFAULT_DAY_START_HOUR,
) -> Baseline:
    """One employee's baseline from their DayResults by work day.

    Days outside ``lookback_days(today)`` are ignored, so today's half-built
    day never trains. ``request_days`` are the work days holding a fix
    request in EXCLUDING_REQUEST_STATUSES (see ``excluded_request_days``);
    they still count as punched but never as clean."""
    first, last = lookback_days(today)
    excluded = set(request_days)
    breaks: list[float] = []
    last_outs: list[float] = []
    first_ins: list[float] = []
    punched = 0
    for day in sorted(days):
        result = days[day]
        if not first <= day <= last or not is_punched(result):
            continue
        punched += 1
        if day in excluded or not is_clean(result):
            continue
        start, _ = work_day_window(day, day_start_hour)
        # A day with no break still trains, as a 0 min longest break: leaving
        # it out would raise the median for people who rarely step out.
        breaks.append(max((g.seconds / 60 for g in result.gaps), default=0.0))
        first_ins.append(_minutes_since(start, result.segments[0].start))
        last_outs.append(_minutes_since(start, result.segments[-1].end))

    clean = len(breaks)
    if clean < MIN_CLEAN_DAYS:
        return Baseline(punched, clean)
    return Baseline(punched, clean, break_stats(breaks), _spread(last_outs), _spread(first_ins))


def excluded_request_days(requests: Collection[tuple[date, str]]) -> frozenset[date]:
    """Work days to drop from training, from (work_day, status) pairs."""
    return frozenset(day for day, status in requests if status in EXCLUDING_REQUEST_STATUSES)


def break_stats(longest_breaks: list[float]) -> BreakStats:
    """KTD2 stats and limit over the per-day longest breaks (minutes)."""
    median = statistics.median(longest_breaks)
    mad = statistics.median(abs(x - median) for x in longest_breaks)
    p90 = percentile(longest_breaks, P90)
    raw = max(LIMIT_FLOOR_MINUTES, P90_FACTOR * p90, median + MAD_SIGMAS * MAD_SCALE * mad)
    limit = min(max(raw, LIMIT_FLOOR_MINUTES), LIMIT_CAP_MINUTES)
    return BreakStats(_round(median), _round(mad), _round(p90), _round(limit))


def percentile(values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default, and
    statistics.quantiles' "inclusive"): rank q x (n - 1) over the sorted
    values, so p0 is the min, p100 the max and the order of input is moot."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of no values")
    rank = q * (len(ordered) - 1)
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _spread(values: list[float]) -> Spread:
    return Spread(_round(percentile(values, P10)), _round(percentile(values, P50)),
                  _round(percentile(values, P90)))


def _minutes_since(start: datetime, moment: datetime) -> float:
    return (moment - start).total_seconds() / 60


def _round(value: float) -> float:
    return round(float(value), ROUND_DIGITS)
