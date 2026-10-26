"""U4 detection rules: the live out-too-long decision and the morning issues,
from real compute_day results."""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from essl_hub import punch_alerts
from essl_hub.alert_mail import DOUBLE_IN, MISSING_FINAL_OUT, OUT_WITHOUT_IN, UNCLEAR_GAP, MorningIssue
from essl_hub.baselines import Baseline, BreakStats, Spread
from essl_hub.collectors import _c3_trusted_direction
from essl_hub.db import C3_UNDECODED_BYTE
from essl_hub.hours import Punch, compute_day, punch_from_row
from essl_hub.punch_alerts import LiveAlert, live_decision, morning_issues

DAY_START = datetime(2026, 10, 27, 6, 0)


def _t(h: int, m: int = 0) -> datetime:
    return datetime(2026, 10, 27, h, m)


def _baseline(limit: float = 90, last_out_p10: float = 720) -> Baseline:
    """Twenty clean days; last-out p10 defaults to 18:00 (720 min after 06:00)."""
    return Baseline(
        20, 20,
        BreakStats(median=40, mad=5, p90=50, limit=limit),
        Spread(p10=last_out_p10, median=last_out_p10 + 30, p90=last_out_p10 + 60),
        Spread(p10=180, median=190, p90=200),
    )


def _today(now: datetime, *swipes: tuple[int, int, str | None]):
    punches = [Punch(_t(h, m), direction) for h, m, direction in swipes]
    return compute_day(punches, target_minutes=480, now=now, is_today=True)


def _yesterday(*swipes: tuple[int, int, str | None]):
    punches = [Punch(_t(h, m), direction) for h, m, direction in swipes]
    return compute_day(punches, target_minutes=480)


def _decide(day, now: datetime, baseline: Baseline | None = None, **kwargs):
    return live_decision(day, baseline if baseline is not None else _baseline(), now=now,
                         day_start=DAY_START, **kwargs)


class LiveDecisionTest(unittest.TestCase):
    def test_out_past_limit_plus_grace_alerts(self) -> None:
        now = _t(14, 45)
        day = _today(now, (9, 0, "entry"), (13, 0, "exit"))
        self.assertEqual(_decide(day, now), LiveAlert(out_since=_t(13), out_minutes=105))

    def test_just_under_limit_plus_grace_does_not(self) -> None:
        now = _t(14, 40)
        day = _today(now, (9, 0, "entry"), (13, 0, "exit"))
        self.assertEqual(_decide(day, now), "under_limit")

    def test_after_usual_last_out(self) -> None:
        now = _t(15, 0)
        day = _today(now, (9, 0, "entry"), (13, 0, "exit"))
        # Last-out p10 at 14:30 = 510 min after the 06:00 day start.
        self.assertEqual(_decide(day, now, _baseline(last_out_p10=510)), "after_usual_last_out")
        self.assertIsInstance(_decide(day, now, _baseline(last_out_p10=541)), LiveAlert)

    def test_currently_inside_is_not_out(self) -> None:
        now = _t(15, 0)
        day = _today(now, (9, 0, "entry"), (13, 0, "exit"), (13, 30, "entry"))
        self.assertEqual(day.status, "open")
        self.assertEqual(_decide(day, now), "not_out")

    def test_outside_alert_hours(self) -> None:
        for now in (_t(8, 59), _t(20, 0), _t(21, 30)):
            day = _today(now, (6, 30, "entry"), (7, 0, "exit"))
            self.assertEqual(_decide(day, now), "outside_alert_hours", now)
        late = _today(_t(21, 30), (9, 0, "entry"), (13, 0, "exit"))
        self.assertIsInstance(
            _decide(late, _t(21, 30), _baseline(last_out_p10=1200), alert_end_hour=22), LiveAlert
        )

    def test_alert_hours_may_wrap_midnight(self) -> None:
        self.assertTrue(punch_alerts.in_alert_hours(_t(23, 0), 22, 2))
        self.assertTrue(punch_alerts.in_alert_hours(datetime(2026, 10, 28, 1, 0), 22, 2))
        self.assertFalse(punch_alerts.in_alert_hours(_t(12, 0), 22, 2))

    def test_no_punch_today(self) -> None:
        now = _t(15, 0)
        self.assertEqual(_decide(_today(now), now), "no_punch_today")

    def test_no_baseline(self) -> None:
        now = _t(15, 0)
        day = _today(now, (9, 0, "entry"), (13, 0, "exit"))
        self.assertEqual(live_decision(day, None, now=now, day_start=DAY_START), "no_baseline")
        # Morning-eligible but under ten clean days: still no live limit.
        self.assertEqual(_decide(day, now, Baseline(12, 4)), "no_baseline")

    def test_only_an_orphan_exit_is_no_entry_today(self) -> None:
        now = _t(15, 0)
        day = _today(now, (13, 0, "exit"))
        self.assertEqual(day.status, "incomplete")
        self.assertEqual(_decide(day, now), "no_entry_today")

    def test_out_stretch_runs_from_the_latest_exit(self) -> None:
        now = _t(15, 0)
        # A second out with no in between: out since the later one.
        day = _today(now, (9, 0, "entry"), (11, 0, "exit"), (12, 30, "exit"))
        self.assertEqual(_decide(day, now), LiveAlert(_t(12, 30), 150))

    def test_c3_realtime_exit_counts_as_out(self) -> None:
        now = _t(14, 45)
        rows = [
            {"event_timestamp": _t(9), "trusted_direction": "entry", "device_id": "c3"},
            # Realtime in/out 15 on a granted swipe (event 0): read as an exit.
            {"event_timestamp": _t(13), "trusted_direction": _c3_trusted_direction(15, 0),
             "raw_direction_byte": C3_UNDECODED_BYTE, "device_id": "c3"},
        ]
        punches = [punch_from_row(row, {}) for row in rows]
        day = compute_day(punches, target_minutes=480, now=now, is_today=True)
        self.assertEqual(_decide(day, now), LiveAlert(_t(13), 105))

    def test_door_denied_row_is_never_out(self) -> None:
        now = _t(14, 45)
        denied = {"event_timestamp": _t(13), "trusted_direction": _c3_trusted_direction(15, 24),
                  "raw_direction_byte": C3_UNDECODED_BYTE, "device_id": "c3",
                  "raw_state": "door denied: anti-passback"}
        self.assertIsNone(denied["trusted_direction"])
        rows = [{"event_timestamp": _t(9), "trusted_direction": "entry", "device_id": "c3"}, denied]
        day = compute_day([punch_from_row(r, {}) for r in rows], target_minutes=480,
                          now=now, is_today=True)
        # Alternation reads the direction-less denial as an exit...
        self.assertEqual(day.segments[-1].end, _t(13))
        door_denied = {r["event_timestamp"] for r in rows if punch_alerts.is_door_denied(r)}
        self.assertEqual(door_denied, {_t(13)})
        # ...but it never proves the person left.
        self.assertEqual(_decide(day, now, door_denied=door_denied), "not_out")


