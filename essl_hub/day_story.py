"""What the Today page says about a day: headline, tone, ring and log.

Pure functions over a DayResult so the template stays declarative and the
wording is unit-testable without Flask.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .db import C3_UNDECODED_BYTE
from .hours import (
    CORRECTION_ADDED, CORRECTION_DIRECTION, CORRECTION_IGNORED, NO_AUDIT_NAME, Collapsed, DayResult,
)
from .punch_alerts import C3_REALTIME_SOURCE


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


_PUNCH_FLAGS = (
    ("interval too short", "interval too short"),
    ("door denied: interlock", "door denied (interlock)"),
    ("door denied: anti-passback", "door denied (anti-passback)"),
)


def punch_flags(raw_state: str | None) -> tuple[str, ...]:
    """Chips for a stored swipe in the raw-punch list: a C3 swipe the door
    denied for punch interval (KTD11), or for interlock / anti-passback while
    "Wait for door close" is off (KTD5), still counts but is marked."""
    return tuple(chip for note, chip in _PUNCH_FLAGS if note in (raw_state or ""))


# KTD4: chips for a swipe an approved fix touched, keyed by the row's
# ``correction`` (hours.apply_corrections).
CORRECTION_FLAGS = {
    CORRECTION_ADDED: "added",
    CORRECTION_DIRECTION: "direction fixed",
    CORRECTION_IGNORED: "ignored",
}
# Day-level chips from the day's latest fix request status.
_FIX_FLAGS = {"pending": "Fix pending", "rejected": "Fix rejected"}


_DIRECTION_WORDS = {"entry": "In", "exit": "Out"}
_CORRECTION_VERBS = {
    CORRECTION_ADDED: "Added",
    CORRECTION_DIRECTION: "Direction fixed",
    CORRECTION_IGNORED: "Ignored",
}


def is_unconfirmed_guess(row: dict) -> bool:
    """A C3 live-log row still carrying the undecoded byte 15: no direction
    (v11; older rows may still hold the old exit guess) until the device
    history confirms it (db.fill_punch_directions)."""
    return row.get("raw_direction_byte") == C3_UNDECODED_BYTE and row.get("source") == C3_REALTIME_SOURCE


def punch_basis(row: dict, direction: str, *, collapsed: bool = False, inferred: bool = False) -> str:
    """Who or what decided this swipe's direction, in words, from the row as
    the day was scored: a fix (naming who made it), a quick re-swipe, a swipe
    filled in between its neighbours, a live-log swipe with no direction yet,
    the direction the device stored, or the hub's alternation from the first
    swipe."""
    correction = row.get("correction")
    if correction in _CORRECTION_VERBS:
        verb = _CORRECTION_VERBS[correction]
        info = row.get("correction_info") or {}
        by = info.get("by")
        if not by or by == NO_AUDIT_NAME:
            return f"{verb} before the audit trail"
        text = f"{verb} by {by}" + (f" ({info['role']})" if info.get("role") else "")
        text += f": {info['reason']}" if info.get("reason") else ""
        device = _DIRECTION_WORDS.get(row.get("device_direction"))
        if correction == CORRECTION_DIRECTION and device and row.get("device_direction") != row.get("trusted_direction"):
            text += f" (the device said {device})"
        return text
    if collapsed:
        return "Quick re-swipe: not counted"
    word = _DIRECTION_WORDS.get(direction)
    stored = _DIRECTION_WORDS.get(row.get("trusted_direction"))
    if not stored and word and is_unconfirmed_guess(row):
        return f"The live log gave no direction; counted as {word} from the swipes around it until the device history confirms it"
    if inferred and word:
        return f"Filled in as {word} between the swipes around it"
    if stored:
        if is_unconfirmed_guess(row):
            return f"Guessed {stored} from the live log; the device history confirms it later"
        return f"Device said {stored}"
    return f"Counted as {word} by alternating from the first swipe" if word else ""


def punch_facts(row: dict) -> str:
    """The raw device facts behind a swipe, for HR and admins."""
    parts = [row.get("device_id") or ""]
    if row.get("door"):
        parts.append(f"door {row['door']}")
    if row.get("reader"):
        parts.append(f"reader {row['reader']}")
    if row.get("raw_direction_byte") is not None:
        byte = f"in/out byte {row['raw_direction_byte']}"
        sent = row.get("in_out_raw")
        if sent is not None and sent != row["raw_direction_byte"]:
            byte += f" (panel sent {sent})"  # R3: the live-log value before decoding
        parts.append(byte)
    if row.get("source") == C3_REALTIME_SOURCE:
        parts.append("live log")
    return " · ".join(part for part in parts if part)


def fix_flags(statuses) -> tuple[str, ...]:
    """The day's fix-request chip from its request statuses (oldest first, as
    ``Database.fix_request_days`` gives them): the latest request decides."""
    latest = next(reversed(list(statuses)), None)
    return (_FIX_FLAGS[latest],) if latest in _FIX_FLAGS else ()


@dataclass(frozen=True)
class BarSegment:
    kind: str  # "inside" | "break" | "unclear"
    number: int | None
    start: datetime
    end: datetime
    seconds: int
    percent: float
    open: bool = False  # the live stretch, ending at now
    text_wide: str = ""  # in-segment text, e.g. "1 · 33m"; "" when too narrow
    text_narrow: str = ""  # same at phone width


@dataclass(frozen=True)
class BarMark:
    """A boundary on the day bar: a tick, and a time label when it fits."""

    position: float  # percent from the left
    time: datetime | None  # None for the live "now" edge
    label: str  # "" = tick only
    narrow: bool  # label also shown at phone width
    align: str  # "start" | "center" | "end"


@dataclass(frozen=True)
class RawPunch:
    time: datetime
    direction: str  # "entry" | "exit" | ""
    flags: tuple[str, ...]  # "not counted", "unpaired", "inferred", "interval too short", "ignored", ...
    note: str = ""  # R6: why a "not counted" swipe was dropped
    basis: str = ""  # who or what decided this swipe's direction (punch_basis)
    facts: str = ""  # the raw device facts, only for roles that may see them


@dataclass(frozen=True)
class BreakView:
    """KTD10: summary, labelled day bar and every stored swipe."""

    inside_seconds: int
    break_seconds: int
    break_count: int
    unclear_seconds: int
    bar: tuple[BarSegment, ...]
    bar_start: datetime | None
    bar_end: datetime | None
    bar_label: str
    marks: tuple[BarMark, ...]
    gaps: tuple  # result.gaps, time-ordered (breaks and unclear)
    punches: tuple[RawPunch, ...]
    fix_flags: tuple[str, ...] = ()  # "Fix pending" | "Fix rejected"


def _hm(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}"


def _dur(seconds: int) -> str:
    """Compact duration for the bar: "19s", "33m", "1h 05m"."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


