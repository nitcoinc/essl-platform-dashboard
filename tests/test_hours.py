from __future__ import annotations

import unittest
from datetime import date, datetime

from essl_hub.hours import Punch, Segment, compute_day, work_day, work_day_window


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

    def test_ae2_trailing_entry_on_a_past_day_counts_pairs_and_flags(self) -> None:
        # AE2 as revised by R3: the 09:00-13:00 pair counts, the trailing entry is flagged.
        punches = [
            Punch(_t(9, 0), "entry"),
            Punch(_t(13, 0), "exit"),
            Punch(_t(14, 0), "entry"),
        ]
        result = compute_day(punches, target_minutes=480, is_today=False)
        self.assertEqual(result.status, "short")
        self.assertEqual(result.effective_minutes, 240)
        self.assertEqual(result.gross_minutes, 240)
        self.assertEqual(result.segments[-1].end, None)
        self.assertEqual(result.segments[-1].start, _t(14, 0))
        self.assertEqual(result.anomalies, ("No exit after 14:00",))

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

    def test_exit_as_first_punch_is_flagged_not_negative(self) -> None:
        punches = [
            Punch(_t(9, 0), "exit"),
            Punch(_t(13, 0), "entry"),
            Punch(_t(17, 0), "exit"),
        ]
        result = compute_day(punches, target_minutes=480, is_today=False)
        self.assertEqual(result.status, "short")
        self.assertEqual(result.effective_minutes, 240)
        self.assertEqual(result.gross_minutes, 240)
        self.assertEqual(result.anomalies, ("Exit at 09:00 with no entry",))

    def test_short_day_is_flagged_short(self) -> None:
        punches = [Punch(_t(9, 0), "entry"), Punch(_t(15, 0), "exit")]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual(result.status, "short")
        self.assertEqual(result.effective_minutes, 360)


class WorkDayTest(unittest.TestCase):
    def test_night_shift_across_midnight_is_one_work_day(self) -> None:
        punch_in, punch_out = _t(22, 10, day=9), _t(3, 40, day=10)
        self.assertEqual(work_day(punch_in, 6), date(2026, 9, 9))
        self.assertEqual(work_day(punch_out, 6), date(2026, 9, 9))
        result = compute_day(
            [Punch(punch_in, "entry"), Punch(punch_out, "exit")], target_minutes=480
        )
        self.assertEqual(result.status, "short")
        self.assertEqual(result.effective_minutes, 330)

    def test_day_start_hour_belongs_to_the_new_work_day(self) -> None:
        self.assertEqual(work_day(_t(6, 0, day=10), 6), date(2026, 9, 10))
        self.assertEqual(work_day(datetime(2026, 9, 10, 5, 59, 59), 6), date(2026, 9, 9))

    def test_window_is_half_open_from_start_hour_to_next_day(self) -> None:
        start, end = work_day_window(date(2026, 9, 9), 6)
        self.assertEqual(start, _t(6, 0, day=9))
        self.assertEqual(end, _t(6, 0, day=10))
        self.assertEqual(work_day(start, 6), date(2026, 9, 9))
        self.assertEqual(work_day(end, 6), date(2026, 9, 10))

    def test_hour_zero_is_calendar_midnight(self) -> None:
        self.assertEqual(work_day(_t(0, 0, day=10), 0), date(2026, 9, 10))
        self.assertEqual(work_day(_t(23, 59, day=9), 0), date(2026, 9, 9))
        self.assertEqual(work_day_window(date(2026, 9, 9), 0), (_t(0, 0, day=9), _t(0, 0, day=10)))


if __name__ == "__main__":
    unittest.main()


