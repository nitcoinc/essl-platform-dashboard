import unittest
from datetime import datetime

from essl_hub import day_story
from essl_hub.hours import DayResult, Segment

D = datetime(2026, 10, 5)


def _at(h, m=0):
    return D.replace(hour=h, minute=m)


def _result(status, effective, segments, anomalies=()):
    return DayResult(status=status, gross_minutes=None, effective_minutes=effective,
                     break_minutes=0, segments=tuple(segments), direction_source="raw_byte",
                     anomalies=tuple(anomalies))


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

    def test_flagged_met_day_reads_as_met_with_review_note(self) -> None:
        r = _result("met", 480, [Segment(_at(9), _at(17)), Segment(_at(18), None)], ["No exit after 18:00"])
        h = day_story.headline(r, 480, None, is_today=False, weekend=False)
        self.assertEqual((h.text, h.tone), ("Target met at 17:00 · needs review", "ok"))
        self.assertNotIn("Missing exit", h.text)

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


class SecondsTest(unittest.TestCase):
    def test_log_stretch_seconds_sum_to_effective_seconds(self) -> None:
        from essl_hub.hours import Punch, compute_day

        punches = [Punch(_at(9).replace(second=12), "entry"), Punch(_at(12, 40).replace(second=59), "exit"),
                   Punch(_at(13, 15).replace(second=3), "entry")]
        now = _at(16, 2).replace(second=41)
        result = compute_day(punches, target_minutes=480, now=now, is_today=True)
        items = day_story.log_items(result, now)
        ins = [i.seconds for i in items if i.kind == "in"]
        self.assertEqual(sum(ins), result.effective_seconds)
        self.assertEqual([i.seconds for i in items if i.kind == "break"], [34 * 60 + 4])

    def test_target_reached_at_uses_seconds(self) -> None:
        r = _result("met", 480, [Segment(_at(9).replace(second=30), _at(17, 30))])
        self.assertEqual(day_story.target_reached_at(r, 480, None), _at(17).replace(second=30))


def _scored(status, effective_seconds, segments, anomalies=()):
    return DayResult(status=status, gross_minutes=None, effective_minutes=effective_seconds // 60,
                     break_minutes=0, segments=tuple(segments), direction_source="raw_byte",
                     anomalies=tuple(anomalies), effective_seconds=effective_seconds)


class TodayStateTest(unittest.TestCase):
    """U11: the Today card's label, data-state and left/over line."""

    def test_left_rounds_up_and_over_rounds_down(self) -> None:
        self.assertEqual(day_story.left_text(28800 - 1019, 28800), "17m left")
        self.assertEqual(day_story.left_text(28800 - 1021, 28800), "18m left")
        self.assertEqual(day_story.left_text(7205, 28800), "6h 00m left")
        self.assertEqual(day_story.left_text(28800, 28800), "+0m over")
        self.assertEqual(day_story.left_text(28800 + 779, 28800), "+12m over")
        self.assertEqual(day_story.left_text(28800 + 3900, 28800), "+1h 05m over")

    def test_labels_and_states_follow_the_state_table(self) -> None:
        cases = [
            (_scored("open", 7200, [Segment(_at(9), None)]), "Inside", "inside"),
            (_scored("open", 29520, [Segment(_at(9), None)]), "Inside · Target met", "inside"),
            (_scored("short", 27780, [Segment(_at(10), _at(17, 43))]), "Out since 17:43:00", "out"),
            (_scored("met", 29520, [Segment(_at(9), _at(17, 12))]), "Out · Target met", "met"),
            (_scored("incomplete", 0, []), "Incomplete: no exit yet", "out"),
        ]
        for result, label, state in cases:
            with self.subTest(label=label):
                self.assertEqual(day_story.today_label(result, 480), label)
                self.assertEqual(day_story.live_state(result, 480), state)

    def test_review_label_names_the_gap(self) -> None:
        r = _scored("open", 3600, [Segment(_at(14, 33), None)], ["No exit after 14:33"])
        self.assertEqual(day_story.review_label(r), "Needs review: no exit after 14:33")
        self.assertIsNone(day_story.review_label(_scored("met", 29000, [])))