class MorningIssuesTest(unittest.TestCase):
    def test_day_ending_on_an_entry_is_one_missing_final_out(self) -> None:
        day = _yesterday((9, 0, "entry"), (13, 0, "exit"), (14, 0, "entry"))
        # compute_day lists the trailing entry in both anomalies and unpaired.
        self.assertEqual(day.anomalies, ("No exit after 14:00",))
        self.assertEqual(day.unpaired, ((_t(14), "entry"),))
        self.assertEqual(morning_issues(day, has_unconfirmed_c3_guess=False),
                         [MorningIssue(MISSING_FINAL_OUT, _t(14))])

    def test_lone_entry_is_missing_final_out(self) -> None:
        day = _yesterday((9, 0, "entry"))
        self.assertEqual(morning_issues(day, has_unconfirmed_c3_guess=False),
                         [MorningIssue(MISSING_FINAL_OUT, _t(9))])

    def test_exit_with_nothing_open_is_out_without_in(self) -> None:
        day = _yesterday((8, 0, "exit"), (9, 0, "entry"), (18, 0, "exit"))
        self.assertEqual(morning_issues(day, has_unconfirmed_c3_guess=False),
                         [MorningIssue(OUT_WITHOUT_IN, _t(8))])
        late = _yesterday((9, 0, "entry"), (17, 0, "exit"), (17, 30, "exit"))
        self.assertEqual(morning_issues(late, has_unconfirmed_c3_guess=False),
                         [MorningIssue(OUT_WITHOUT_IN, _t(17, 30))])

    def test_two_entries_are_a_double_in(self) -> None:
        day = _yesterday((9, 0, "entry"), (10, 0, "entry"), (18, 0, "exit"))
        self.assertEqual(morning_issues(day, has_unconfirmed_c3_guess=False),
                         [MorningIssue(DOUBLE_IN, _t(9), _t(10))])

    def test_double_in_then_no_exit(self) -> None:
        day = _yesterday((9, 0, "entry"), (17, 0, "exit"), (17, 10, "entry"), (17, 20, "entry"))
        self.assertEqual(morning_issues(day, has_unconfirmed_c3_guess=False), [
            MorningIssue(DOUBLE_IN, _t(17, 10), _t(17, 20)),
            MorningIssue(MISSING_FINAL_OUT, _t(17, 20)),
        ])

    def test_unpaired_swipes_inside_a_gap_report_one_unclear_gap(self) -> None:
        doubled = _yesterday((9, 0, "entry"), (12, 0, "exit"), (13, 0, "entry"),
                             (13, 30, "entry"), (18, 0, "exit"))
        self.assertEqual([g.kind for g in doubled.gaps], ["unclear"])
        self.assertEqual(morning_issues(doubled, has_unconfirmed_c3_guess=False),
                         [MorningIssue(UNCLEAR_GAP, _t(12), _t(13, 30))])
        orphan = _yesterday((9, 0, "entry"), (12, 0, "exit"), (12, 30, "exit"),
                            (13, 0, "entry"), (18, 0, "exit"))
        self.assertEqual(morning_issues(orphan, has_unconfirmed_c3_guess=False),
                         [MorningIssue(UNCLEAR_GAP, _t(12), _t(13))])

    def test_several_issues_come_in_time_order(self) -> None:
        day = _yesterday((8, 0, "exit"), (9, 0, "entry"), (12, 0, "exit"), (12, 30, "exit"),
                         (13, 0, "entry"), (17, 0, "exit"), (17, 30, "entry"))
        self.assertEqual(morning_issues(day, has_unconfirmed_c3_guess=False), [
            MorningIssue(OUT_WITHOUT_IN, _t(8)),
            MorningIssue(UNCLEAR_GAP, _t(12), _t(13)),
            MorningIssue(MISSING_FINAL_OUT, _t(17, 30)),
        ])

    def test_clean_and_empty_days_report_nothing(self) -> None:
        clean = _yesterday((9, 0, "entry"), (13, 0, "exit"), (13, 40, "entry"), (18, 0, "exit"))
        self.assertEqual(morning_issues(clean, has_unconfirmed_c3_guess=False), [])
        self.assertEqual(morning_issues(_yesterday(), has_unconfirmed_c3_guess=False), [])

    def test_unconfirmed_c3_guess_holds(self) -> None:
        day = _yesterday((9, 0, "entry"))
        self.assertEqual(morning_issues(day, has_unconfirmed_c3_guess=True), punch_alerts.HOLD)
        self.assertEqual(punch_alerts.HOLD, "hold")


