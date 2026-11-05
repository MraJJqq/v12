import json
import os
import tempfile
import unittest
from v12.config import SYMBOLS
from v12.scanner import build_public_feed, publish_public_feed

def complete_report():
    return {
        "signal_day": "2026-11-05",
        "scan_status": "complete",
        "symbols_ok": 27,
        "symbols_total": 27,
        "started_at": "2026-11-05T16:00:00+08:00",
        "finished_at": "2026-11-05T16:04:00+08:00",
        "signals": [
            {"symbol": "AMZNUSDT", "formal_signal": True, "candle_timestamp_ms": 1,
             "filter_side": "多", "tunnels": ["中锋"]},
            {"symbol": "QCOMUSDT", "formal_signal": True, "candle_timestamp_ms": 2,
             "filter_side": "空", "tunnels": ["红峰"]},
            {"symbol": "BTCUSDT", "formal_signal": False},
        ],
    }

class PublicFeedTests(unittest.TestCase):
    def test_public_schema_matches_v4_contract(self):
        payload = build_public_feed(complete_report())
        self.assertEqual(payload["source"], "VEGAS V12")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["scan_status"], "complete")
        self.assertEqual((payload["symbols_ok"], payload["symbols_total"]), (27, 27))
        self.assertEqual(len(payload["symbols"]), 27)
        self.assertEqual(len(set(payload["symbols"])), 27)
        self.assertIn("SKHY", payload["symbols"])
        self.assertEqual([x["symbol"] for x in payload["signals"]], ["AMZNUSDT", "QCOMUSDT"])

    def test_incomplete_scan_is_never_published_or_allowed_to_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "public-signals.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"source": "previous"}, stream)
            report = complete_report()
            report["scan_status"] = "partial"
            report["symbols_ok"] = 26
            self.assertFalse(publish_public_feed(report, path))
            with open(path, encoding="utf-8") as stream:
                self.assertEqual(json.load(stream), {"source": "previous"})

    def test_complete_scan_writes_atomic_public_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "public-signals.json")
            self.assertTrue(publish_public_feed(complete_report(), path))
            with open(path, encoding="utf-8") as stream:
                payload = json.load(stream)
            self.assertEqual(payload["symbols_total"], len(SYMBOLS))
            self.assertEqual(payload["signal_day"], "2026-11-05")

if __name__ == "__main__":
    unittest.main()