class PunchFlagsTest(unittest.TestCase):
    def test_interval_too_short_chip(self) -> None:
        self.assertEqual(day_story.punch_flags("event 20 · in/out 1 · interval too short"), ("interval too short",))
        self.assertEqual(day_story.punch_flags("event 0 · in/out 1"), ())
        self.assertEqual(day_story.punch_flags(None), ())
        # KTD5: door-denied swipes captured under "Wait for door close" off.
        self.assertEqual(day_story.punch_flags("event 25 · in/out 1 · door denied: interlock"),
                         ("door denied (interlock)",))
        self.assertEqual(day_story.punch_flags("24 · 15 · door denied: anti-passback"),
                         ("door denied (anti-passback)",))

    def test_same_person_event_0_then_20_counts_once(self) -> None:
        from essl_hub.hours import Punch, compute_day

        second = Punch(_at(13).replace(second=1))
        r = compute_day([Punch(_at(9), "entry"), Punch(_at(13), "exit"), second, Punch(_at(14), "entry"),
                         Punch(_at(18), "exit")], target_minutes=480)
        self.assertEqual(r.collapsed, (second,))
        self.assertEqual(r.break_seconds, 3600)


if __name__ == "__main__":
    unittest.main()


class BreakViewTest(unittest.TestCase):
    """U11/KTD10: summary figures, bar segments, break rows, raw punches."""

    def _day(self, *swipes, now=None):
        from essl_hub.hours import Punch, compute_day
        punches = [Punch(t, d) for t, d in swipes]
        return compute_day(punches, target_minutes=480, now=now, is_today=now is not None)

    @staticmethod
    def _rows(*swipes, states=None):
        states = states or {}
        return [{"event_timestamp": t, "trusted_direction": d, "raw_state": states.get(t)} for t, d in swipes]

    def test_three_breaks_number_the_bar_and_sum_to_the_total(self) -> None:
        swipes = [(_at(9), "entry"), (_at(11), "exit"), (_at(11, 20), "entry"), (_at(13), "exit"),
                  (_at(13, 40), "entry"), (_at(15), "exit"), (_at(15, 20), "entry"), (_at(16, 32), "exit")]
        view = day_story.break_view(self._day(*swipes), None, self._rows(*swipes))
        self.assertEqual(view.break_seconds, 4800)
        self.assertEqual(view.break_count, 3)
        self.assertEqual([s.number for s in view.bar if s.kind == "break"], [1, 2, 3])
        self.assertEqual(view.bar_label, "Inside 6:12, 3 breaks totalling 1:20: break 1 11:00 to 11:20 (20m); "
                                          "break 2 13:00 to 13:40 (40m); break 3 15:00 to 15:20 (20m)")
        self.assertEqual([m.label for m in view.marks if m.label][0], "09:00")
        self.assertEqual(view.marks[-1].label, "16:32")
        self.assertAlmostEqual(sum(s.percent for s in view.bar), 100, places=1)
        self.assertEqual(sum(g.seconds for g in view.gaps if g.kind == "break"), view.break_seconds)
        self.assertEqual([p.direction for p in view.punches][:2], ["entry", "exit"])

    def test_clean_day_is_one_solid_segment_with_no_breaks(self) -> None:
        swipes = [(_at(9), "entry"), (_at(17, 30), "exit")]
        view = day_story.break_view(self._day(*swipes), None, self._rows(*swipes))
        self.assertEqual([s.kind for s in view.bar], ["inside"])
        self.assertEqual(view.bar_label, "Inside 8:30, no breaks")
        self.assertEqual(view.gaps, ())

    def test_unclear_gap_is_its_own_kind_and_excluded_from_breaks(self) -> None:
        swipes = [(_at(9), "entry"), (_at(12), "exit"), (_at(13), "exit"), (_at(14), "entry"), (_at(18), "exit")]
        result = self._day(*swipes)
        view = day_story.break_view(result, None, self._rows(*swipes))
        kinds = [s.kind for s in view.bar]
        self.assertIn("unclear", kinds)
        self.assertEqual(view.break_seconds, result.break_seconds)
        self.assertGreater(view.unclear_seconds, 0)
        self.assertIn("unclear", view.bar_label)
        self.assertIn("unpaired", [f for p in view.punches for f in p.flags])

    def test_filler_swipe_shows_inferred_in_and_short_break_merges_labels(self) -> None:
        swipes = [(_at(9), "entry"), (_at(12), "exit"), (_at(12).replace(second=19), None), (_at(13), "exit"),
                  (_at(13, 30), "entry"), (_at(18), "exit")]
        view = day_story.break_view(self._day(*swipes), None, self._rows(*swipes))
        filler = view.punches[2]
        self.assertEqual((filler.direction, filler.flags), ("entry", ("inferred",)))
        self.assertEqual(view.break_count, 2)
        labels = [m.label for m in view.marks if m.label]
        self.assertEqual(labels[0], "09:00")
        self.assertEqual(labels[-1], "18:00")
        self.assertEqual(len([lab for lab in labels if lab == "12:00"]), 1)  # 19s break: one label
        self.assertIn("break 1 12:00 to 12:00 (19s)", view.bar_label)

    def test_open_day_bar_ends_at_now(self) -> None:
        swipes = [(_at(9), "entry"), (_at(12), "exit"), (_at(12, 30), "entry")]
        view = day_story.break_view(self._day(*swipes, now=_at(14)), _at(14), self._rows(*swipes))
        self.assertTrue(view.bar[-1].open)
        self.assertEqual(view.bar[-1].end, _at(14))
        self.assertEqual(view.bar_end, _at(14))

    def test_duplicates_are_not_counted_and_interval_flag_is_shown(self) -> None:
        dup = D.replace(hour=9, second=20)
        swipes = [(_at(9), "entry"), (dup, "entry"), (_at(17), "exit")]
        rows = self._rows(*swipes, states={_at(17): "event 20 · in/out 1 · interval too short"})
        view = day_story.break_view(self._day(*swipes), None, rows)
        self.assertEqual(len(view.punches), 3)
        self.assertEqual(view.punches[1].flags, ("not counted",))
        self.assertEqual(view.punches[2].flags, ("interval too short",))

    def test_not_counted_swipe_states_the_swipe_it_repeats(self) -> None:
        # R6: X990 (no direction), window 60: 09:00:20 repeats the 09:00:00 In.
        from essl_hub.hours import Punch, compute_day
        swipes = [(_at(9), None), (D.replace(hour=9, second=20), None), (_at(18), None)]
        r = compute_day([Punch(t, d, 60) for t, d in swipes], target_minutes=480)
        self.assertEqual(r.collapsed_reasons[0].gap_seconds, 20)
        view = day_story.break_view(r, None, self._rows(*swipes))
        self.assertEqual([(p.flags, p.note) for p in view.punches], [
            ((), ""), (("not counted",), "same as In 09:00:00, 20s earlier"), ((), "")])

    def test_window_0_counts_every_swipe_with_no_note(self) -> None:
        from essl_hub.hours import Punch, compute_day
        swipes = [(_at(9), None), (D.replace(hour=9, second=20), None), (_at(18), None)]
        r = compute_day([Punch(t, d, 0) for t, d in swipes], target_minutes=480)
        view = day_story.break_view(r, None, self._rows(*swipes))
        self.assertEqual([p.direction for p in view.punches], ["entry", "exit", "entry"])
        self.assertEqual([p.note for p in view.punches], ["", "", ""])
        self.assertNotIn("not counted", [f for p in view.punches for f in p.flags])

    def test_reswipe_note_names_the_out_it_repeats(self) -> None:
        again = D.replace(hour=13, second=12)
        swipes = [(_at(9), "entry"), (_at(13), "exit"), (again, "exit"), (_at(14), "entry"), (_at(18), "exit")]
        view = day_story.break_view(self._day(*swipes), None, self._rows(*swipes))
        self.assertEqual(view.punches[2].note, "same as Out 13:00:00, 12s earlier")

    def test_corrected_swipes_carry_text_chips_and_the_bar_names_them(self) -> None:
        # KTD4: rows as hours.apply_corrections returns them.
        dup = D.replace(hour=9, minute=5)
        swipes = [(_at(9), "entry"), (_at(13), "exit"), (_at(13, 40), "entry"), (_at(18), "exit")]
        rows = self._rows(*swipes[:1], (dup, "entry"), *swipes[1:])
        for row, mark in zip(rows, (None, "ignored", None, "added", "direction_fixed")):
            row["correction"] = mark
        view = day_story.break_view(self._day(*swipes), None, rows)
        self.assertEqual([(p.direction, p.flags) for p in view.punches], [
            ("entry", ()), ("entry", ("ignored",)), ("exit", ()),
            ("entry", ("added by HR",)), ("exit", ("direction fixed",))])
        self.assertTrue(view.bar_label.endswith(", 1 punch added by HR, 1 punch ignored"))

    def test_day_level_fix_chip_follows_the_latest_request(self) -> None:
        swipes = [(_at(9), "entry"), (_at(18), "exit")]
        result, rows = self._day(*swipes), self._rows(*swipes)
        cases = (((), ()), (["pending"], ("Fix pending",)), (["rejected"], ("Fix rejected",)),
                 (["rejected", "pending"], ("Fix pending",)), (["pending", "approved"], ()),
                 (["closed_correct"], ()))
        for statuses, chips in cases:
            with self.subTest(statuses=statuses):
                self.assertEqual(day_story.break_view(result, None, rows, statuses).fix_flags, chips)

