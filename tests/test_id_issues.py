import unittest
from datetime import datetime

from essl_hub.id_issues import find_issues, names_agree


def _emp(i, uid, name):
    return {"id": i, "device_user_id": uid, "name": name}


def _seen(uid, name, n=3):
    return {"device_user_id": uid, "device_name": name, "punch_count": n, "last_seen": datetime(2026, 10, 9), "sites": "Uppal"}


class NamesAgreeTest(unittest.TestCase):
    def test_reordered_initialled_and_labelled_names_agree(self) -> None:
        self.assertTrue(names_agree("K Arun Prakash", "Arun Prakash"))
        self.assertTrue(names_agree("Gopal R (office boy)", "Rao Gopala"))
        self.assertTrue(names_agree("E.Manohar", "Manohar"))
        self.assertTrue(names_agree("", "Anyone"))

    def test_different_people_disagree(self) -> None:
        self.assertFalse(names_agree("Mohan Das", "Temporary"))
        self.assertFalse(names_agree("Ravi Kishore", "Sampath"))


class FindIssuesTest(unittest.TestCase):
    def test_each_kind(self) -> None:
        summary = [_seen("NP0231", "New Joiner"), _seen("NP0009", "K Arun Prakash"), _seen("NP0002", "Mohan Das"), _seen("C3-2", "Temporary")]
        employees = [_emp(1, "NP0009", "Arun Prakash"), _emp(2, "NP0002", "Ravi"), _emp(3, "admin@x.com", "Admin"), _emp(4, "NP0500", "Ghost")]
        kinds = {(i.kind, i.device_user_id) for i in find_issues(summary, employees)}
        self.assertEqual(kinds, {("unknown", "NP0231"), ("name", "NP0002"), ("shared", "C3-2"), ("placeholder", "admin@x.com"), ("never", "NP0500")})

    def test_dismissed_name_mismatch_is_hidden(self) -> None:
        issues = find_issues([_seen("NP0002", "Chandra")], [_emp(2, "NP0002", "Ravi")], {"name:NP0002:chandra"})
        self.assertEqual(issues, [])

    def test_same_person_dismissal_returns_when_device_name_changes(self) -> None:
        dismissed = {i.key for i in find_issues([_seen("NP0002", "Chandra")], [_emp(2, "NP0002", "Ravi")])}
        self.assertEqual(find_issues([_seen("NP0002", "Chandra")], [_emp(2, "NP0002", "Ravi")], dismissed), [])
        again = find_issues([_seen("NP0002", "Mohan Das")], [_emp(2, "NP0002", "Ravi")], dismissed)
        self.assertEqual([i.kind for i in again], ["name"])

    def test_never_punched_dismissal_hides_it(self) -> None:
        self.assertEqual(find_issues([], [_emp(4, "NP0500", "Ghost")], {"never:NP0500"}), [])


    def test_punches_only_under_extra_id_are_not_never_punched(self) -> None:
        asha = {**_emp(1, "128", "Asha"), "device_ids": ["128", "NP0128"]}
        self.assertEqual(find_issues([_seen("NP0128", "Asha")], [asha]), [])


class PrimaryFixTest(unittest.TestCase):
    def _fixes(self, summary, employees):
        from essl_hub.id_issues import id_twins, primary_fix, suggestions

        issues = find_issues(summary, employees)
        pairs, twins = suggestions(issues), id_twins(issues)
        return {i.key: primary_fix(i, pairs, twins) for i in issues}, {i.key: i for i in issues}

    def test_unknown_with_matching_roster_name_links_and_states_impact(self) -> None:
        summary = [{**_seen("NP0231", "Sita R", n=42), "first_seen": datetime(2026, 10, 3)}]
        fixes, issues = self._fixes(summary, [_emp(7, "sita@example.com", "Sita Rao")])
        fix = fixes["unknown:NP0231"]
        self.assertEqual((fix.action, fix.label, fix.employee_id), ("link", "Link to Sita Rao", 7))
        self.assertEqual(issues["unknown:NP0231"].impact, "42 punches since 3 Oct count for nobody.")

    def test_id_format_twin_suggests_change_id(self) -> None:
        fixes, _ = self._fixes([_seen("NP0128", "Somebody Else")], [_emp(5, "128", "Kiran")])
        self.assertEqual((fixes["unknown:NP0128"].label, fixes["unknown:NP0128"].employee_id), ("Change ID to NP0128", 5))
        self.assertEqual((fixes["never:128"].label, fixes["never:128"].device_user_id), ("Change ID to NP0128", "NP0128"))

    def test_unknown_without_match_adds_a_person(self) -> None:
        fixes, _ = self._fixes([_seen("NP0900", "Zed")], [])
        self.assertEqual(fixes["unknown:NP0900"].action, "add")




