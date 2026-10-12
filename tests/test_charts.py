from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from essl_hub import charts
from essl_hub.hours import DayResult


def _result(status, effective, segments=()):
    return DayResult(
        status=status,
        gross_minutes=None if status == "incomplete" else effective,
        effective_minutes=effective,
        break_minutes=0,
        segments=tuple(segments),
        direction_source="raw_byte",
    )


class WeekBarsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.days = [
            {"date": date(2026, 10, 5), "result": _result("met", 540)},
            {"date": date(2026, 10, 6), "result": _result("incomplete", 0)},
            {"date": date(2026, 10, 7), "result": _result("empty", 0)},
            {"date": date(2026, 10, 8), "result": None},
        ]

    def test_nine_hour_day_rises_above_the_target_line(self) -> None:
        geometry = charts.week_bars(self.days, 480)
        nine = geometry["bars"][0]
        self.assertLess(nine.y, geometry["target_y"])
        self.assertEqual(nine.label, "9h 00m")
        self.assertEqual(nine.kind, "worked")

    def test_incomplete_day_is_an_outlined_labelled_marker_not_a_value(self) -> None:
        bar = charts.week_bars(self.days, 480)["bars"][1]
        self.assertEqual(bar.kind, "incomplete")
        self.assertEqual(bar.label, "Incomplete")
        self.assertIsNone(bar.minutes)
        svg = charts.render_week_bars(self.days, 480, label="Week")
        self.assertIn('class="wk-bar is-incomplete"', svg)
        self.assertIn(">Incomplete</text>", svg)

    def test_future_day_has_no_bar(self) -> None:
        bar = charts.week_bars(self.days, 480)["bars"][3]
        self.assertEqual((bar.kind, bar.height), ("future", 0))

    def test_svg_is_accessible_with_dashed_target_and_hidden_table(self) -> None:
        svg = str(charts.render_week_bars(self.days, 480, label="Effective hours"))
        self.assertIn('role="img"', svg)
        self.assertIn(">Effective hours</title>", svg)
        self.assertIn('class="wk-target"', svg)
        self.assertIn('<div class="visually-hidden"><table>', svg)
        self.assertIn("<td>Incomplete</td>", svg)
        self.assertIn("<td>Not yet</td>", svg)

    def test_scale_runs_zero_to_ten_hours_in_two_hour_ticks(self) -> None:
        ticks = charts.week_bars(self.days, 480)["ticks"]
        self.assertEqual([t["label"] for t in ticks], ["0h", "2h", "4h", "6h", "8h", "10h"])

    def test_value_labels_never_touch_the_target_line(self) -> None:
        for minutes in range(300, 660, 5):
            days = [{"date": date(2026, 10, 5), "result": _result("short", minutes)}]
            geometry = charts.week_bars(days, 480)
            bar, target = geometry["bars"][0], geometry["target_y"]
            top, bottom = bar.label_y - charts.LABEL_HEIGHT + 3, bar.label_y + 3
            self.assertFalse(
                top < target + charts.TARGET_CLEAR and bottom > target - charts.TARGET_CLEAR,
                f"label for {minutes} min sits on the target line",
            )

    def test_bars_are_coloured_by_outcome_and_today_is_marked(self) -> None:
        days = self.days[:1] + [{"date": date(2026, 10, 6), "result": _result("short", 405)}]
        svg = str(charts.render_week_bars(days, 480, label="Week", today=date(2026, 10, 6)))
        self.assertIn('class="wk-bar is-met"', svg)
        self.assertIn('class="wk-bar is-short"', svg)
        self.assertIn('class="wk-today"', svg)
        self.assertIn('class="wk-day is-today"', svg)
        self.assertIn("<title>Tue 06 Oct: 6h 45m, short by 1h 15m</title>", svg)
        self.assertIn('class="wk-target-label"', svg)

    def test_label_is_escaped(self) -> None:
        svg = str(charts.render_week_bars(self.days, 480, label="<b>x</b>"))
        self.assertNotIn("<b>x</b>", svg)


if __name__ == "__main__":
    unittest.main()
