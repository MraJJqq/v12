import unittest

from datetime import datetime, timezone

from v12.watchdog import _later_success


def run(created, event="schedule", conclusion="success", title=""):
    return {
        "created_at": created,
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
        "display_title": title,
    }


class WatchdogSlotTests(unittest.TestCase):
    def setUp(self):
        self.failed = run("2026-11-05T00:05:00Z", conclusion="failure")
        self.slot_end = datetime(2026, 11, 5, 1, 50, tzinfo=timezone.utc)
        self.marker = "recovery:20261105-0800"

    def test_success_in_same_slot_supersedes_failure(self):
        later = run("2026-11-05T00:30:00Z")
        self.assertTrue(_later_success([self.failed, later], self.failed, self.slot_end, self.marker))

    def test_success_in_next_slot_does_not_mask_failure(self):
        later = run("2026-11-05T02:05:00Z")
        self.assertFalse(_later_success([self.failed, later], self.failed, self.slot_end, self.marker))

    def test_unrelated_manual_scan_does_not_mask_failure(self):
        later = run("2026-11-05T00:30:00Z", event="workflow_dispatch", title="V12 scan (manual)")
        self.assertFalse(_later_success([self.failed, later], self.failed, self.slot_end, self.marker))

    def test_successful_recovery_for_same_slot_supersedes_failure(self):
        later = run(
            "2026-11-05T03:00:00Z",
            event="workflow_dispatch",
            title="V12 scan (recovery:20261105-0800)",
        )
        self.assertTrue(_later_success([self.failed, later], self.failed, self.slot_end, self.marker))


if __name__ == "__main__":
    unittest.main()
