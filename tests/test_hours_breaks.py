"""U10 break math: characterization of clean days, then duplicates, unpaired
swipes and unclear gaps (KTD9)."""

from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta

from essl_hub import day_story
from essl_hub.hours import (
    Collapsed, Punch, Segment, apply_corrections, compute_day, counted_punches,
)


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
        # 59 s: exactly 60 s counts since KTD3 made the window strict.
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(9, 0, 59)), Punch(_t(13), "exit"),
                         Punch(_t(14), "entry"), Punch(_t(14, 0, 59)), Punch(_t(18), "exit")], target_minutes=480)
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


class DefaultWindowCharacterizationTest(unittest.TestCase):
    """Pinned at the default 60 s window before U2 made it per device (KTD3)."""

    def test_direction_less_59s_collapses(self) -> None:
        dup = Punch(_t(9, 0, 59))
        r = compute_day([Punch(_t(9)), dup, Punch(_t(18))], target_minutes=480)
        self.assertEqual(r.collapsed, (dup,))
        self.assertEqual(r.segments, (Segment(_t(9), _t(18)),))

    def test_direction_less_exactly_60s_counts(self) -> None:
        # Collapsed before U2 (gap <= window); KTD3 makes the window strict.
        r = compute_day([Punch(_t(9)), Punch(_t(9, 1)), Punch(_t(18))], target_minutes=480)
        self.assertEqual(r.collapsed, ())
        self.assertEqual(r.segments[0], Segment(_t(9), _t(9, 1)))

    def test_same_direction_entry_30s_collapses(self) -> None:
        again = Punch(_t(9, 0, 30), "entry")
        r = compute_day([Punch(_t(9), "entry"), again, Punch(_t(18), "exit")], target_minutes=480)
        self.assertEqual((r.collapsed, r.unpaired), ((again,), ()))
        self.assertEqual(r.segments, (Segment(_t(9), _t(18)),))

    def test_known_entry_then_exit_5s_both_count(self) -> None:
        r = compute_day([Punch(_t(9), "entry"), Punch(_t(9, 0, 5), "exit")], target_minutes=480)
        self.assertEqual(r.collapsed, ())
        self.assertEqual(r.segments, (Segment(_t(9), _t(9, 0, 5)),))


class PerDeviceWindowTest(unittest.TestCase):
    """KTD3: each punch is judged by its own device's quick-swipe window."""

    def test_x990_window_0_counts_every_swipe(self) -> None:
        r = compute_day([Punch(_t(9), None, 0), Punch(_t(9, 0, 20), None, 0), Punch(_t(18), None, 0)],
                        target_minutes=480)
        self.assertEqual((r.collapsed, r.collapsed_reasons), ((), ()))
        self.assertEqual(r.segments[0], Segment(_t(9), _t(9, 0, 20)))
        self.assertEqual(r.unpaired, ((_t(18), "entry"),))

    def test_x990_window_60_collapses_with_reason(self) -> None:
        dup = Punch(_t(9, 0, 20), None, 60)
        r = compute_day([Punch(_t(9), None, 60), dup, Punch(_t(18), None, 60)], target_minutes=480)
        self.assertEqual(r.collapsed, (dup,))
        self.assertEqual(r.collapsed_reasons, (Collapsed(dup, _t(9), "entry", 20),))
        self.assertEqual(r.segments, (Segment(_t(9), _t(18)),))

    def test_window_0_same_second_both_count(self) -> None:
        r = compute_day([Punch(_t(9), None, 0), Punch(_t(9), None, 0)], target_minutes=480)
        self.assertEqual(r.collapsed, ())
        self.assertEqual(r.segments, (Segment(_t(9), _t(9)),))

    def test_mixed_day_judges_by_current_punchs_window(self) -> None:
        # C3 entry (window 10), X990 swipe 30 s later (window 60): judged by 60.
        dup = Punch(_t(9, 0, 30), None, 60)
        r = compute_day([Punch(_t(9), "entry", 10), dup, Punch(_t(18), "exit", 10)], target_minutes=480)
        self.assertEqual(r.collapsed, (dup,))
        self.assertEqual(r.collapsed_reasons[0].gap_seconds, 30)
        self.assertEqual(r.segments, (Segment(_t(9), _t(18)),))

    def test_known_entry_exit_5s_window_60_both_count(self) -> None:
        r = compute_day([Punch(_t(9), "entry", 60), Punch(_t(9, 0, 5), "exit", 60)], target_minutes=480)
        self.assertEqual(r.collapsed, ())
        self.assertEqual(r.segments, (Segment(_t(9), _t(9, 0, 5)),))

    def test_entry_entry_30s_window_60_collapses_second(self) -> None:
        again = Punch(_t(9, 0, 30), "entry", 60)
        r = compute_day([Punch(_t(9), "entry", 60), again, Punch(_t(18), "exit", 60)], target_minutes=480)
        self.assertEqual(r.collapsed_reasons, (Collapsed(again, _t(9), "entry", 30),))
        self.assertEqual((r.segments, r.unpaired), ((Segment(_t(9), _t(18)),), ()))

    def test_entry_entry_30s_window_10_counts_both(self) -> None:
        r = compute_day([Punch(_t(9), "entry", 10), Punch(_t(9, 0, 30), "entry", 10),
                         Punch(_t(18), "exit", 10)], target_minutes=480)
        self.assertEqual(r.collapsed, ())
        # The first entry is superseded by the second and shown unpaired.
        self.assertEqual(r.segments, (Segment(_t(9, 0, 30), _t(18)),))
        self.assertEqual(r.unpaired, ((_t(9), "entry"),))


