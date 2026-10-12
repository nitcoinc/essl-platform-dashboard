from __future__ import annotations

import unittest
from datetime import datetime

from essl_hub.hours import Punch, compute_day, group_by_day


def _t(hour: int, minute: int, day: int = 10) -> datetime:
    return datetime(2026, 9, day, hour, minute, 0)


class ComputeDayTest(unittest.TestCase):
    def test_ae1_normal_day_with_one_lunch_break(self) -> None:
        # Covers AE1.
        punches = [
            Punch(_t(9, 0), "entry"),
            Punch(_t(13, 0), "exit"),
            Punch(_t(14, 0), "entry"),
            Punch(_t(18, 15), "exit"),
        ]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual(result.status, "met")
        self.assertEqual(result.gross_minutes, 9 * 60 + 15)
        self.assertEqual(result.effective_minutes, 8 * 60 + 15)
        self.assertEqual(result.break_minutes, 60)

    def test_ae2_trailing_entry_on_a_past_day_is_incomplete(self) -> None:
        # Covers AE2.
        punches = [
            Punch(_t(9, 0), "entry"),
            Punch(_t(13, 0), "exit"),
            Punch(_t(14, 0), "entry"),
        ]
        result = compute_day(punches, target_minutes=480, is_today=False)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.effective_minutes, 0)
        self.assertIsNone(result.gross_minutes)
        self.assertEqual(result.segments[-1].end, None)
        self.assertEqual(result.segments[-1].start, _t(14, 0))

    def test_ae3_today_still_inside_counts_to_now(self) -> None:
        # Covers AE3.
        punches = [
            Punch(_t(9, 0), "entry"),
            Punch(_t(13, 0), "exit"),
            Punch(_t(14, 0), "entry"),
        ]
        result = compute_day(punches, target_minutes=480, now=_t(15, 30), is_today=True)
        self.assertEqual(result.status, "open")
        self.assertEqual(result.effective_minutes, 5 * 60 + 30)
        remaining = 480 - result.effective_minutes
        self.assertEqual(remaining, 150)

    def test_ae5_undecodable_direction_byte_resolves_by_alternation(self) -> None:
        # Covers AE5.
        punches = [
            Punch(_t(9, 0), None),
            Punch(_t(13, 0), None),
        ]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual(result.direction_source, "alternation")
        self.assertEqual(result.effective_minutes, 4 * 60)

    def test_three_short_breaks_match_captured_real_sequence(self) -> None:
        punches = [
            Punch(_t(9, 2), "entry"),
            Punch(_t(11, 40), "exit"),
            Punch(_t(11, 48), "entry"),
            Punch(_t(13, 31), "exit"),
            Punch(_t(13, 36), "entry"),
            Punch(_t(16, 12), "exit"),
            Punch(_t(16, 20), "entry"),
            Punch(_t(17, 36), "exit"),
        ]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual(result.gross_minutes, 8 * 60 + 34)
        self.assertEqual(result.effective_minutes, 8 * 60 + 13)
        self.assertEqual(result.break_minutes, 21)
        self.assertEqual(result.status, "met")

    def test_single_entry_with_no_exit_today_is_open_not_incomplete(self) -> None:
        result = compute_day(
            [Punch(_t(9, 0), "entry")],
            target_minutes=480,
            now=_t(10, 0),
            is_today=True,
        )
        self.assertEqual(result.status, "open")
        self.assertEqual(result.effective_minutes, 60)

    def test_day_with_no_punches_is_neither_met_nor_incomplete(self) -> None:
        result = compute_day([], target_minutes=480)
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.effective_minutes, 0)
        self.assertEqual(result.gross_minutes, 0)

    def test_punches_out_of_order_are_sorted_before_pairing(self) -> None:
        punches = [
            Punch(_t(16, 0), "exit"),
            Punch(_t(9, 0), "entry"),
        ]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual(result.status, "short")
        self.assertEqual(result.effective_minutes, 7 * 60)

    def test_exit_as_first_punch_is_incomplete_not_negative(self) -> None:
        punches = [
            Punch(_t(9, 0), "exit"),
            Punch(_t(13, 0), "entry"),
            Punch(_t(17, 0), "exit"),
        ]
        result = compute_day(punches, target_minutes=480, is_today=False)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.effective_minutes, 0)
        self.assertIsNone(result.gross_minutes)

    def test_short_day_is_flagged_short(self) -> None:
        punches = [Punch(_t(9, 0), "entry"), Punch(_t(15, 0), "exit")]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual(result.status, "short")
        self.assertEqual(result.effective_minutes, 360)


class GroupByDayTest(unittest.TestCase):
    def test_groups_punches_by_calendar_day(self) -> None:
        punches = [
            Punch(_t(9, 0, day=9), "entry"),
            Punch(_t(17, 0, day=9), "exit"),
            Punch(_t(9, 0, day=10), "entry"),
        ]
        buckets = group_by_day(punches)
        self.assertEqual(len(buckets), 2)
        self.assertEqual(len(buckets[_t(9, 0, day=9).date()]), 2)
        self.assertEqual(len(buckets[_t(9, 0, day=10).date()]), 1)


if __name__ == "__main__":
    unittest.main()
