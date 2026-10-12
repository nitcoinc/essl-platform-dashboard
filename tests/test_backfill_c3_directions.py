import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_c3_directions  # noqa: E402
from essl_hub.config import DEVICES  # noqa: E402
from essl_hub.db import Database  # noqa: E402


def _punch(fingerprint, direction, device_id="gachibowli-c3-400"):
    return {"fingerprint": fingerprint, "device_id": device_id, "site": "Gachibowli",
            "device_user_id": "NP0106", "event_timestamp": datetime(2026, 10, 10, 20, 7),
            "trusted_direction": direction, "raw": {}}


class PlanTest(unittest.TestCase):
    def test_only_null_rows_with_a_directed_history_match_are_planned(self) -> None:
        stored = [{"fingerprint": "a", "trusted_direction": None},
                  {"fingerprint": "b", "trusted_direction": None},
                  {"fingerprint": "c", "trusted_direction": "entry"}]
        history = [_punch("a", "exit"), _punch("b", None), _punch("c", "exit"), _punch("z", "exit")]
        self.assertEqual([r["fingerprint"] for r in backfill_c3_directions.planned_repairs(stored, history)], ["a"])


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Database(os.path.join(tmp.name, "essl.db"))
        self.database.initialize(DEVICES)
        self.database.insert_punches([_punch("a", None), _punch("b", None)])
        self.history = [_punch("a", "exit"), _punch("b", "entry"), _punch("new", "exit")]

    def _run(self, *argv) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            backfill_c3_directions.main(list(argv), database=self.database, downloader=lambda d: self.history)
        return out.getvalue()

    def _directions(self) -> dict:
        with self.database.connect() as connection:
            return {r["fingerprint"]: r["trusted_direction"] for r in connection.execute(
                "SELECT fingerprint, trusted_direction FROM punches").fetchall()}

    def test_dry_run_reports_counts_and_writes_nothing(self) -> None:
        output = self._run()
        self.assertIn("2 C3 punches have no direction; 2 match history (1 exit, 1 entry)", output)
        self.assertEqual(self._directions(), {"a": None, "b": None})

    def test_apply_updates_matches_and_inserts_nothing(self) -> None:
        self.assertIn("applied: 2 updated", self._run("--apply"))
        self.assertEqual(self._directions(), {"a": "exit", "b": "entry"})
        self.assertIn("0 C3 punches have no direction", self._run("--apply"))


if __name__ == "__main__":
    unittest.main()