class CanonicalTwinTest(unittest.TestCase):
    def _fixes(self, summary, employees):
        from essl_hub.id_issues import id_twins, primary_fix, suggestions

        issues = find_issues(summary, employees)
        pairs, twins = suggestions(issues), id_twins(issues, employees)
        return {i.key: primary_fix(i, pairs, twins) for i in issues}

    def test_twin_of_a_punching_person_adds_another_device_id(self) -> None:
        fixes = self._fixes([_seen("NP128", "Device"), _seen("NP0128", "Kiran")], [_emp(5, "NP0128", "Kiran")])
        fix = fixes["unknown:NP128"]
        self.assertEqual((fix.action, fix.label, fix.device_user_id, fix.employee_id),
                         ("link", "Add as another device ID for Kiran", "NP128", 5))

    def test_twin_of_a_never_punched_id_changes_the_id(self) -> None:
        fixes = self._fixes([_seen("NP0128", "Device")], [_emp(5, "128", "Kiran")])
        self.assertEqual(fixes["unknown:NP0128"].label, "Change ID to NP0128")
        self.assertEqual(fixes["never:128"].device_user_id, "NP0128")

    def test_two_unknowns_with_the_same_number_suggest_nothing(self) -> None:
        fixes = self._fixes([_seen("NP128", "A"), _seen("0128", "B"), _seen("128", "Kiran")], [_emp(5, "128", "Kiran")])
        self.assertEqual(fixes["unknown:NP128"].action, "add")
        self.assertEqual(fixes["unknown:0128"].action, "add")

    def test_all_zero_ids_never_pair_and_get_no_add_button(self) -> None:
        fixes = self._fixes([_seen("0", "X"), _seen("NP0000", "Y")], [_emp(5, "00", "Kiran")])
        self.assertEqual(fixes["unknown:0"].action, "none")
        self.assertEqual(fixes["unknown:NP0000"].action, "none")

    def test_extra_ids_count_as_mapped(self) -> None:
        employee = {**_emp(5, "NP0128", "Kiran"), "device_ids": ["NP0128", "128"], "archived_at": datetime(2026, 1, 1)}
        issues = find_issues([_seen("128", "Kiran"), _seen("NP0128", "Kiran")], [employee])
        self.assertEqual(issues, [])


class SuggestionsTest(unittest.TestCase):
    def test_unknown_id_is_paired_with_matching_unclaimed_roster_person(self) -> None:
        from essl_hub.id_issues import suggestions

        issues = find_issues(
            [{"device_user_id": "NP0231", "device_name": "Sita R", "punch_count": 4}],
            [{"id": 7, "device_user_id": "sita@example.com", "name": "Sita Rao"},
             {"id": 8, "device_user_id": "ravi@example.com", "name": "Ravi Teja"}],
        )
        pairs = suggestions(issues)
        self.assertEqual([p.employee_id for p in pairs["unknown:NP0231"]], [7])
        self.assertEqual([p.device_user_id for p in pairs["placeholder:sita@example.com"]], ["NP0231"])
        self.assertNotIn("placeholder:ravi@example.com", pairs)

    def test_blank_device_name_suggests_nobody(self) -> None:
        from essl_hub.id_issues import suggestions

        issues = find_issues([{"device_user_id": "NP0231", "device_name": "", "punch_count": 1}],
                             [{"id": 7, "device_user_id": "sita@example.com", "name": "Sita Rao"}])
        self.assertEqual(suggestions(issues), {})


class ArchivedPeopleTest(unittest.TestCase):
    def test_archived_ids_stay_mapped_but_no_rule_applies(self) -> None:
        gone = {**_emp(7, "NP0007", "Ravi"), "archived_at": datetime(2026, 10, 1), "device_ids": ["NP0007", "7"]}
        never = {**_emp(8, "NP0008", "Old"), "archived_at": datetime(2026, 10, 1)}
        issues = find_issues([_seen("7", "Someone Else"), _seen("NP0007", "Ravi")], [gone, never])
        self.assertEqual(issues, [])

    def test_archived_holder_is_never_a_twin_suggestion(self) -> None:
        from essl_hub.id_issues import id_twins

        gone = {**_emp(7, "128", "Kiran"), "archived_at": datetime(2026, 10, 1)}
        issues = find_issues([_seen("NP0128", "Kiran")], [gone])
        self.assertEqual(id_twins(issues, [gone]), {})


if __name__ == "__main__":
    unittest.main()
