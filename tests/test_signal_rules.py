import unittest
from unittest.mock import patch
import pandas as pd
import signal_rules


class EmaWarmupTests(unittest.TestCase):
    def test_all_required_spans_wait_for_full_warmup(self):
        values = pd.Series(range(1, 800), dtype="float64")
        for span in (12, 144, 169, 576, 676):
            with self.subTest(span=span):
                ema = signal_rules._ema(values, span)
                self.assertTrue(ema.iloc[:span - 1].isna().all())
                self.assertTrue(pd.notna(ema.iloc[span - 1]))
                self.assertTrue(pd.notna(ema.iloc[-1]))


class SignalRuleBoundaryTests(unittest.TestCase):
    def test_touch_bar_is_not_confirmation_bar(self):
        d4 = pd.DataFrame({
            "open_time": pd.to_datetime(["2026-01-01 00:00", "2026-01-01 04:00"]),
            "open": [100.0, 100.0], "high": [101.0, 101.0],
            "low": [99.0, 99.0], "close": [100.0, 100.0],
        })
        times = pd.date_range("2026-01-01 04:00", periods=4, freq="1h")
        d1 = pd.DataFrame({
            "open_time": times,
            "open": [98.0, 99.0, 100.0, 100.0],
            "high": [99.0, 101.0, 102.0, 101.0],
            "low": [97.0, 98.0, 100.0, 98.0],
            "close": [98.0, 99.0, 101.0, 99.0],
        })
        with patch.object(signal_rules, "_ema", side_effect=lambda s, span: pd.Series(100.0, index=s.index)):
            found = signal_rules.detect_signals(d4, d1)
        self.assertEqual(len(found), 1)
        self.assertEqual(found.iloc[0]["direction"], "空")
        self.assertEqual(found.iloc[0]["time"], pd.Timestamp("2026-01-01 08:00"))
        self.assertEqual(set(found.iloc[0]["tunnels"]), {"中锋", "红峰"})


if __name__ == "__main__":
    unittest.main()
