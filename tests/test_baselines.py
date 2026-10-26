"""U3 baseline engine: day counts, the KTD2 limit and the time spreads."""

from __future__ import annotations

import json
import random
import unittest
from datetime import date, datetime, time, timedelta

from essl_hub import baselines
from essl_hub.baselines import Baseline, BreakStats, compute_baseline, percentile
from essl_hub.hours import Punch, compute_day

TODAY = date(2026, 10, 27)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    """A time on work day ``day`` (06:00 start): hours before 6 are next morning."""
    moment = datetime.combine(day, time(hour, minute))
    return moment + timedelta(days=1) if hour < baselines.DEFAULT_DAY_START_HOUR else moment


def _day(day: date, *swipes: tuple[int, int, str]):
    punches = [Punch(_at(day, h, m), direction) for h, m, direction in swipes]
    return compute_day(punches, target_minutes=480)


def _clean(day: date, break_minutes: int = 40, last_out: tuple[int, int] = (18, 0)):
    """In 09:00, out 13:00, back after ``break_minutes``, out at ``last_out``."""
    back = _at(day, 13) + timedelta(minutes=break_minutes)
    punches = [Punch(_at(day, 9), "entry"), Punch(_at(day, 13), "exit")]
    if break_minutes:
        punches.append(Punch(back, "entry"))
    else:
        punches.pop()
    punches.append(Punch(_at(day, *last_out), "exit"))
    return compute_day(punches, target_minutes=480)


def _days(count: int, start: int = 1) -> list[date]:
    return [TODAY - timedelta(days=k) for k in range(start, start + count)]


def _breaks(minutes: list[int]) -> dict:
    return {d: _clean(d, m) for d, m in zip(_days(len(minutes)), minutes)}


class DayCountTest(unittest.TestCase):
    def test_nine_clean_days_give_no_live_limit_ten_do(self) -> None:
        nine = compute_baseline(_breaks([40] * 9), today=TODAY)
        self.assertEqual((nine.punched_days, nine.clean_days), (9, 9))
        self.assertIsNone(nine.breaks)
        self.assertIsNone(nine.metrics())
        self.assertFalse(nine.live_eligible)
        self.assertFalse(nine.morning_eligible)

        ten = compute_baseline(_breaks([40] * 10), today=TODAY)
        self.assertEqual(ten.clean_days, 10)
        self.assertTrue(ten.live_eligible)
        self.assertTrue(ten.morning_eligible)
        self.assertEqual(ten.limit_minutes, 90)

    def test_twelve_punched_four_clean_is_morning_only(self) -> None:
        days = _days(12)
        results = {d: _clean(d) for d in days[:4]}
        results.update({d: _day(d, (9, 0, "entry")) for d in days[4:]})
        base = compute_baseline(results, today=TODAY)
        self.assertEqual((base.punched_days, base.clean_days), (12, 4))
        self.assertTrue(base.morning_eligible)
        self.assertFalse(base.live_eligible)
        self.assertIsNone(base.limit_minutes)

    def test_missing_exit_and_unclear_gap_days_count_as_punched_not_clean(self) -> None:
        days = _days(12)
        no_exit = _day(days[0], (9, 0, "entry"), (13, 0, "exit"), (17, 0, "entry"))
        self.assertIn("No exit after 17:00", no_exit.anomalies)
        unclear = _day(days[1], (9, 0, "entry"), (12, 0, "exit"), (12, 30, "exit"),
                       (13, 0, "entry"), (18, 0, "exit"))
        self.assertEqual([g.kind for g in unclear.gaps], ["unclear"])
        results = {days[0]: no_exit, days[1]: unclear}
        results.update({d: _clean(d) for d in days[2:]})
        base = compute_baseline(results, today=TODAY)
        self.assertEqual((base.punched_days, base.clean_days), (12, 10))

    def test_empty_days_are_not_punched(self) -> None:
        results = _breaks([40] * 10)
        results[TODAY - timedelta(days=20)] = compute_day([], target_minutes=480)
        self.assertEqual(compute_baseline(results, today=TODAY).punched_days, 10)

    def test_request_days_are_punched_but_not_clean(self) -> None:
        results = _breaks([40] * 10)
        days = _days(3)
        excluded = baselines.excluded_request_days([
            (days[0], "pending"), (days[1], "rejected"), (days[2], "closed_correct"),
        ])
        self.assertEqual(excluded, {days[0]})
        base = compute_baseline(results, today=TODAY, request_days=excluded)
        self.assertEqual((base.punched_days, base.clean_days), (10, 9))
        self.assertFalse(base.live_eligible)
        self.assertEqual(
            baselines.excluded_request_days([(TODAY, "approved"), (TODAY, "partial")]), {TODAY}
        )

    def test_only_the_lookback_trains(self) -> None:
        results = _breaks([40] * 9)
        # Today (still being built) and a day just past 8 weeks never count.
        results[TODAY] = _clean(TODAY)
        results[TODAY - timedelta(days=baselines.LOOKBACK_DAYS + 1)] = _clean(TODAY)
        self.assertEqual(compute_baseline(results, today=TODAY).punched_days, 9)
        oldest = TODAY - timedelta(days=baselines.LOOKBACK_DAYS)
        results[oldest] = _clean(oldest)
        self.assertEqual(compute_baseline(results, today=TODAY).clean_days, 10)
        self.assertEqual(baselines.lookback_days(TODAY), (oldest, TODAY - timedelta(days=1)))