class C3GuessTest(unittest.TestCase):
    """Mirrors db._FRAPPE_HELD_SQL: byte 15 with a realtime_fallback source."""

    def test_realtime_byte_15_is_an_unconfirmed_guess(self) -> None:
        source = json.dumps({"source": "realtime_fallback"})
        self.assertTrue(punch_alerts.is_unconfirmed_c3_guess(
            {"raw_direction_byte": 15, "raw_json": source}))
        self.assertTrue(punch_alerts.is_unconfirmed_c3_guess(
            {"raw_direction_byte": 15, "raw_json": {"source": "realtime_fallback"}}))

    def test_confirmed_or_other_rows_are_not(self) -> None:
        realtime = json.dumps({"source": "realtime_fallback"})
        for row in (
            {"raw_direction_byte": 1, "raw_json": realtime},  # a history read replaced the byte
            {"raw_direction_byte": 15, "raw_json": json.dumps({"source": "history"})},
            {"raw_direction_byte": 15, "raw_json": None},
            {"raw_direction_byte": 15, "raw_json": "not json"},
            {"raw_direction_byte": None, "raw_json": realtime},
            {"raw_json": realtime},
        ):
            self.assertFalse(punch_alerts.is_unconfirmed_c3_guess(row), row)

    def test_any_guess_in_the_day(self) -> None:
        realtime = json.dumps({"source": "realtime_fallback"})
        rows = [{"raw_direction_byte": 0, "raw_json": realtime},
                {"raw_direction_byte": 15, "raw_json": realtime}]
        self.assertTrue(punch_alerts.has_unconfirmed_c3_guess(rows))
        self.assertFalse(punch_alerts.has_unconfirmed_c3_guess(rows[:1]))
        self.assertFalse(punch_alerts.has_unconfirmed_c3_guess([]))

    def test_door_denied_note(self) -> None:
        self.assertTrue(punch_alerts.is_door_denied({"raw_state": "door denied: interlock"}))
        self.assertFalse(punch_alerts.is_door_denied({"raw_state": "interval too short"}))
        self.assertFalse(punch_alerts.is_door_denied({"raw_state": None}))


if __name__ == "__main__":
    unittest.main()
