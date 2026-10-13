"""U10 break math: characterization of clean days, then duplicates, unpaired
swipes and unclear gaps (KTD9)."""

from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta

from essl_hub import day_story
from essl_hub.hours import Punch, Segment, compute_day


def _t(h: int, m: int = 0, s: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 9, day, h, m, s)


def _summary(r):
    return (r.status, r.gross_minutes, r.effective_minutes, r.break_minutes, r.segments,
            r.direction_source, r.anomalies, r.effective_seconds, r.gross_seconds, r.break_seconds)


class CleanDayCharacterizationTest(unittest.TestCase):
    """Pinned before U10 changed hours.py; must stay identical."""

    def test_clean_raw_byte_day(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit"),
                         Punch(_t(14), "entry"), Punch(_t(18), "exit")], target_minutes=480)
        self.assertEqual(_summary(r), (
            "met", 540, 480, 60, (Segment(_t(9), _t(13)), Segment(_t(14), _t(18))),
            "raw_byte", (), 28800, 32400, 3600))
        kinds = [(i.kind, i.seconds) for i in day_story.log_items(r, None)]
        self.assertEqual(kinds, [("in", 14400), ("out", None), ("break", 3600), ("in", 14400), ("out", None)])

    def test_clean_alternation_day_with_seconds(self) -> None:
        r = compute_day([Punch(_t(9, 0, 12)), Punch(_t(12, 40, 59)), Punch(_t(13, 15, 3)),
                         Punch(_t(17, 2, 41))], target_minutes=480)
        self.assertEqual(_summary(r), (
            "short", 482, 448, 34,
            (Segment(_t(9, 0, 12), _t(12, 40, 59)), Segment(_t(13, 15, 3), _t(17, 2, 41))),
            "alternation", (), 13247 + 13658, 28949, 28949 - 13247 - 13658))

    def test_night_shift(self) -> None:
        punches = [Punch(_t(21), "entry"), Punch(_t(1, day=11), "exit"),
                   Punch(_t(1, 30, day=11), "entry"), Punch(_t(5, day=11), "exit")]
        r = compute_day(punches, target_minutes=480)
        self.assertEqual((r.gross_seconds, r.effective_seconds, r.break_seconds, r.break_minutes),
                         (8 * 3600, 7.5 * 3600, 1800, 30))

    def test_open_day(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit"), Punch(_t(13, 32), "entry")],
                        target_minutes=480, now=_t(15), is_today=True)
        self.assertEqual(_summary(r), (
            "open", 360, 328, 32, (Segment(_t(9), _t(13)), Segment(_t(13, 32), None)),
            "raw_byte", (), 328 * 60, 360 * 60, 32 * 60))
        self.assertEqual([i.kind for i in day_story.log_items(r, _t(15))], ["in", "out", "break", "in", "now"])

    def test_past_day_trailing_entry(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit")], target_minutes=480)
        self.assertEqual(_summary(r), (
            "short", 240, 240, 0, (Segment(_t(9), _t(13)),), "raw_byte", (), 14400, 14400, 0))


class BreakMathTest(unittest.TestCase):
    def test_clean_day_one_break_gap(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit"),
                         Punch(_t(14), "entry"), Punch(_t(18), "exit")], target_minutes=480)
        self.assertEqual(len(r.gaps), 1)
        g = r.gaps[0]
        self.assertEqual((g.kind, g.number, g.start, g.end, g.seconds, g.unpaired),
                         ("break", 1, _t(13), _t(14), 3600, ()))
        self.assertEqual((r.unclear_seconds, r.collapsed, r.unpaired), (0, (), ()))

    def test_direction_less_duplicate_collapsed(self) -> None:
        dup = Punch(_t(9, 0, 4))
        r = compute_day([Punch(_t(9)), dup, Punch(_t(13)), Punch(_t(14)), Punch(_t(18))], target_minutes=480)
        self.assertEqual(r.segments, (Segment(_t(9), _t(13)), Segment(_t(14), _t(18))))
        self.assertEqual(r.effective_seconds, 8 * 3600)
        self.assertEqual([g.kind for g in r.gaps], ["break"])
        self.assertEqual(r.collapsed, (dup,))
        self.assertEqual(r.anomalies, ())

    def test_orphan_exit_makes_unclear_gap(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit"), Punch(_t(14), "exit"),
                         Punch(_t(15), "entry"), Punch(_t(18), "exit")], target_minutes=480)
        self.assertEqual(r.break_seconds, 0)
        self.assertEqual(r.break_minutes, 0)
        self.assertEqual(len(r.gaps), 1)
        g = r.gaps[0]
        self.assertEqual((g.kind, g.number, g.start, g.end, g.seconds, g.unpaired),
                         ("unclear", 1, _t(13), _t(15), 7200, ((_t(14), "exit"),)))
        self.assertEqual(r.unclear_seconds, 7200)
        self.assertEqual(r.unpaired, ((_t(14), "exit"),))
        kinds = [i.kind for i in day_story.log_items(r, None)]
        self.assertEqual(kinds, ["in", "out", "unclear", "in", "out"])

    def test_superseded_entry_makes_unclear_gap(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit"), Punch(_t(14), "entry"),
                         Punch(_t(15), "entry"), Punch(_t(15, 30), "exit")], target_minutes=480)
        self.assertEqual([(g.kind, g.start, g.end, g.unpaired) for g in r.gaps],
                         [("unclear", _t(13), _t(15), ((_t(14), "entry"),))])
        self.assertEqual(r.break_seconds, 0)

    def test_mixed_device_duplicates_collapsed(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(9, 1)), Punch(_t(13), "exit"),
                         Punch(_t(14), "entry"), Punch(_t(14, 1)), Punch(_t(18), "exit")], target_minutes=480)
        self.assertEqual(r.effective_seconds, 8 * 3600)
        self.assertEqual(len(r.collapsed), 2)
        self.assertEqual(r.break_seconds, 3600)

    def test_past_day_no_break_before_missing_exit(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit"), Punch(_t(14), "entry")],
                        target_minutes=480)
        self.assertEqual((r.gaps, r.break_seconds), ((), 0))
        self.assertEqual([i.kind for i in day_story.log_items(r, None)], ["in", "out", "in", "missing"])

    def test_61_seconds_apart_both_count(self) -> None:
        r = compute_day([Punch(_t(9)), Punch(_t(9, 1, 1)), Punch(_t(10)), Punch(_t(18))], target_minutes=480)
        self.assertEqual(r.collapsed, ())
        self.assertEqual(r.segments, (Segment(_t(9), _t(9, 1, 1)), Segment(_t(10), _t(18))))

    def test_collapse_measures_from_previous_kept_swipe(self) -> None:
        r = compute_day([Punch(_t(9)), Punch(_t(9, 0, 50)), Punch(_t(9, 1, 30)), Punch(_t(18))],
                        target_minutes=480)
        # 09:00:50 collapsed (50s after 09:00); 09:01:30 is 90s after the kept 09:00.
        self.assertEqual(len(r.collapsed), 1)

    def test_open_day_breaks_equal_listed_breaks(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(12), "exit"), Punch(_t(12, 30), "entry"),
                         Punch(_t(13), "exit"), Punch(_t(14), "entry")],
                        target_minutes=480, now=_t(16), is_today=True)
        self.assertEqual(r.status, "open")
        self.assertEqual(r.break_seconds, sum(g.seconds for g in r.gaps if g.kind == "break"))
        self.assertEqual([g.number for g in r.gaps], [1, 2])

    def test_span_equals_inside_plus_breaks_plus_unclear(self) -> None:
        rng = random.Random(7)
        for _ in range(500):
            stamp = _t(7)
            punches = []
            for _ in range(rng.randint(1, 12)):
                stamp += timedelta(seconds=rng.choice([5, 30, 61, 600, 3600, 5400]))
                punches.append(Punch(stamp, rng.choice(["entry", "exit", None])))
            r = compute_day(punches, target_minutes=480)
            if r.gross_seconds is None:
                continue
            self.assertEqual(r.gross_seconds, r.effective_seconds + r.break_seconds + r.unclear_seconds)


