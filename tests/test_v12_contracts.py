import json,os,tempfile,unittest
from unittest.mock import patch
from v12.config import SYMBOLS,CORE_SYMBOLS,STOCK_SYMBOLS
from v12.bitget import BitgetError,_parse_row,_validate,_page,fetch_candles,_read_cache
from v12.scanner import _frame, scan_exit_code
from v12.state import atomic_write_json, load_signal_state, merge_daily_signals

class PoolTests(unittest.TestCase):
    def test_22_contracts(self):
        self.assertEqual((len(SYMBOLS),len(CORE_SYMBOLS),len(STOCK_SYMBOLS)),(22,4,18))
        self.assertIn("SKHYUSDT",SYMBOLS)
        self.assertIn("OPENAIUSDT",SYMBOLS)
        self.assertEqual(len(set(SYMBOLS)),22)

class CandleTests(unittest.TestCase):
    def test_valid_row(self):
        self.assertEqual(_parse_row(["1793505600000","100","103","99","102"],"BTC")["close"],102)
    def test_bad_ohlc(self):
        with self.assertRaises(BitgetError):
            _parse_row(["1793505600000","100","99","98","101"],"BTC")
    def test_gap_is_reported_not_rejected(self):
        gaps=_validate([{"timestamp_ms":0},{"timestamp_ms":7200000}],3600000,"test")
        self.assertEqual(len(gaps),1)
    def test_duplicate_rejected(self):
        with self.assertRaises(BitgetError):
            _validate([{"timestamp_ms":0},{"timestamp_ms":0}],3600000,"test")
    def test_page_uses_bounded_90_day_window_and_exclusive_end(self):
        from v12 import bitget
        captured={}
        def fake_get(path,params):
            captured.update(params)
            return {"data":[["7200000","10","11","9","10"],["3600000","10","11","9","10"]]}
        with patch.object(bitget,"_get",side_effect=fake_get):
            rows=_page("BTCUSDT","1H",7200000)
        self.assertLessEqual(int(captured["endTime"])-int(captured["startTime"]),bitget.MAX_QUERY_RANGE_MS)
        self.assertEqual([x["timestamp_ms"] for x in rows],[3600000])
    def test_legacy_cache_complete_flag_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as folder:
            path=os.path.join(folder,"legacy.json")
            with open(path,"w",encoding="utf-8") as stream:
                json.dump({"version":1,"complete":True,"candles":[
                    {"timestamp_ms":0,"open":10,"high":11,"low":9,"close":10}
                ]},stream)
            data,complete=_read_cache(path,3600000)
        self.assertEqual(len(data),1)
        self.assertFalse(complete)

    def test_short_pages_do_not_mark_history_complete(self):
        from v12 import bitget
        interval=3600000;base=100*interval
        first=[{"timestamp_ms":base+i*interval,"open":10,"high":11,"low":9,"close":10,"closed":False} for i in range(3)]
        second=[{"timestamp_ms":base-interval,"open":10,"high":11,"low":9,"close":10,"closed":False}]
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(bitget,"CACHE_DIR",folder),patch.object(bitget,"_page",side_effect=[first,second,[]]):
                rows=fetch_candles("BTCUSDT","1H")
                with open(os.path.join(folder,"BTCUSDT-1H.json"),encoding="utf-8") as stream:
                    cache=json.load(stream)
        self.assertEqual(len(rows),4)
        self.assertTrue(cache["complete"])

class StatusTests(unittest.TestCase):
    def test_complete(self):
        self.assertEqual(scan_exit_code({"scan_status":"complete","symbols_ok":22,"symbols_total":22}),0)
    def test_partial(self):
        self.assertEqual(scan_exit_code({"scan_status":"partial","symbols_ok":21,"symbols_total":22}),1)



class StateTests(unittest.TestCase):
    def test_daily_state_resets_for_new_date(self):
        previous = {"date": "2026-11-04", "signals": [
            {"symbol": "BTCUSDT", "formal_signal": True, "candle_timestamp_ms": 1, "filter_side": "多"}
        ]}
        current = [{"symbol": "NVDAUSDT", "formal_signal": True, "candle_timestamp_ms": 2, "filter_side": "空"}]
        merged = merge_daily_signals(previous, current, "2026-11-05")
        self.assertEqual([item["symbol"] for item in merged["signals"]], ["NVDAUSDT"])

    def test_daily_state_deduplicates_formal_signals(self):
        signal = {"symbol": "BTCUSDT", "formal_signal": True, "candle_timestamp_ms": 1, "filter_side": "多"}
        merged = merge_daily_signals({"date": "2026-11-05", "signals": [signal]}, [signal], "2026-11-05")
        self.assertEqual(len(merged["signals"]), 1)

    def test_atomic_json_write_and_invalid_state_shape(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "state.json")
            atomic_write_json(path, {"ok": True})
            self.assertEqual(load_signal_state(path), {"ok": True})
            with open(path, "w", encoding="utf-8") as stream:
                json.dump([], stream)
            self.assertEqual(load_signal_state(path), {})

    def test_malformed_cache_root_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "bad.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump([], stream)
            data, complete = _read_cache(path, 3600000)
        self.assertEqual(data, {})
        self.assertFalse(complete)


    def test_cache_with_invalid_ohlc_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as folder:
            path=os.path.join(folder,"bad-ohlc.json")
            with open(path,"w",encoding="utf-8") as stream:
                json.dump({"version":2,"complete":True,"candles":[{
                    "timestamp_ms":0,"open":10,"high":9,"low":8,"close":10,"closed":True
                }]},stream)
            data,complete=_read_cache(path,3600000)
        self.assertEqual(data,{})
        self.assertFalse(complete)


class ScannerInputTests(unittest.TestCase):
    def test_frame_excludes_unclosed_candles(self):
        rows = [
            {"timestamp_ms": 0, "open": 10, "high": 11, "low": 9, "close": 10, "closed": True},
            {"timestamp_ms": 3600000, "open": 10, "high": 12, "low": 9, "close": 11, "closed": False},
        ]
        frame = _frame(rows, "1H")
        self.assertEqual(len(frame), 1)
        self.assertEqual(float(frame.iloc[0]["close"]), 10.0)


if __name__=="__main__":unittest.main()