class CleanDayCharacterisationTest(unittest.TestCase):
    """Clean days (every entry has its exit) must score exactly as before U2."""

    def test_two_pairs_met(self) -> None:
        punches = [
            Punch(_t(9, 0), "entry"),
            Punch(_t(13, 0), "exit"),
            Punch(_t(13, 30), "entry"),
            Punch(_t(18, 0), "exit"),
        ]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual(
            (result.status, result.gross_minutes, result.effective_minutes, result.break_minutes),
            ("met", 540, 510, 30),
        )
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.direction_source, "raw_byte")

    def test_single_pair_short(self) -> None:
        result = compute_day([Punch(_t(9, 0), "entry"), Punch(_t(15, 0), "exit")], target_minutes=480)
        self.assertEqual((result.status, result.gross_minutes, result.effective_minutes), ("short", 360, 360))

    def test_alternation_even_count(self) -> None:
        punches = [Punch(_t(9, 0)), Punch(_t(12, 0)), Punch(_t(13, 0)), Punch(_t(18, 0))]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual((result.status, result.effective_minutes, result.break_minutes), ("met", 480, 60))
        self.assertEqual(result.direction_source, "alternation")

    def test_overnight_pair(self) -> None:
        result = compute_day([Punch(_t(22, 10), "entry"), Punch(_t(3, 40, day=11), "exit")], target_minutes=300)
        self.assertEqual((result.status, result.effective_minutes), ("met", 330))

    def test_today_open_clean(self) -> None:
        punches = [Punch(_t(9, 0), "entry"), Punch(_t(12, 0), "exit"), Punch(_t(13, 0), "entry")]
        result = compute_day(punches, target_minutes=480, now=_t(17, 0), is_today=True)
        self.assertEqual(
            (result.status, result.gross_minutes, result.effective_minutes, result.break_minutes),
            ("open", 480, 420, 60),
        )
        self.assertIsNone(result.segments[-1].end)

    def test_empty(self) -> None:
        result = compute_day([], target_minutes=480)
        self.assertEqual((result.status, result.effective_minutes, result.segments), ("empty", 0, ()))


class OrphanTolerantPairingTest(unittest.TestCase):
    def test_clean_days_have_no_anomalies(self) -> None:
        punches = [Punch(_t(9, 0), "entry"), Punch(_t(13, 0), "exit"),
                   Punch(_t(14, 0), "entry"), Punch(_t(18, 0), "exit")]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual((result.status, result.effective_minutes, len(result.segments)), ("met", 480, 2))
        self.assertEqual(result.anomalies, ())

    def test_entry_entry_exit_counts_the_later_pair(self) -> None:
        punches = [Punch(_t(9, 0), "entry"), Punch(_t(14, 33), "entry"), Punch(_t(18, 0), "exit")]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual(result.status, "short")
        self.assertEqual(result.effective_minutes, 207)
        self.assertEqual([(s.start, s.end) for s in result.segments], [(_t(14, 33), _t(18, 0))])
        self.assertEqual(result.anomalies, ("No exit after 09:00",))

    def test_leading_exit_is_flagged_and_pair_counts(self) -> None:
        punches = [Punch(_t(8, 0), "exit"), Punch(_t(9, 0), "entry"), Punch(_t(17, 0), "exit")]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual((result.status, result.effective_minutes), ("met", 480))
        self.assertEqual(result.anomalies, ("Exit at 08:00 with no entry",))

    def test_today_trailing_entry_after_orphans_is_open(self) -> None:
        punches = [Punch(_t(14, 33), "entry"), Punch(_t(17, 52), "entry"), Punch(_t(19, 44), "entry")]
        result = compute_day(punches, target_minutes=480, now=_t(22, 0), is_today=True)
        self.assertEqual(result.status, "open")
        self.assertEqual(result.effective_minutes, 136)
        self.assertEqual(result.segments[-1], Segment(start=_t(19, 44), end=None))
        self.assertEqual(result.anomalies, ("No exit after 14:33", "No exit after 17:52"))

    def test_past_day_single_entry_is_incomplete(self) -> None:
        result = compute_day([Punch(_t(9, 0), "entry")], target_minutes=480)
        self.assertEqual((result.status, result.effective_minutes, result.gross_minutes), ("incomplete", 0, None))
        self.assertEqual(result.anomalies, ("No exit after 09:00",))

    def test_alternation_odd_count_past_day_counts_pairs(self) -> None:
        punches = [Punch(_t(9, 0)), Punch(_t(13, 0)), Punch(_t(14, 0)), Punch(_t(18, 0)), Punch(_t(19, 0))]
        result = compute_day(punches, target_minutes=480)
        self.assertEqual((result.status, result.effective_minutes, result.gross_minutes), ("met", 480, 540))
        self.assertEqual(result.anomalies, ("No exit after 19:00",))
