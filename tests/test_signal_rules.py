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
    @staticmethod
    def _four_hour():
        return pd.DataFrame({
            "open_time": pd.to_datetime(["2026-01-01 00:00", "2026-01-01 04:00"]),
            "open": [100.0, 100.0], "high": [101.0, 101.0],
            "low": [99.0, 99.0], "close": [100.0, 100.0],
        })

    def _one_hour(self, rows):
        times = pd.date_range("2026-01-01 04:00", periods=len(rows), freq="1h")
        return pd.DataFrame([
            {"open_time": times[i], "open": row[0], "high": row[1], "low": row[2], "close": row[3]}
            for i, row in enumerate(rows)
        ])

    def test_touch_bar_is_not_confirmation_bar(self):
        d1 = self._one_hour([
            (98, 99, 97, 98),
            (99, 101, 98, 99),
            (100, 102, 100, 101),
            (100, 101, 98, 99),
        ])
        with patch.object(signal_rules, "_ema", side_effect=lambda s, span: pd.Series(100.0, index=s.index)):
            found = signal_rules.detect_signals(self._four_hour(), d1)
        self.assertEqual(len(found), 1)
        self.assertEqual(found.iloc[0]["direction"], "空")
        self.assertEqual(found.iloc[0]["time"], pd.Timestamp("2026-01-01 08:00"))
        self.assertEqual(set(found.iloc[0]["tunnels"]), {"中锋", "红峰"})

    def test_tunnel_rearms_only_after_price_leaves(self):
        d1 = self._one_hour([
            (98, 99, 97, 98),
            (99, 101, 98, 100),
            (99, 102, 98, 99),
            (102, 103, 102, 102),
            (102, 101, 99, 100.5),
            (100, 102, 99, 101),
        ])
        with patch.object(signal_rules, "_ema", side_effect=lambda s, span: pd.Series(100.0, index=s.index)):
            found = signal_rules.detect_signals(self._four_hour(), d1)
        self.assertEqual(len(found), 2)
        self.assertEqual(list(found["direction"]), ["空", "多"])
        self.assertEqual(list(found["time"]), [
            pd.Timestamp("2026-01-01 07:00"),
            pd.Timestamp("2026-01-01 10:00"),
        ])


if __name__ == "__main__":
    unittest.main()

class PairedTunnelWarmupTests(unittest.TestCase):
    def test_tunnel_waits_until_both_emas_are_valid(self):
        index = pd.RangeIndex(4)
        fast = pd.Series([float("nan"), 10.0, 11.0, 12.0], index=index)
        slow = pd.Series([float("nan"), float("nan"), 9.0, 10.0], index=index)
        low, high = signal_rules._paired_bounds(fast, slow)
        self.assertTrue(pd.isna(low.iloc[0]))
        self.assertTrue(pd.isna(high.iloc[0]))
        self.assertTrue(pd.isna(low.iloc[1]))
        self.assertTrue(pd.isna(high.iloc[1]))
        self.assertEqual(low.iloc[2], 9.0)
        self.assertEqual(high.iloc[2], 11.0)
