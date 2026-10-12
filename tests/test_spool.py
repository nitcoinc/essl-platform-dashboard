from __future__ import annotations

import os
import tempfile
import unittest

from essl_hub.spool import MAX_ATTEMPTS, Spool


class SpoolTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "nested", "collector-spool.db")
        self.spool = Spool(self.path)
        self.addCleanup(self.spool.close)

    def test_append_and_pending_are_oldest_first(self) -> None:
        self.spool.append("a", [{"fingerprint": "1"}])
        self.spool.append("b", [{"fingerprint": "2"}, {"fingerprint": "3"}])
        entries = self.spool.pending()
        self.assertEqual([e.device_id for e in entries], ["a", "b"])
        self.assertEqual(entries[1].punches, [{"fingerprint": "2"}, {"fingerprint": "3"}])
        self.assertEqual(self.spool.depth(), 2)
        self.assertEqual(self.spool.oldest_at(), entries[0].created_at)

    def test_ack_removes_the_batch(self) -> None:
        entry_id = self.spool.append("a", [{"fingerprint": "1"}])
        self.spool.ack(entry_id)
        self.assertEqual(self.spool.pending(), [])
        self.assertIsNone(self.spool.oldest_at())

    def test_non_json_values_are_stored_as_text(self) -> None:
        from datetime import datetime

        self.spool.append("a", [{"event_timestamp": datetime(2026, 10, 10, 9, 0)}])
        self.assertEqual(self.spool.pending()[0].punches, [{"event_timestamp": "2026-10-10 09:00:00"}])

    def test_fail_bumps_attempts_then_moves_to_dead(self) -> None:
        entry_id = self.spool.append("a", [{"fingerprint": "1"}])
        for _ in range(MAX_ATTEMPTS - 1):
            self.assertFalse(self.spool.fail(entry_id, "boom"))
        self.assertEqual(self.spool.pending()[0].attempts, MAX_ATTEMPTS - 1)
        self.assertTrue(self.spool.fail(entry_id, "boom"))
        self.assertEqual(self.spool.depth(), 0)
        self.assertEqual(self.spool.dead_count(), 1)

    def test_the_spool_survives_a_restart(self) -> None:
        self.spool.append("a", [{"fingerprint": "1"}])
        dead = self.spool.append("b", [{"fingerprint": "2"}])
        for _ in range(MAX_ATTEMPTS):
            self.spool.fail(dead, "boom")
        self.spool.close()
        reopened = Spool(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual([e.punches for e in reopened.pending()], [[{"fingerprint": "1"}]])
        self.assertEqual(reopened.dead_count(), 1)
        reopened.append("c", [])
        self.assertEqual(reopened.depth(), 2)


if __name__ == "__main__":
    unittest.main()
