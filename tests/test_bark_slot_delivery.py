import os
import tempfile
import unittest
from unittest.mock import patch

from v12.bark import _accepted, _delivery_slot, _save_ok


class BarkSlotDeliveryTests(unittest.TestCase):
    def test_each_scheduled_beijing_slot_has_its_own_identity(self):
        report = {
            "signal_day": "2026-11-21",
            "started_at": "2026-11-21T00:03:00+00:00",
        }
        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule",
            "V12_BARK_SCHEDULE_CRON": "0 0,2,8,12,14 * * *",
        }, clear=False):
            first = _delivery_slot(report)
            report["started_at"] = "2026-11-21T02:03:00+00:00"
            second = _delivery_slot(report)
        self.assertEqual(first, "20261121-0800")
        self.assertEqual(second, "20261121-1000")
        self.assertNotEqual(first, second)

    def test_recovery_uses_the_original_slot_identity(self):
        with patch.dict(os.environ, {
            "V12_BARK_RECOVERY_SLOT": "recovery:20261121-0800",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
        }, clear=False):
            self.assertEqual(
                _delivery_slot({"signal_day": "2026-11-21"}),
                "20261121-0800",
            )

    def test_success_is_deduplicated_only_within_the_same_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "bark-state.json")
            _save_ok("fingerprint-0800", "2026-11-21", path, "20261121-0800")
            self.assertTrue(_accepted(
                "fingerprint-0800", "2026-11-21", path, "20261121-0800"
            ))
            self.assertFalse(_accepted(
                "fingerprint-1000", "2026-11-21", path, "20261121-1000"
            ))


if __name__ == "__main__":
    unittest.main()