class MagneticDoorPairingTest(unittest.TestCase):
    """Every recorded swipe is real: a direction-less swipe fills a missing
    alternation before it can be a duplicate."""

    def test_real_day_filler_swipe_becomes_in(self) -> None:
        punches = [Punch(_t(15, 37, 59), "entry"), Punch(_t(16, 14, 52), "exit"),
                   Punch(_t(16, 48, 3), "entry"), Punch(_t(17, 48, 13), "exit"),
                   Punch(_t(17, 48, 32)), Punch(_t(18, 46, 55), "exit"),
                   Punch(_t(19, 14, 54), "entry"), Punch(_t(20, 0, 37), "exit"),
                   Punch(_t(20, 23, 50), "entry")]
        r = compute_day(punches, target_minutes=480)
        self.assertEqual([(g.kind, g.number, g.start, g.end, g.seconds) for g in r.gaps], [
            ("break", 1, _t(16, 14, 52), _t(16, 48, 3), 1991),
            ("break", 2, _t(17, 48, 13), _t(17, 48, 32), 19),
            ("break", 3, _t(18, 46, 55), _t(19, 14, 54), 1679),
        ])
        self.assertEqual((r.collapsed, r.inferred, r.unclear_seconds), ((), (_t(17, 48, 32),), 0))
        self.assertEqual(r.unpaired, ((_t(20, 23, 50), "entry"),))
        self.assertEqual((r.gross_seconds, r.effective_seconds, r.break_seconds),
                         (15758, 15758 - 1991 - 19 - 1679, 1991 + 19 + 1679))
        live = compute_day(punches, target_minutes=480, now=_t(21), is_today=True)
        self.assertEqual([g.seconds for g in live.gaps], [1991, 19, 1679, 1393])
        self.assertEqual(live.gross_seconds, live.effective_seconds + live.break_seconds)

    def test_swipe_between_out_and_in_within_window_is_duplicate(self) -> None:
        dup = Punch(_t(13, 0, 20))
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit"), dup,
                         Punch(_t(14), "entry"), Punch(_t(18), "exit")], target_minutes=480)
        self.assertEqual((r.collapsed, r.inferred), ((dup,), ()))
        self.assertEqual([(g.kind, g.seconds) for g in r.gaps], [("break", 3600)])

    def test_pure_direction_less_double_swipe_still_collapsed(self) -> None:
        dup = Punch(_t(13, 0, 4))
        r = compute_day([Punch(_t(9)), Punch(_t(13)), dup, Punch(_t(14)), Punch(_t(18))], target_minutes=480)
        self.assertEqual((r.collapsed, r.inferred), ((dup,), ()))
        self.assertEqual(r.segments, (Segment(_t(9), _t(13)), Segment(_t(14), _t(18))))

    def test_same_direction_reswipe_not_counted(self) -> None:
        again = Punch(_t(13, 0, 30), "exit")
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit"), again,
                         Punch(_t(14), "entry"), Punch(_t(18), "exit")], target_minutes=480)
        self.assertEqual(r.collapsed, (again,))
        self.assertEqual(r.break_seconds, 3600)

    def test_known_directions_never_collapsed_across_direction(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit"), Punch(_t(13, 0, 19), "entry"),
                         Punch(_t(18), "exit")], target_minutes=480)
        self.assertEqual(r.collapsed, ())
        self.assertEqual([(g.kind, g.seconds) for g in r.gaps], [("break", 19)])

    def test_out_out_without_filler_stays_unclear(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(13), "exit"), Punch(_t(14), "exit"),
                         Punch(_t(15), "entry"), Punch(_t(18), "exit")], target_minutes=480)
        self.assertEqual([(g.kind, g.seconds) for g in r.gaps], [("unclear", 7200)])
        self.assertEqual(r.gross_seconds, r.effective_seconds + r.break_seconds + r.unclear_seconds)


if __name__ == "__main__":
    unittest.main()
