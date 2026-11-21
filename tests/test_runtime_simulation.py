import json
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from v12 import scanner, state


class RuntimeSimulationTests(unittest.TestCase):
    @staticmethod
    def candle(timestamp_ms):
        return {
            "timestamp_ms": timestamp_ms,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "closed": True,
        }

    def test_full_pool_scan_simulation_is_read_only_and_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            state_path = os.path.join(folder, "signal-state.json")
            row = self.candle(1793836800000)
            with open(state_path, "w", encoding="utf-8") as stream:
                json.dump({"__daily_formal_signals__": {
                    "date": datetime.now(scanner.TIMEZONE).date().isoformat(),
                    "signals": [
                        {"symbol": "XYZUSDT", "formal_signal": True, "candle_timestamp_ms": 1, "filter_side": "多"},
                        {"symbol": "AMDUSDT", "formal_signal": True, "candle_timestamp_ms": 2, "filter_side": "多"},
                    ],
                }}, stream)

            with patch.object(scanner, "STATE_PATH", state_path), \
                 patch.object(scanner, "fetch_candles", return_value=[row]), \
                 patch.object(scanner, "detect_signals",
                              return_value=pd.DataFrame(columns=["time", "direction", "tunnels"])):
                report = scanner.scan_market_data()

            self.assertEqual(report["scan_status"], "complete")
            self.assertEqual(report["symbols_total"], 22)
            self.assertEqual(report["symbols_ok"], 22)
            self.assertEqual(report["symbols_excluded"], 0)
            self.assertIn("OPENAIUSDT", [item["symbol"] for item in report["results"]])
            self.assertEqual([item["symbol"] for item in report["signals"]], ["AMDUSDT"])
            self.assertEqual(scanner.scan_exit_code(report), 0)
            self.assertTrue(os.path.exists(state_path))
            saved = state.load_signal_state(state_path)
            self.assertIn("__daily_formal_signals__", saved)

    def test_daily_signal_merge_deduplicates_and_resets_on_new_day(self):
        signal = {
            "symbol": "BTCUSDT",
            "formal_signal": True,
            "candle_timestamp_ms": 123456,
            "filter_side": "above",
        }
        same_day = state.merge_daily_signals(
            {"date": "2026-11-05", "signals": [signal]},
            [signal],
            "2026-11-05",
        )
        self.assertEqual(len(same_day["signals"]), 1)

        next_day = state.merge_daily_signals(
            {"date": "2026-11-05", "signals": [signal]},
            [],
            "2026-11-06",
        )
        self.assertEqual(next_day, {"date": "2026-11-06", "signals": []})



if __name__ == "__main__":
    unittest.main()