class CorrectionOverlayTest(unittest.TestCase):
    """U2/KTD4: approved fixes merge into punch rows before compute_day."""

    WINDOWS = {"x990": 60, "c3": 0}
    MAIN_IDS = {1: "7"}

    @staticmethod
    def _rows(*swipes, device="x990", owner=1):
        return [{"fingerprint": f"fp-{i}", "employee_id": owner, "device_user_id": "7", "device_id": device,
                 "event_timestamp": t, "trusted_direction": d, "raw_state": None}
                for i, (t, d) in enumerate(swipes)]

    @staticmethod
    def _fix(kind, cid=1, fingerprint=None, at=None, direction=None, owner=1):
        return {"id": cid, "employee_id": owner, "kind": kind, "fingerprint": fingerprint,
                "event_timestamp": at, "direction": direction}

    def _score(self, rows, corrections=()):
        merged = apply_corrections(rows, list(corrections), self.MAIN_IDS)
        return compute_day(counted_punches(merged, self.WINDOWS), target_minutes=480)

    def test_added_in_turns_x990_long_break_into_two_stretches(self) -> None:
        rows = self._rows((_t(9), None), (_t(13), None), (_t(18), None))
        before = self._score(rows)
        self.assertEqual(before.segments, (Segment(_t(9), _t(13)), Segment(_t(18), None)))
        self.assertEqual(before.anomalies, ("No exit after 18:00",))
        after = self._score(rows, [self._fix("add", at=_t(13, 40), direction="entry")])
        self.assertEqual(after.segments, (Segment(_t(9), _t(13)), Segment(_t(13, 40), _t(18))))
        self.assertEqual((after.status, after.anomalies, after.break_seconds), ("met", (), 2400))

    def test_added_row_carries_the_owners_main_id_and_no_device(self) -> None:
        [added] = apply_corrections([], [self._fix("add", cid=5, at=_t(13, 40), direction="entry")],
                                    self.MAIN_IDS)
        self.assertEqual(
            {k: added[k] for k in ("device_user_id", "employee_id", "fingerprint", "device_id", "site",
                                   "trusted_direction", "correction")},
            {"device_user_id": "7", "employee_id": 1, "fingerprint": "correction:5", "device_id": None,
             "site": None, "trusted_direction": "entry", "correction": "added"})
        # An owner outside the read (no main ID known) adds nothing.
        self.assertEqual(apply_corrections([], [self._fix("add", owner=2, at=_t(9), direction="entry")],
                                           self.MAIN_IDS), [])

    def test_set_direction_makes_a_wrong_c3_exit_an_entry(self) -> None:
        rows = self._rows((_t(9), "entry"), (_t(13), "exit"), (_t(13, 40), "exit"), (_t(18), "exit"),
                          device="c3")
        self.assertEqual(self._score(rows).segments, (Segment(_t(9), _t(13)),))
        fixed = self._score(rows, [self._fix("set_direction", fingerprint="fp-2", direction="entry")])
        self.assertEqual(fixed.segments, (Segment(_t(9), _t(13)), Segment(_t(13, 40), _t(18))))
        self.assertEqual(fixed.anomalies, ())

    def test_ignored_duplicate_leaves_hours_but_stays_in_the_rows(self) -> None:
        rows = self._rows((_t(9), "entry"), (_t(9, 5), "entry"), (_t(18), "exit"), device="c3")
        self.assertEqual(self._score(rows).anomalies, ("No exit after 09:00",))
        merged = apply_corrections(rows, [self._fix("ignore", fingerprint="fp-0")], self.MAIN_IDS)
        self.assertEqual([r["correction"] for r in merged], ["ignored", None, None])
        result = compute_day(counted_punches(merged, self.WINDOWS), target_minutes=480)
        self.assertEqual((result.segments, result.anomalies), ((Segment(_t(9, 5), _t(18)),), ()))

    def test_added_punch_30s_after_same_direction_punch_is_not_collapsed(self) -> None:
        rows = self._rows((_t(9), "entry"), (_t(13), "exit"), device="x990")
        result = self._score(rows, [self._fix("add", at=_t(13, 0, 30), direction="exit")])
        self.assertEqual(result.collapsed, ())
        self.assertEqual(result.unpaired, ((_t(13, 0, 30), "exit"),))
        # The same swipe from the device itself would collapse into the 13:00 Out.
        real = self._score(self._rows((_t(9), "entry"), (_t(13), "exit"), (_t(13, 0, 30), "exit")))
        self.assertEqual(len(real.collapsed), 1)

    def test_corrected_rows_carry_who_fixed_them_and_why(self) -> None:
        rows = self._rows((_t(9), "entry"), (_t(13), "exit"), (_t(18), "exit"), device="c3")
        by_hari = {**self._fix("set_direction", cid=4, fingerprint="fp-1", direction="entry"),
                   "created_by_name": "Hari", "created_by_role": "hr", "created_at": _t(20),
                   "reason": "Badge log", "path": "request"}
        by_adam = {**self._fix("ignore", cid=7, fingerprint="fp-1"),
                   "created_by_name": "Adam", "created_by_role": "admin", "created_at": _t(21),
                   "reason": "Duplicate", "path": "direct"}
        added = {**self._fix("add", cid=9, at=_t(13, 40), direction="entry"),
                 "created_by_name": "Adam", "created_by_role": "admin", "created_at": _t(21, 5),
                 "reason": "Forgot to tap", "path": "direct"}
        merged = apply_corrections(rows, [by_hari, by_adam, added], self.MAIN_IDS)
        info = {r["fingerprint"]: r["correction_info"] for r in merged}
        self.assertIsNone(info["fp-0"])
        # The later correction on one punch names the fixer shown.
        self.assertEqual(info["fp-1"], {"id": 7, "by": "Adam", "role": "admin", "at": _t(21),
                                        "reason": "Duplicate", "path": "direct"})
        self.assertEqual(info["correction:9"], {"id": 9, "by": "Adam", "role": "admin", "at": _t(21, 5),
                                                "reason": "Forgot to tap", "path": "direct"})
        # A correction row from before the audit trail still merges.
        [legacy] = apply_corrections(rows[:1], [self._fix("ignore", fingerprint="fp-0")], self.MAIN_IDS)
        self.assertEqual(legacy["correction_info"]["by"], "before audit trail")

    def test_the_fixer_shown_matches_the_correction_that_decided_the_final_state(self) -> None:
        rows = self._rows((_t(9), "entry"), (_t(13), "exit"), device="c3")
        def fix(kind, cid, name, at):
            return {**self._fix(kind, cid=cid, fingerprint="fp-1", direction="entry" if kind == "set_direction" else None),
                    "created_by_name": name, "created_by_role": "admin", "created_at": at, "reason": name, "path": "direct"}
        ignored_then_direction = apply_corrections(
            rows, [fix("ignore", 1, "Ada", _t(20)), fix("set_direction", 2, "Bea", _t(21))], self.MAIN_IDS)
        row = ignored_then_direction[1]
        self.assertEqual((row["correction"], row["correction_info"]["by"]), ("ignored", "Ada"))
        direction_then_ignored = apply_corrections(
            rows, [fix("set_direction", 1, "Bea", _t(20)), fix("ignore", 2, "Ada", _t(21))], self.MAIN_IDS)
        self.assertEqual(direction_then_ignored[1]["correction_info"]["by"], "Ada")
        twice = apply_corrections(
            rows, [fix("set_direction", 1, "Bea", _t(20)), fix("set_direction", 2, "Cy", _t(21))], self.MAIN_IDS)
        self.assertEqual(twice[1]["correction_info"]["by"], "Cy")

    def test_a_direction_fix_keeps_what_the_device_said(self) -> None:
        rows = self._rows((_t(9), "entry"), (_t(13), "exit"), device="c3")
        merged = apply_corrections(
            rows, [self._fix("set_direction", fingerprint="fp-1", direction="entry")], self.MAIN_IDS)
        self.assertEqual([(r["trusted_direction"], r["device_direction"]) for r in merged],
                         [("entry", "entry"), ("entry", "exit")])
        self.assertEqual(self._rows((_t(9), None))[0].get("device_direction", "absent"), "absent")

    def test_dangling_or_foreign_fingerprint_is_inert(self) -> None:
        rows = self._rows((_t(9), "entry"), (_t(18), "exit"), device="c3")
        corrections = [self._fix("ignore", fingerprint="fp-gone"),
                       self._fix("set_direction", cid=2, fingerprint="fp-gone", direction="exit"),
                       # The punch's ID now belongs to someone else: their fix does not follow it.
                       self._fix("ignore", cid=3, fingerprint="fp-0", owner=2)]
        merged = apply_corrections(rows, corrections, self.MAIN_IDS)
        self.assertEqual([(r["trusted_direction"], r["correction"]) for r in merged],
                         [("entry", None), ("exit", None)])
        self.assertEqual(rows[0].get("correction", "untouched"), "untouched")


