import json,os,tempfile,unittest
from unittest.mock import patch
from v12.config import SYMBOLS,CORE_SYMBOLS,STOCK_SYMBOLS
from v12.bitget import BitgetError,_parse_row,_validate,_page,fetch_candles,_read_cache
from v12.bark import build_body,DeliveryError,_post,send_report
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

class BarkTests(unittest.TestCase):
    def test_non_pool_symbols_are_not_notified(self):
        _,symbols=build_body({"signals":[{"symbol":"XYZUSDT","formal_signal":True}]})
        self.assertEqual(symbols,[])

    def test_no_change_sections(self):
        body,symbols=build_body({"signals":[]})
        self.assertEqual(symbols,[])
        self.assertIn("美股巨头\n▰ No change ▰",body)
        self.assertIn("核心能源\n▰ No change ▰",body)
    def test_pool_order(self):
        _,symbols=build_body({"signals":[{"symbol":"NVDAUSDT","formal_signal":True},{"symbol":"BTCUSDT","formal_signal":True}]})
        self.assertEqual(symbols,["NVDA","BTC"])
    def test_missing_key_is_marked_rejected_not_stuck_sending(self):
        from v12 import bark
        with tempfile.TemporaryDirectory() as folder:
            report_path=os.path.join(folder,"report.json")
            pending_path=os.path.join(folder,"pending.json")
            state_path=os.path.join(folder,"state.json")
            with open(report_path,"w",encoding="utf-8") as stream:
                json.dump({"signal_day":"2026-11-05","signals":[]},stream)
            with patch.object(bark,"BARK_PENDING_PATH",pending_path),patch.object(bark,"BARK_STATE_PATH",state_path),patch.object(bark,"_send",side_effect=RuntimeError("missing key")):
                self.assertEqual(send_report(report_path),1)
            with open(pending_path,encoding="utf-8") as stream:
                pending=json.load(stream)["pending"]
                self.assertEqual(pending[0]["status"],"rejected")

    def test_each_scheduled_no_change_scan_still_pushes(self):
        from v12 import bark
        with tempfile.TemporaryDirectory() as folder:
            report_path=os.path.join(folder,"report.json")
            pending_path=os.path.join(folder,"pending.json")
            state_path=os.path.join(folder,"state.json")
            report={"signal_day":"2026-11-05","started_at":"2026-11-05T08:00:00+08:00","signals":[]}
            with patch.object(bark,"BARK_PENDING_PATH",pending_path),patch.object(bark,"BARK_STATE_PATH",state_path),patch.object(bark,"_send") as send:
                with open(report_path,"w",encoding="utf-8") as stream: json.dump(report,stream)
                self.assertEqual(send_report(report_path),0)
                report["started_at"]="2026-11-05T10:00:00+08:00"
                with open(report_path,"w",encoding="utf-8") as stream: json.dump(report,stream)
                self.assertEqual(send_report(report_path),0)
                self.assertEqual(send_report(report_path),0)
                self.assertEqual(send.call_count,2)

    def test_5xx_is_ambiguous(self):
        import urllib.error
        error=urllib.error.HTTPError("https://api.day.app/push",502,"bad gateway",{},None)
        with patch("urllib.request.urlopen",side_effect=error):
            with self.assertRaises(DeliveryError) as caught:_post({"body":"test"})
        self.assertTrue(caught.exception.uncertain)
    def test_429_is_retryable(self):
        import urllib.error
        error=urllib.error.HTTPError("https://api.day.app/push",429,"rate limited",{"Retry-After":"45"},None)
        with patch("urllib.request.urlopen",side_effect=error):
            with self.assertRaises(DeliveryError) as caught:_post({"body":"test"})
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.retry_after,"45")

    def test_pending_queue_preserves_distinct_notifications(self):
        from v12 import bark
        with tempfile.TemporaryDirectory() as folder:
            path=os.path.join(folder,"pending.json")
            with patch.object(bark,"BARK_PENDING_PATH",path):
                bark._pending("first","2026-11-15","first body",["BTC"],"retryable")
                bark._pending("second","2026-11-15","second body",["AMD"],"retryable")
                with open(path,encoding="utf-8") as stream: state=json.load(stream)
        self.assertEqual([item["fingerprint"] for item in state["pending"]],["first","second"])

    def test_interrupted_retry_is_not_blindly_resent(self):
        from v12 import bark
        from datetime import datetime
        from v12.config import TIMEZONE
        with tempfile.TemporaryDirectory() as folder:
            pending_path=os.path.join(folder,"pending.json")
            state_path=os.path.join(folder,"accepted.json")
            today=datetime.now(TIMEZONE).date().isoformat()
            with open(pending_path,"w",encoding="utf-8") as stream:
                json.dump({"pending":[{"fingerprint":"interrupted","date":today,"body":"notice","symbols":["BTC"],"status":"retryable"}]},stream)
            with patch.object(bark,"BARK_PENDING_PATH",pending_path),patch.object(bark,"BARK_STATE_PATH",state_path),patch.object(bark,"_send",side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt): bark.retry_pending()
            with open(pending_path,encoding="utf-8") as stream:
                self.assertEqual(json.load(stream)["pending"][0]["status"],"sending")
            with patch.object(bark,"BARK_PENDING_PATH",pending_path),patch.object(bark,"BARK_STATE_PATH",state_path),patch.object(bark,"_send") as send:
                self.assertEqual(bark.retry_pending(),0)
                send.assert_not_called()

    def test_pending_queue_does_not_read_legacy_single_record(self):
        from v12 import bark
        with tempfile.TemporaryDirectory() as folder:
            pending_path=os.path.join(folder,"pending.json")
            with open(pending_path,"w",encoding="utf-8") as stream:
                json.dump({"fingerprint":"old","date":"2026-11-15","body":"old body","status":"retryable"},stream)
            with patch.object(bark,"BARK_PENDING_PATH",pending_path):
                self.assertEqual(bark._pending_queue(),[])

    def test_retry_after_seconds_and_http_date(self):
        from datetime import datetime,timezone,timedelta
        from email.utils import format_datetime
        from v12.bark import _retry_delay
        now=datetime(2026,11,15,12,0,tzinfo=timezone.utc)
        self.assertEqual(_retry_delay("125",now=now),125)
        header=format_datetime(now+timedelta(seconds=240),usegmt=True)
        self.assertEqual(_retry_delay(header,now=now),240)
        self.assertEqual(_retry_delay(None,attempt=3,now=now),240)

    def test_retry_pending_respects_next_retry_at(self):
        from datetime import datetime,timedelta
        from v12 import bark
        from v12.config import TIMEZONE
        with tempfile.TemporaryDirectory() as folder:
            pending_path=os.path.join(folder,"pending.json")
            state_path=os.path.join(folder,"accepted.json")
            due=(datetime.now(TIMEZONE)+timedelta(hours=1)).isoformat()
            item={"fingerprint":"later","date":datetime.now(TIMEZONE).date().isoformat(),
                "body":"later","symbols":[],"status":"retryable","next_retry_at":due}
            with open(pending_path,"w",encoding="utf-8") as stream:
                json.dump({"pending":[item]},stream)
            with patch.object(bark,"BARK_PENDING_PATH",pending_path),patch.object(bark,"BARK_STATE_PATH",state_path),patch.object(bark,"_send") as send:
                self.assertEqual(bark.retry_pending(),0)
                send.assert_not_called()

    def test_initial_429_persists_retry_after_and_due_time(self):
        from v12 import bark
        with tempfile.TemporaryDirectory() as folder:
            report_path=os.path.join(folder,"report.json")
            pending_path=os.path.join(folder,"pending.json")
            state_path=os.path.join(folder,"state.json")
            with open(report_path,"w",encoding="utf-8") as stream:
                json.dump({"signal_day":"2026-11-15","started_at":"2026-11-15T20:00:00+08:00","signals":[]},stream)
            error=DeliveryError("rate limited",retryable=True,retry_after="300")
            with patch.object(bark,"BARK_PENDING_PATH",pending_path),patch.object(bark,"BARK_STATE_PATH",state_path),patch.object(bark,"_send",side_effect=error),patch.dict(os.environ,{"V12_BARK_KEY":"test"}):
                self.assertEqual(send_report(report_path),1)
            with open(pending_path,encoding="utf-8") as stream:
                item=json.load(stream)["pending"][0]
            self.assertEqual(item["status"],"retryable")
            self.assertEqual(item["retry_after"],"300")
            self.assertEqual(item["retry_attempts"],1)
            self.assertTrue(item["next_retry_at"])

    def test_watchdog_alert_uses_durable_queue_and_separate_accepted_state(self):
        from datetime import datetime
        from v12 import bark
        from v12.config import TIMEZONE
        with tempfile.TemporaryDirectory() as folder:
            pending_path=os.path.join(folder,"pending.json")
            scan_state=os.path.join(folder,"scan-state.json")
            watchdog_state=os.path.join(folder,"watchdog-state.json")
            error=DeliveryError("rate limited",retryable=True,retry_after="300")
            with patch.object(bark,"BARK_PENDING_PATH",pending_path),patch.object(bark,"BARK_STATE_PATH",scan_state),patch.object(bark,"BARK_WATCHDOG_STATE_PATH",watchdog_state),patch.object(bark,"_send",side_effect=error):
                with self.assertRaises(DeliveryError):
                    bark.send_watchdog_alert("watchdog alert","test-key",42)
            with open(pending_path,encoding="utf-8") as stream:
                item=json.load(stream)["pending"][0]
            self.assertEqual(item["kind"],"watchdog")
            self.assertEqual(item["status"],"retryable")
            item["next_retry_at"]=datetime.now(TIMEZONE).isoformat()
            with open(pending_path,"w",encoding="utf-8") as stream:
                json.dump({"pending":[item]},stream)
            with patch.object(bark,"BARK_PENDING_PATH",pending_path),patch.object(bark,"BARK_STATE_PATH",scan_state),patch.object(bark,"BARK_WATCHDOG_STATE_PATH",watchdog_state),patch.object(bark,"_send"):
                self.assertEqual(bark.retry_pending(),0)
            with open(watchdog_state,encoding="utf-8") as stream:
                self.assertEqual(json.load(stream)["fingerprint"],item["fingerprint"])
            self.assertFalse(os.path.exists(scan_state))


    def test_watchdog_issue_alert_is_sent_by_retry_worker(self):
        from datetime import datetime
        from v12 import bark
        from v12.config import TIMEZONE
        with tempfile.TemporaryDirectory() as folder:
            pending_path=os.path.join(folder,"pending.json")
            scan_state=os.path.join(folder,"scan-state.json")
            watchdog_state=os.path.join(folder,"watchdog-state.json")
            issue={"number":42,"title":"[V12 Watchdog] Scan health alert","body":"V12 scan failed."}
            with patch.object(bark,"BARK_PENDING_PATH",pending_path),patch.object(bark,"BARK_STATE_PATH",scan_state),patch.object(bark,"BARK_WATCHDOG_STATE_PATH",watchdog_state),patch.object(bark,"_github_request",side_effect=[[issue],{}]) as api,patch.object(bark,"_send") as send,patch.dict(os.environ,{"GITHUB_REPOSITORY":"MraJJqq/v12","GH_TOKEN":"test","V12_BARK_KEY":"test-key"}):
                self.assertEqual(bark.process_watchdog_issues(),0)
                send.assert_called_once_with("V12 scan failed.","test-key")
                self.assertEqual(api.call_args_list[1].args,("/issues/42","PATCH",{"state":"closed"}))
            with open(watchdog_state,encoding="utf-8") as stream:
                accepted=json.load(stream)
            self.assertEqual(accepted["date"],datetime.now(TIMEZONE).date().isoformat())
            self.assertFalse(os.path.exists(scan_state))


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
