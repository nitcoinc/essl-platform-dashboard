import unittest
from datetime import datetime

from essl_hub import day_story
from essl_hub.hours import DayResult, Segment

D = datetime(2026, 10, 5)


def _at(h, m=0):
    return D.replace(hour=h, minute=m)


def _result(status, effective, segments):
    return DayResult(status=status, gross_minutes=None, effective_minutes=effective,
                     break_minutes=0, segments=tuple(segments), direction_source="raw_byte")


class HeadlineTest(unittest.TestCase):
    def test_open_short_shows_time_to_go_and_projected_finish(self) -> None:
        r = _result("open", 120, [Segment(_at(9), None)])
        h = day_story.headline(r, 480, _at(11), is_today=True, weekend=False)
        self.assertEqual((h.text, h.tone), ("Inside · 6h 00m to go", "info"))
        self.assertEqual(day_story.target_reached_at(r, 480, _at(11)), _at(17))

    def test_met_day_reports_when_target_was_crossed_across_breaks(self) -> None:
        r = _result("met", 510, [Segment(_at(9), _at(13)), Segment(_at(14), _at(18, 30))])
        h = day_story.headline(r, 480, None, is_today=False, weekend=False)
        self.assertEqual((h.text, h.tone), ("Target met at 18:00", "ok"))

    def test_short_incomplete_weekend_and_not_in(self) -> None:
        short = _result("short", 400, [Segment(_at(9), _at(15, 40))])
        self.assertEqual(day_story.headline(short, 480, None, is_today=False, weekend=False).text, "Short by 1h 20m")
        bad = _result("incomplete", 0, [Segment(_at(9), None)])
        self.assertEqual(day_story.headline(bad, 480, None, is_today=False, weekend=False).tone, "bad")
        empty = _result("empty", 0, [])
        self.assertEqual(day_story.headline(empty, 480, None, is_today=False, weekend=True).text, "Weekend")
        self.assertEqual(day_story.headline(empty, 480, _at(8), is_today=True, weekend=False).text, "Not in yet")


class LogTest(unittest.TestCase):
    def test_breaks_between_stretches_and_now_node_while_inside(self) -> None:
        r = _result("open", 0, [Segment(_at(9), _at(13)), Segment(_at(13, 32), None)])
        items = day_story.log_items(r, _at(15))
        self.assertEqual([i.kind for i in items], ["in", "out", "break", "in", "now"])
        self.assertEqual(items[0].minutes, 240)
        self.assertEqual(items[2].minutes, 32)
        self.assertEqual(items[3].minutes, 88)

    def test_missing_exit_marks_the_gap(self) -> None:
        r = _result("incomplete", 0, [Segment(_at(9), None)])
        self.assertEqual([i.kind for i in day_story.log_items(r, None)], ["in", "missing"])

    def test_ring_percent_caps_at_100(self) -> None:
        self.assertEqual(day_story.ring_percent(_result("met", 600, []), 480), 100)
        self.assertEqual(day_story.ring_percent(_result("short", 240, []), 480), 50)


if __name__ == "__main__":
    unittest.main()