if __name__ == "__main__":
    unittest.main()


class LiveLogUnknownDirectionTest(unittest.TestCase):
    """v11: the reported Gachibowli day of 2026-10-28. 17:51 and 19:53 were
    live-log byte 15 (no direction); 19:55, 20:07 and 20:22 byte 0 (entry)."""

    def _day(self):
        return compute_day([
            Punch(_t(17, 51), None, 10), Punch(_t(19, 53), None, 10), Punch(_t(19, 55), "entry", 10),
            Punch(_t(20, 7), "entry", 10), Punch(_t(20, 22), "entry", 10),
        ], target_minutes=480)

    def test_arrival_counts_in_and_the_swipe_before_the_next_entry_counts_out(self) -> None:
        r = self._day()
        self.assertEqual(r.segments[0], Segment(_t(17, 51), _t(19, 53)))
        self.assertEqual(r.inferred, (_t(19, 53),))
        self.assertEqual(r.effective_seconds, 2 * 3600 + 2 * 60)

    def test_back_to_back_live_entries_stay_unpaired_until_history_confirms_them(self) -> None:
        # Known limit (plan, System-Wide Impact): only the history read can
        # say which of 19:55, 20:07 and 20:22 was really a leaving.
        r = self._day()
        self.assertEqual(r.unpaired, ((_t(19, 55), "entry"), (_t(20, 7), "entry"), (_t(20, 22), "entry")))
        self.assertTrue(r.anomalies)
