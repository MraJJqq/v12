"""V12 orchestration; signal decisions are delegated unchanged to signal_rules.py."""
import json,os,sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import pandas as pd
from signal_rules import detect_signals
from .bitget import fetch_candles
from .config import (
    REPORT_PATH,
    SCAN_WORKERS,
    STATE_PATH,
    SYMBOLS,
    TIMEZONE,
    VERSION,
)
from .state import load_signal_state,merge_daily_signals,save_signal_state

def _frame(rows,tf):
    closed=[x for x in rows if x.get("closed")]
    if not closed:raise ValueError(f"No closed {tf} candles")
    d=pd.DataFrame(closed)
    d["open_time"]=pd.to_datetime(d["timestamp_ms"],unit="ms",utc=True).dt.tz_localize(None)
    return d[["open_time","open","high","low","close"]].sort_values("open_time").reset_index(drop=True)
def _ms(value):
    t=pd.Timestamp(value)
    t=t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return int(t.timestamp()*1000)
def _day(ms):return datetime.fromtimestamp(ms/1000,tz=timezone.utc).astimezone(TIMEZONE).date().isoformat()
def _one(symbol,day):
    item={"symbol":symbol,"data_available":False}
    try:
        h1=fetch_candles(symbol,"1H");h4=fetch_candles(symbol,"4H")
        d1,d4=_frame(h1,"1H"),_frame(h4,"4H")
        found=detect_signals(d4,d1);records=[]
        for row in found.to_dict("records"):
            ts=_ms(row["time"])
            if _day(ts)==day:
                records.append({"symbol":symbol,"formal_signal":True,"candle_timestamp_ms":ts,"filter_side":str(row["direction"]),"tunnels":list(row["tunnels"])})
        item.update(data_available=True,candles={"1H_received":len(h1),"1H_closed":len(d1),"4H_received":len(h4),"4H_closed":len(d4)},history={"1H_oldest":h1[0]["timestamp_ms"],"1H_newest":h1[-1]["timestamp_ms"],"4H_oldest":h4[0]["timestamp_ms"],"4H_newest":h4[-1]["timestamp_ms"]})
        return item,records
    except Exception as e:
        item["error"]=f"{type(e).__name__}: {e}"
        return item,[]
def scan_market_data():
    started=datetime.now(TIMEZONE);day=started.date().isoformat()
    state=load_signal_state(STATE_PATH);prior=state.get("__daily_formal_signals__",{})
    if not isinstance(prior,dict):prior={}
    report={"version":VERSION,"started_at":started.isoformat(),"signal_day":day,"source":"Bitget Mix USDT-FUTURES","scan_timeframes":["4H","1H"],"symbols_total":len(SYMBOLS),"symbols_ok":0,"symbols_excluded":0,"scan_status":"running","failed_symbols":[],"results":[],"signals":[]}
    with ThreadPoolExecutor(max_workers=min(SCAN_WORKERS,len(SYMBOLS))) as pool:
        results=list(pool.map(lambda s:_one(s,day),SYMBOLS))
    new=[]
    for symbol,(item,signals) in zip(SYMBOLS,results):
        report["results"].append(item)
        if item["data_available"]:
            report["symbols_ok"]+=1;new.extend(signals)
        else:
            report["symbols_excluded"]+=1
            report["failed_symbols"].append({"symbol":symbol,"error":item.get("error","unknown")})
    daily=merge_daily_signals(prior,new,day);report["signals"]=daily["signals"]
    report["finished_at"]=datetime.now(TIMEZONE).isoformat()
    report["scan_status"]="complete" if report["symbols_ok"]==report["symbols_total"] else ("partial" if report["symbols_ok"] else "failed")
    state["__daily_formal_signals__"]=daily;state["__rule_version__"]={"version":11,"source":"signal_rules.py"}
    save_signal_state(STATE_PATH,state)
    return report
def scan_exit_code(report):
    return 0 if report.get("scan_status")=="complete" and report.get("symbols_ok")==report.get("symbols_total") else 1
def main():
    report=scan_market_data();rendered=json.dumps(report,ensure_ascii=False,indent=2);print(rendered)
    path=os.environ.get("V12_REPORT_PATH",REPORT_PATH);os.makedirs(os.path.dirname(path) or ".",exist_ok=True)
    with open(path,"w",encoding="utf-8") as f:f.write(rendered+"\n")
    return scan_exit_code(report)
if __name__=="__main__":sys.exit(main())
