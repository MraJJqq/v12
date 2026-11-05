import unittest
from unittest.mock import patch

from v12 import bitget


class BitgetGapRepairTests(unittest.TestCase):
    @staticmethod
    def _row(ts, close=100.0):
        return {
            "timestamp_ms": ts, "open": close, "high": close + 1,
            "low": close - 1, "close": close, "closed": True,
        }

    def test_repairs_an_internal_missing_interval(self):
        interval = 60 * 60 * 1000
        data = {
            0: self._row(0),
            2 * interval: self._row(2 * interval),
        }
        with patch.object(bitget, "_page", return_value=[self._row(interval)]):
            bitget._repair_gaps(data, "BTCUSDT", "1H", interval)
        self.assertEqual(sorted(data), [0, interval, 2 * interval])

    def test_unavailable_gap_fails_closed(self):
        interval = 60 * 60 * 1000
        data = {
            0: self._row(0),
            2 * interval: self._row(2 * interval),
        }
        with patch.object(bitget, "_page", return_value=[]):
            with self.assertRaises(bitget.BitgetError):
                bitget._repair_gaps(data, "BTCUSDT", "1H", interval)


if __name__ == "__main__":
    unittest.main()