class LimitTest(unittest.TestCase):
    def test_short_breaks_give_the_floor(self) -> None:
        base = compute_baseline(_breaks([30, 33, 35, 38, 40, 42, 45, 31, 44, 36]), today=TODAY)
        self.assertEqual(base.limit_minutes, baselines.LIMIT_FLOOR_MINUTES)

    def test_one_outlier_leaves_the_limit_near_the_floor(self) -> None:
        base = compute_baseline(_breaks([40] * 20 + [180]), today=TODAY)
        self.assertEqual(base.breaks.median, 40)
        self.assertEqual(base.breaks.mad, 0)
        self.assertEqual(base.limit_minutes, 90)

    def test_identical_breaks_give_mad_zero(self) -> None:
        base = compute_baseline(_breaks([60] * 12), today=TODAY)
        self.assertEqual(base.breaks, BreakStats(median=60, mad=0, p90=60, limit=90))

    def test_long_breaks_cap_at_four_hours(self) -> None:
        base = compute_baseline(_breaks([170, 180, 190] * 4), today=TODAY)
        self.assertEqual(base.limit_minutes, baselines.LIMIT_CAP_MINUTES)

    def test_spread_term_wins_between_floor_and_cap(self) -> None:
        minutes = [40, 50, 60, 70, 80, 90, 100, 110, 120, 130]
        base = compute_baseline(_breaks(minutes), today=TODAY)
        # median 85, MAD 25 -> 85 + 4 x 1.4826 x 25 = 233.26; 1.5 x p90 (121) = 181.5
        self.assertEqual(base.breaks.median, 85)
        self.assertEqual(base.breaks.mad, 25)
        self.assertEqual(base.breaks.p90, 121)
        self.assertEqual(base.limit_minutes, 233.3)

    def test_a_day_without_a_break_trains_as_zero(self) -> None:
        days = _days(10)
        results = {d: _clean(d, break_minutes=0) for d in days[:5]}
        results.update({d: _clean(d, break_minutes=60) for d in days[5:]})
        self.assertEqual(results[days[0]].gaps, ())
        base = compute_baseline(results, today=TODAY)
        self.assertEqual(base.clean_days, 10)
        # Five zeros and five 60s: the zeros pull the median down to 30.
        self.assertEqual(base.breaks.median, 30)
        self.assertEqual(base.breaks.mad, 30)


class SpreadTest(unittest.TestCase):
    def test_last_out_and_first_in_in_minutes_since_day_start(self) -> None:
        base = compute_baseline(_breaks([40] * 10), today=TODAY)
        # 09:00 and 18:00 with the work day starting 06:00.
        self.assertEqual(base.first_in.as_dict(), {"p10": 180, "median": 180, "p90": 180})
        self.assertEqual(base.last_out.as_dict(), {"p10": 720, "median": 720, "p90": 720})

    def test_after_midnight_last_out_sorts_after_evening(self) -> None:
        days = _days(10)
        results = {d: _clean(d, last_out=(23, 0)) for d in days[:5]}
        results.update({d: _clean(d, last_out=(1, 0)) for d in days[5:]})
        base = compute_baseline(results, today=TODAY)
        # 23:00 is 1020 min after 06:00; 01:00 next morning is 1140, not 60.
        self.assertEqual(base.last_out.p10, 1020)
        self.assertEqual(base.last_out.median, 1080)
        self.assertEqual(base.last_out.p90, 1140)

    def test_day_start_hour_moves_the_origin(self) -> None:
        base = compute_baseline(_breaks([40] * 10), today=TODAY, day_start_hour=4)
        self.assertEqual(base.first_in.median, 300)


class DeterminismTest(unittest.TestCase):
    def test_percentile_is_linear_between_ranks(self) -> None:
        self.assertEqual(percentile([1, 2, 3, 4], 0.9), 3.7)
        self.assertEqual(percentile([4, 1, 3, 2], 0.5), 2.5)
        self.assertEqual(percentile([7], 0.1), 7)
        self.assertEqual(percentile([1, 2, 3, 4], 0.0), 1)
        self.assertEqual(percentile([1, 2, 3, 4], 1.0), 4)
        with self.assertRaises(ValueError):
            percentile([], 0.5)

    def test_same_input_any_order_gives_same_baseline(self) -> None:
        minutes = [25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80]
        expected = compute_baseline(_breaks(minutes), today=TODAY)
        rng = random.Random(7)
        for _ in range(5):
            items = list(_breaks(minutes).items())
            rng.shuffle(items)
            self.assertEqual(compute_baseline(dict(items), today=TODAY), expected)

    def test_metrics_round_trip_through_json(self) -> None:
        base = compute_baseline(_breaks([40, 41, 43, 47, 52, 58, 61, 66, 70, 79]), today=TODAY)
        stored = json.dumps(base.metrics())
        self.assertEqual(Baseline.from_stored(base.punched_days, base.clean_days, stored), base)
        self.assertEqual(Baseline.from_stored(10, 10, json.loads(stored)), base)
        self.assertEqual(set(base.metrics()), {"break", "last_out", "first_in"})

    def test_no_metrics_round_trip(self) -> None:
        self.assertEqual(Baseline.from_stored(12, 4, None), Baseline(12, 4))
        self.assertFalse(Baseline.from_stored(12, 4, None).live_eligible)


if __name__ == "__main__":
    unittest.main()
