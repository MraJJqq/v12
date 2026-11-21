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
            "V12_BARK_SCHEDULE_CRON": "0 0 * * *",
            "V12_BARK_RECOVERY_SLOT": "",
        }, clear=False):
            first = _delivery_slot(report)
        report["started_at"] = "2026-11-21T02:03:00+00:00"
        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule",
            "V12_BARK_SCHEDULE_CRON": "0 2 * * *",
            "V12_BARK_RECOVERY_SLOT": "",
        }, clear=False):
            second = _delivery_slot(report)
        self.assertEqual(first, "20261121-0800")
        self.assertEqual(second, "20261121-1000")
        self.assertNotEqual(first, second)

    def test_schedule_cron_maps_exact_utc_hour(self):
        report={"signal_day":"2026-11-21","started_at":"2026-11-21T01:58:00+00:00"}
        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME":"schedule",
            "V12_BARK_SCHEDULE_CRON":"0 0 * * *",
            "V12_BARK_RECOVERY_SLOT":"",
        }, clear=False):
            self.assertEqual(_delivery_slot(report),"20261121-0800")

    def test_prepare_commits_intent_before_delivery_and_keeps_legacy_fingerprint(self):
        import hashlib
        import json
        from v12 import bark
        with tempfile.TemporaryDirectory() as folder:
            report_path=os.path.join(folder,"report.json")
            pending_path=os.path.join(folder,"pending.json")
            state_path=os.path.join(folder,"state.json")
            report={"signal_day":"2026-11-21","started_at":"2026-11-21T00:00:00+00:00","signals":[]}
            with open(report_path,"w",encoding="utf-8") as stream:
                json.dump(report,stream)
            env={"GITHUB_EVENT_NAME":"schedule","V12_BARK_SCHEDULE_CRON":"0 0 * * *",
                 "V12_BARK_RECOVERY_SLOT":"","GITHUB_RUN_ID":"","V12_BARK_KEY":"test-key"}
            with patch.object(bark,"BARK_PENDING_PATH",pending_path), \
                 patch.object(bark,"BARK_STATE_PATH",state_path), \
                 patch.dict(os.environ,env,clear=False), patch.object(bark,"_send") as send:
                self.assertEqual(bark.prepare_report(report_path),0)
                send.assert_not_called()
                with open(pending_path,encoding="utf-8") as stream:
                    item=json.load(stream)["pending"][0]
                body,_,day,slot,fingerprint=bark._report_delivery(report_path)
                expected=hashlib.sha256(f"{day}\n{slot}\n{body}".encode()).hexdigest()
                self.assertEqual(fingerprint,expected)
                self.assertEqual(item["status"],"sending")
                self.assertEqual(bark.deliver_prepared(report_path),0)
                send.assert_called_once_with(body,"test-key")
                self.assertTrue(bark._accepted(fingerprint,day,slot=slot))

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
