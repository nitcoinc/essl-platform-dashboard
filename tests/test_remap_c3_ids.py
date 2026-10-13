import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import remap_c3_ids  # noqa: E402


def _row(row_id, user, event_type=0, pin="9"):
    return {"id": row_id, "device_id": "gachibowli-c3-400", "device_user_id": user, "fingerprint": "old",
            "event_timestamp": "2026-10-10 01:21:40",
            "raw_json": {"pin": pin, "card_no": "1", "door": 1, "event_type": event_type,
                         "timestamp": "2026-10-10 01:21:40", "source": "realtime_fallback"}}


class RemapTest(unittest.TestCase):
    def test_bare_pins_are_rewritten_and_reruns_are_noops(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            (change,) = remap_c3_ids.planned_changes([_row(1, "9")])
            self.assertEqual(change[1], "NP0009")
            done = _row(1, "NP0009")
            done["fingerprint"] = change[2]
            self.assertEqual(remap_c3_ids.planned_changes([done]), [])

    def test_denied_swipes_are_dropped_and_temporary_cards_unlinked(self) -> None:
        self.assertEqual(remap_c3_ids.not_punches([_row(1, "9"), _row(2, "9", event_type=27)]), [2])
        # KTD11: interval-too-short swipes are attendance now; the script keeps them.
        self.assertEqual(remap_c3_ids.not_punches([_row(1, "9"), _row(2, "9", event_type=20)]), [])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(remap_c3_ids.planned_changes([_row(3, "2", pin="2")])[0][1], "C3-2")


if __name__ == "__main__":
    unittest.main()