# Label width as a share of the bar (a "16:14" label is ~34px): the bar is up
# to 640px wide on desktop and ~340px on a phone.
_LABEL_WIDE, _LABEL_NARROW = 6.2, 11.5


def _segment_text(kind: str, number: int | None, seconds: int, percent: float) -> tuple[str, str]:
    full = f"{number} · {_dur(seconds)}" if kind == "break" else f"Unclear · {_dur(seconds)}"
    options = (full, str(number)) if kind == "break" else (full, "Unclear", "?")

    def fit(px_per_percent: float) -> str:
        width = percent * px_per_percent
        return next((t for t in options if width >= 7 * len(t) + 7), "")

    return fit(6.4), fit(3.4)


def _bar_marks(bar: list[BarSegment], live: bool) -> tuple[BarMark, ...]:
    """Ticks at every segment boundary; time labels where they fit. The two
    edges always label; break and unclear edges follow, longest gap first, and
    a label that would collide is dropped (the punch list holds every time)."""
    if not bar:
        return ()
    edges: list[tuple[float, datetime | None, int]] = []  # position, time, priority
    pos = 0.0
    edges.append((0.0, bar[0].start, 0))
    for seg in bar[:-1]:
        pos += seg.percent
        edges.append((pos, seg.end, 0))
    edges.append((100.0, None if live else bar[-1].end, 0))
    # Priority: edges of the bar first, then by the length of the gap touched.
    ranked = []
    for i, (p, t, _) in enumerate(edges):
        if i in (0, len(edges) - 1):
            rank = float("inf")
        else:
            touching = [s for s in (bar[i - 1], bar[i]) if s.kind != "inside"]
            rank = max((s.seconds for s in touching), default=0)
        ranked.append((rank, i))
    ranked.sort(key=lambda r: (-r[0], r[1]))

    def place(width: float) -> set[int]:
        taken: list[tuple[float, float]] = []
        chosen: set[int] = set()
        for _, i in ranked:
            p = edges[i][0]
            if i == 0:
                span = (0.0, width)
            elif i == len(edges) - 1:
                span = (100.0 - width, 100.0)
            else:
                span = (p - width / 2, p + width / 2)
            if all(span[1] <= a or span[0] >= b for a, b in taken):
                taken.append(span)
                chosen.add(i)
        return chosen

    wide, narrow = place(_LABEL_WIDE), place(_LABEL_NARROW)
    marks = []
    for i, (p, t, _) in enumerate(edges):
        align = "start" if i == 0 else "end" if i == len(edges) - 1 else "center"
        text = "now" if t is None else t.strftime("%H:%M")
        marks.append(BarMark(round(p, 3), t, text if i in wide else "", i in narrow, align))
    return tuple(marks)


