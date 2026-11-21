import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from v12.watchdog import _later_success, _scan_state
from v12.config import WATCHDOG_GRACE_MINUTES


def run(created, event="schedule", conclusion="success", title=""):
    return {
        "created_at": created,
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
        "display_title": title,
    }


class V12ScanWatchdogTests(unittest.TestCase):
    def test_grace_period_is_ten_minutes(self):
        self.assertEqual(WATCHDOG_GRACE_MINUTES, 10)

    def test_later_success_in_same_slot_supersedes_failure(self):
        failed = run("2026-11-21T00:01:00Z", conclusion="failure")
        later = run("2026-11-21T00:15:00Z")
        end = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)
        self.assertTrue(_later_success([failed, later], failed, end, "recovery:20261121-0800"))

    def test_success_in_next_slot_does_not_mask_failure(self):
        failed = run("2026-11-21T00:01:00Z", conclusion="failure")
        later = run("2026-11-21T02:01:00Z")
        end = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)
        self.assertFalse(_later_success([failed, later], failed, end, "recovery:20261121-0800"))

    def test_unrelated_manual_run_does_not_mask_scheduled_failure(self):
        failed = run("2026-11-21T00:01:00Z", conclusion="failure")
        manual = run("2026-11-21T00:15:00Z", event="workflow_dispatch", title="V12 scan (manual)")
        end = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)
        self.assertFalse(_later_success([failed, manual], failed, end, "recovery:20261121-0800"))

    def test_scan_and_relay_success_are_confirmed(self):
        payload = {"jobs": [{"steps": [
            {"name": "Scan Bitget history", "conclusion": "success"},
            {"name": "Persist signal state", "conclusion": "success"},
            {"name": "Publish complete signal snapshot to relay", "conclusion": "success"},
        ]}]}
        with patch("v12.watchdog._request", return_value=payload):
            self.assertEqual(_scan_state("https://api.github.com/repos/test/v12", {"id": 1}), "complete")

    def test_scan_failure_is_recoverable(self):
        payload = {"jobs": [{"steps": [
            {"name": "Scan Bitget history", "conclusion": "failure"},
        ]}]}
        with patch("v12.watchdog._request", return_value=payload):
            self.assertEqual(_scan_state("https://api.github.com/repos/test/v12", {"id": 2}), "scan_failed")

    def test_relay_failure_is_distinguished_from_scan_failure(self):
        payload = {"jobs": [{"steps": [
            {"name": "Scan Bitget history", "conclusion": "success"},
            {"name": "Persist signal state", "conclusion": "success"},
            {"name": "Publish complete signal snapshot to relay", "conclusion": "failure"},
        ]}]}
        with patch("v12.watchdog._request", return_value=payload):
            self.assertEqual(_scan_state("https://api.github.com/repos/test/v12", {"id": 3}), "publish_failed")


if __name__ == "__main__":
    unittest.main()
