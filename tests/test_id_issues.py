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
        issues = find_issues([_seen("NP0002", "Chandra")], [_emp(2, "NP0002", "Ravi")], {"name:NP0002"})
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


if __name__ == "__main__":
    unittest.main()