def break_view(result: DayResult, now: datetime | None, raw_rows=(), fix_statuses=(), *,
               show_facts: bool = False) -> BreakView:
    """The break view for one day. ``raw_rows`` are the stored swipes
    (``event_timestamp``, ``raw_state``) so the raw list shows every one,
    with approved fixes merged (KTD4): ignored swipes stay listed, marked.
    ``fix_statuses`` are the day's fix request statuses, oldest first.
    ``show_facts`` adds each swipe's raw device facts (HR and admins only)."""
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
            pct = round(secs * 100 / total, 3)
            wide, narrow = _segment_text(kind, number, secs, pct) if kind != "inside" else ("", "")
            bar.append(BarSegment(kind, number, a, b, secs, pct, is_open, wide, narrow))
    breaks = [g for g in result.gaps if g.kind == "break"]
    if breaks:
        label = f"Inside {_hm(result.effective_seconds)}, {len(breaks)} break{'s' if len(breaks) != 1 else ''} totalling {_hm(result.break_seconds)}"
        label += ": " + "; ".join(
            f"break {g.number} {g.start:%H:%M} to {g.end:%H:%M} ({_dur(g.seconds)})" for g in breaks)
    else:
        label = f"Inside {_hm(result.effective_seconds)}, no breaks"
    unclear = [g for g in result.gaps if g.kind == "unclear"]
    if unclear:
        label += f", {_hm(result.unclear_seconds)} unclear: " + "; ".join(
            f"{g.start:%H:%M} to {g.end:%H:%M} ({_dur(g.seconds)})" for g in unclear)
    raw_rows = list(raw_rows)
    # KTD4: the bar is the text record, so it names what HR changed.
    for kind in (CORRECTION_ADDED, CORRECTION_IGNORED):
        count = sum(1 for row in raw_rows if row.get("correction") == kind)
        if count:
            label += f", {count} punch{'es' if count != 1 else ''} {CORRECTION_FLAGS[kind]}"

    starts = {s.start for s in result.segments}
    ends = {s.end for s in result.segments if s.end is not None}
    unpaired = {t: d for t, d in result.unpaired}
    for gap in result.gaps:
        unpaired.update({t: d for t, d in gap.unpaired})
    collapsed = list(result.collapsed_reasons)
    inferred = set(result.inferred)
    punches: list[RawPunch] = []
    for row in raw_rows:
        t = row["event_timestamp"]
        flags: list[str] = []
        note = ""
        correction = row.get("correction")
        if correction == CORRECTION_IGNORED:
            # Never scored, so it pairs with nothing and repeats nothing.
            flags.append(CORRECTION_FLAGS[correction])
            flags.extend(punch_flags(row.get("raw_state")))
            punches.append(RawPunch(
                t, row.get("trusted_direction") or "", tuple(flags),
                basis=punch_basis(row, ""), facts=punch_facts(row) if show_facts else ""))
            continue
        reason = next((c for c in collapsed if c.punch.timestamp == t), None)
        if reason is not None:
            collapsed.remove(reason)
            direction = row.get("trusted_direction") or ""
            flags.append("not counted")
            note = collapsed_note(reason)
        elif t in starts:
            direction = "entry"
        elif t in ends:
            direction = "exit"
        elif t in unpaired:
            direction = unpaired[t]
            flags.append("unpaired")
        else:
            direction = row.get("trusted_direction") or ""
        if t in inferred and "not counted" not in flags:
            flags.append("inferred")
        if correction in CORRECTION_FLAGS:
            flags.append(CORRECTION_FLAGS[correction])
        flags.extend(punch_flags(row.get("raw_state")))
        punches.append(RawPunch(
            t, direction, tuple(flags), note,
            basis=punch_basis(row, direction, collapsed="not counted" in flags, inferred="inferred" in flags),
            facts=punch_facts(row) if show_facts else ""))
    return BreakView(
        inside_seconds=result.effective_seconds,
        break_seconds=result.break_seconds,
        break_count=len(breaks),
        unclear_seconds=result.unclear_seconds,
        bar=tuple(bar),
        bar_start=bar_start,
        bar_end=bar_end,
        bar_label=label,
        marks=_bar_marks(bar, open_day),
        gaps=result.gaps,
        punches=tuple(punches),
        fix_flags=fix_flags(fix_statuses),
    )


def collapsed_note(reason: Collapsed) -> str:
    """R6: "same as In 10:00:03, 12s earlier"; the kept swipe's direction is
    the one its own row shows."""
    label = {"entry": "In", "exit": "Out"}.get(reason.repeats_direction, "swipe")
    return f"same as {label} {reason.repeats:%H:%M:%S}, {reason.gap_seconds}s earlier"


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
