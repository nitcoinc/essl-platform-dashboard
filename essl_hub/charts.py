"""Server-rendered SVG charts (R19, KTD15).

No chart library: geometry is computed here and drawn as inline SVG that reads
the theme's chart tokens (``--chart-*`` in static/styles.css). Every chart has a
``<title>``, ``role="img"`` and an adjacent visually-hidden data table so the
numbers reach assistive technology (R21).

Horizontal positions use percentages so a chart fills its container at any
width without stretching text; vertical positions are fixed pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import count

from markupsafe import Markup, escape

from .hours import DayResult

_ids = count(1)


def fmt_minutes(minutes: int) -> str:
    """Hours always read as "7h 42m" (DESIGN.md)."""
    minutes = max(0, int(minutes))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


# --- Week bars ------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    day: date
    x: float  # percent, bar centre
    y: float  # px, top of bar
    height: float  # px
    minutes: int | None
    kind: str  # worked | live | incomplete | empty | future
    label: str
    met: bool = False
    label_y: float = 0.0  # px, value label baseline; never on the target line


CHART_HEIGHT = 184
PLOT_TOP = 12
PLOT_BOTTOM = 152  # baseline; day labels sit below it
INCOMPLETE_HEIGHT = 28
GUTTER = 8.0  # percent on the right for the hour scale and the target label
TICK_STEP = 120  # minutes between gridlines
LABEL_GAP = 6  # px between a bar top and its value label baseline
LABEL_HEIGHT = 13  # px, cap height plus descent at 12px
TARGET_CLEAR = 3  # px kept free on both sides of the target line


def _short_hours(minutes: int) -> str:
    return f"{minutes // 60}h" if minutes % 60 == 0 else fmt_minutes(minutes)


def week_bars(days: list[dict], target_minutes: int) -> dict:
    """Geometry for Mon-Fri bars against the daily target.

    ``days`` items carry ``date`` and ``result`` (``None`` for a future day).
    The scale runs from 0 to at least 10 hours in 2-hour steps. An incomplete
    day never gets a guessed height: it is a short hatched marker labelled
    "Incomplete". A value label that would touch the target line moves above it."""
    worked = [
        d["result"].effective_minutes
        for d in days
        if d["result"] is not None and d["result"].status != "incomplete"
    ]
    peak = max([target_minutes + 60, 600, *worked])
    top_minutes = -(-peak // TICK_STEP) * TICK_STEP
    plot_h = PLOT_BOTTOM - PLOT_TOP

    def y_for(minutes: float) -> float:
        return PLOT_BOTTOM - minutes / top_minutes * plot_h

    target_y = y_for(target_minutes)

    def label_y_for(top: float) -> float:
        baseline = top - LABEL_GAP
        box_top, box_bottom = baseline - LABEL_HEIGHT + 3, baseline + 3
        if box_top < target_y + TARGET_CLEAR and box_bottom > target_y - TARGET_CLEAR:
            baseline = target_y - TARGET_CLEAR - 3 - LABEL_GAP + 3
        return baseline

    bars = []
    columns = len(days) or 1
    span = 100 - GUTTER
    for index, entry in enumerate(days):
        x = (index + 0.5) / columns * span
        result: DayResult | None = entry["result"]
        if result is None:
            bars.append(Bar(entry["date"], x, PLOT_BOTTOM, 0, None, "future", ""))
        elif result.status == "incomplete":
            top = PLOT_BOTTOM - INCOMPLETE_HEIGHT
            bars.append(
                Bar(entry["date"], x, top, INCOMPLETE_HEIGHT, None, "incomplete",
                    "Incomplete", label_y=label_y_for(top))
            )
        elif result.status == "empty":
            bars.append(Bar(entry["date"], x, PLOT_BOTTOM, 0, 0, "empty", fmt_minutes(0),
                            label_y=label_y_for(PLOT_BOTTOM)))
        else:
            minutes = result.effective_minutes
            y = y_for(minutes)
            bars.append(
                Bar(entry["date"], x, y, PLOT_BOTTOM - y, minutes,
                    "live" if result.status == "open" else "worked", fmt_minutes(minutes),
                    met=minutes >= target_minutes, label_y=label_y_for(y))
            )
    ticks = [
        {"minutes": m, "y": y_for(m), "label": _short_hours(m)}
        for m in range(0, top_minutes + 1, TICK_STEP)
    ]
    return {
        "bars": bars,
        "ticks": ticks,
        "target_y": target_y,
        "target_minutes": target_minutes,
        "span": span,
        "column": span / columns,
    }


def _bar_title(bar: Bar, target_minutes: int) -> str:
    day = bar.day.strftime("%a %d %b")
    if bar.kind == "future":
        return f"{day}: not yet"
    if bar.kind == "incomplete":
        return f"{day}: incomplete, counts zero"
    if bar.kind == "empty":
        return f"{day}: no punches"
    if bar.met:
        note = "target met"
    else:
        note = f"short by {fmt_minutes(target_minutes - (bar.minutes or 0))}"
    live = ", still inside" if bar.kind == "live" else ""
    return f"{day}: {bar.label}, {note}{live}"


def render_week_bars(days: list[dict], target_minutes: int, *, label: str, today: date | None = None) -> Markup:
    geometry = week_bars(days, target_minutes)
    uid = next(_ids)
    column = geometry["column"]
    span = _num(geometry["span"])
    bar_w = column * 0.44
    target_y = _num(geometry["target_y"])
    hatch = f"wk-hatch-{uid}"
    parts = [
        f'<svg class="chart chart-week" width="100%" height="{CHART_HEIGHT}" role="img" '
        f'aria-labelledby="wk-title-{uid}" focusable="false">',
        f'<title id="wk-title-{uid}">{escape(label)}</title>',
        f'<defs><pattern id="{hatch}" width="6" height="6" patternUnits="userSpaceOnUse" '
        f'patternTransform="rotate(45)"><rect class="wk-hatch-bg" width="6" height="6"/>'
        f'<line class="wk-hatch-line" x1="0" y1="0" x2="0" y2="6"/></pattern></defs>',
    ]
    for bar in geometry["bars"]:
        if today is not None and bar.day == today:
            parts.append(
                f'<rect class="wk-today" x="{_num(bar.x - column / 2 + column * 0.06)}%" y="0" '
                f'width="{_num(column * 0.88)}%" height="{CHART_HEIGHT}" rx="6"/>'
            )
    for tick in geometry["ticks"]:
        y = _num(tick["y"])
        cls = "wk-base" if tick["minutes"] == 0 else "wk-grid"
        parts.append(f'<line class="{cls}" x1="0" x2="{span}%" y1="{y}" y2="{y}"/>')
        if abs(tick["y"] - geometry["target_y"]) >= 10:
            parts.append(
                f'<text class="wk-tick" x="100%" y="{y}" dy="0.35em" text-anchor="end">'
                f'{tick["label"]}</text>'
            )
    for bar in geometry["bars"]:
        left = _num(bar.x - bar_w / 2)
        width = _num(bar_w)
        title = f"<title>{escape(_bar_title(bar, target_minutes))}</title>"
        parts.append(f'<g class="wk-col">{title}')
        # Hit area so the whole column shows the title on hover.
        parts.append(
            f'<rect class="wk-hit" x="{_num(bar.x - column / 2)}%" y="0" '
            f'width="{_num(column)}%" height="{CHART_HEIGHT}"/>'
        )
        if bar.kind in ("worked", "live") and bar.height > 0:
            tone = "is-met" if bar.met else "is-short"
            radius = min(4.0, bar.height / 2)
            parts.append(
                f'<rect class="wk-bar {tone}" x="{left}%" y="{_num(bar.y)}" '
                f'width="{width}%" height="{_num(bar.height)}" rx="{_num(radius)}"/>'
            )
            if bar.height > radius:
                # Square off the bottom corners: rounded top, flat on the baseline.
                parts.append(
                    f'<rect class="wk-bar {tone}" x="{left}%" y="{_num(bar.y + radius)}" '
                    f'width="{width}%" height="{_num(bar.height - radius)}"/>'
                )
        elif bar.kind == "incomplete":
            parts.append(
                f'<rect class="wk-bar is-incomplete" x="{left}%" y="{_num(bar.y + 0.5)}" '
                f'width="{width}%" height="{_num(bar.height - 0.5)}" rx="4" fill="url(#{hatch})"/>'
            )
        elif bar.kind == "future":
            parts.append(
                f'<line class="wk-future" x1="{left}%" x2="{_num(bar.x + bar_w / 2)}%" '
                f'y1="{PLOT_BOTTOM - 1}" y2="{PLOT_BOTTOM - 1}"/>'
            )
        if bar.label:
            cls = "wk-value"
            if bar.kind == "incomplete":
                cls += " is-incomplete"
            elif bar.kind == "empty":
                cls += " is-muted"
            elif not bar.met:
                cls += " is-short"
            parts.append(
                f'<text class="{cls}" x="{_num(bar.x)}%" y="{_num(bar.label_y)}" '
                f'text-anchor="middle">{escape(bar.label)}</text>'
            )
        is_today = today is not None and bar.day == today
        day_cls = "wk-day is-today" if is_today else ("wk-day is-future" if bar.kind == "future" else "wk-day")
        parts.append(
            f'<text class="{day_cls}" x="{_num(bar.x)}%" y="{PLOT_BOTTOM + 21}" '
            f'text-anchor="middle">{bar.day.strftime("%a")}</text>'
        )
        parts.append("</g>")
    # Drawn last so the dashed target reads across the bars; labelled at its end.
    parts.append(
        f'<line class="wk-target" x1="0" x2="{span}%" y1="{target_y}" y2="{target_y}"/>'
    )
    parts.append(
        f'<text class="wk-target-label" x="100%" y="{target_y}" dy="0.35em" text-anchor="end">'
        f'{_short_hours(target_minutes)}</text>'
    )
    parts.append("</svg>")

    rows = []
    for bar in geometry["bars"]:
        value = {"future": "Not yet", "incomplete": "Incomplete", "empty": "No punches"}.get(
            bar.kind, bar.label
        )
        rows.append(f"<tr><td>{bar.day.strftime('%A %d %B')}</td><td>{escape(value)}</td></tr>")
    parts.append(
        f'<div class="visually-hidden"><table><caption>{escape(label)}. Daily target '
        f"{fmt_minutes(target_minutes)}.</caption>"
        "<thead><tr><th scope=\"col\">Day</th><th scope=\"col\">Effective hours</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )
    return Markup("".join(parts))
