"""Bitget full-history fetcher with bounded-window pagination and resumable cache."""
import json, math, os, threading, time, urllib.error, urllib.parse, urllib.request
from .config import (
    BITGET_BASE_URL,
    CACHE_DIR,
    HISTORY_PAGE_LIMIT,
    HTTP_MAX_RETRIES,
    HTTP_MIN_INTERVAL_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    PRODUCT_TYPE,
    TIMEFRAMES,
)

class BitgetError(RuntimeError): pass
_lock = threading.Lock()
_next = 0.0
MAX_HISTORY_PAGES = 10000
MAX_QUERY_RANGE_MS = 90 * 24 * 60 * 60 * 1000

def _slot():
    global _next
    with _lock:
        delay = _next - time.monotonic()
        if delay > 0: time.sleep(delay)
        _next = time.monotonic() + HTTP_MIN_INTERVAL_SECONDS

def _get(path, params):
    req = urllib.request.Request(f"{BITGET_BASE_URL}{path}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent":"VEGAS-V12/1.0","Accept":"application/json"})
    for n in range(HTTP_MAX_RETRIES):
        _slot()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as r:
                payload = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if (e.code != 429 and not 500 <= e.code < 600) or n == HTTP_MAX_RETRIES - 1:
                raise BitgetError(f"Bitget HTTP {e.code}: {e.reason}") from e
            retry = e.headers.get("Retry-After") if e.headers else None
            try: delay = max(float(retry or 0), 2 ** (n + 1))
            except (TypeError, ValueError): delay = 2 ** (n + 1)
            time.sleep(min(delay, 30)); continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if n == HTTP_MAX_RETRIES - 1: raise BitgetError(f"Bitget network failure: {e}") from e
            time.sleep(min(2 ** (n + 1), 8)); continue
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise BitgetError(f"Invalid Bitget JSON: {e}") from e
        if not isinstance(payload, dict):
            raise BitgetError("Invalid Bitget response: expected a JSON object")
        if payload.get("code") == "00000": return payload
        code, msg = str(payload.get("code", "?")), str(payload.get("msg", "?"))
        if ("429" in code or "rate limit" in msg.lower()) and n < HTTP_MAX_RETRIES - 1:
            time.sleep(min(2 ** (n + 1), 30)); continue
        raise BitgetError(f"Bitget API error {code}: {msg}")
    raise BitgetError("Bitget retry limit reached")

def _parse_row(row, ctx):
    if not isinstance(row, list) or len(row) < 5: raise BitgetError(f"Malformed candle: {ctx}")
    try:
        ts = int(row[0]); o,h,l,c = (float(row[i]) for i in range(1,5))
    except (TypeError, ValueError, OverflowError) as e: raise BitgetError(f"Invalid candle: {ctx}") from e
    if ts < 0 or not all(math.isfinite(v) and v > 0 for v in (o,h,l,c)):
        raise BitgetError(f"Invalid OHLC: {ctx} @ {ts}")
    if h < max(o,c) or l > min(o,c) or l > h: raise BitgetError(f"Invalid OHLC: {ctx} @ {ts}")
    return {"timestamp_ms":ts,"open":o,"high":h,"low":l,"close":c,"closed":False}

def _validate(rows, interval, ctx):
    """Check alignment and uniqueness; real missing intervals are reported, not fatal."""
    seen=set(); previous=None; gaps=[]
    for row in sorted(rows,key=lambda x:int(x["timestamp_ms"])):
        ts=int(row["timestamp_ms"])
        if ts % interval: raise BitgetError(f"Misaligned {ctx} timestamp {ts}")
        if ts in seen: raise BitgetError(f"Duplicate {ctx} timestamp {ts}")
        if previous is not None and ts-previous > interval: gaps.append((previous,ts))
        seen.add(ts); previous=ts
    return gaps

def _path(symbol,tf): return os.path.join(CACHE_DIR,f"{symbol}-{tf}.json")

def _read_cache(path,interval):
    try:
        with open(path,encoding="utf-8") as f: payload=json.load(f)
        if not isinstance(payload, dict):
            return {}, False
        rows = payload.get("candles", [])
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or "timestamp_ms" not in row for row in rows
        ):
            return {}, False
        data={}
        for row in rows:
            if not isinstance(row, dict):
                return {}, False
            parsed=_parse_row(
                [row.get("timestamp_ms"), row.get("open"), row.get("high"),
                 row.get("low"), row.get("close")],
                "cache",
            )
            parsed["closed"]=bool(row.get("closed", False))
            data[parsed["timestamp_ms"]]=parsed
        if len(data)!=len(rows): return {},False
        _validate(list(data.values()),interval,"cache")
        return data,payload.get("version") == 2 and payload.get("complete") is True
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError,BitgetError): return {},False

def _save(path,rows,complete):
    os.makedirs(os.path.dirname(path),exist_ok=True)
    payload={"version":2,"complete":bool(complete),"candles":rows}
    # Avoid rewriting an unchanged cache file; retain atomic replacement on changes.
    try:
        with open(path,encoding="utf-8") as existing:
            if json.load(existing) == payload:
                return
    except (OSError,ValueError,TypeError):
        pass
    tmp=path+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(payload,f,separators=(",",":"))
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)

def _repair_gaps(data, symbol, tf, interval):
    """Backfill missing intervals between cached candles; fail closed if unavailable."""
    for _ in range(MAX_HISTORY_PAGES):
        gaps=_validate(list(data.values()),interval,f"{symbol} {tf} gap repair")
        if not gaps:
            return
        progressed=False
        for previous, following in gaps:
            cursor=following
            while cursor-previous > interval:
                page=_page(symbol,tf,cursor)
                additions=[row for row in page if previous < int(row["timestamp_ms"]) < cursor]
                if not additions:
                    break
                before=len(data)
                _merge(data,additions)
                progressed = progressed or len(data)>before
                next_cursor=min(int(row["timestamp_ms"]) for row in additions)
                if next_cursor >= cursor:
                    break
                cursor=next_cursor
        if not progressed:
            remaining=_validate(list(data.values()),interval,f"{symbol} {tf} unresolved gaps")
            first,last=remaining[0] if remaining else (None,None)
            raise BitgetError(
                f"Unrepairable {tf} history gap for {symbol}: {first} -> {last}"
            )
    raise BitgetError(f"{tf} gap repair exceeded safety page cap for {symbol}")

def _page(symbol,tf,end=None):
    interval=TIMEFRAMES[tf]
    params={"symbol":symbol,"productType":PRODUCT_TYPE,"granularity":tf,"limit":str(HISTORY_PAGE_LIMIT)}
    if end is not None:
        end=int(end)
        start=max(0,end-MAX_QUERY_RANGE_MS+interval)
        start=(start//interval)*interval
        params["startTime"]=str(start); params["endTime"]=str(end)
    raw = _get("/api/v2/mix/market/history-candles", params).get("data") or []
    if not isinstance(raw, list):
        raise BitgetError(f"Malformed Bitget candle page: {symbol} {tf}")
    rows = [_parse_row(row, f"{symbol} {tf}") for row in raw]
    if end is not None: rows=[x for x in rows if x["timestamp_ms"] < end]
    _validate(rows,interval,f"{symbol} {tf} page")
    return sorted(rows,key=lambda x:x["timestamp_ms"])

def _merge(target,rows):
    for row in rows: target[int(row["timestamp_ms"])]=row

def fetch_candles(symbol,tf):
    if tf not in TIMEFRAMES: raise ValueError(f"Unsupported timeframe: {tf}")
    interval=TIMEFRAMES[tf]; path=_path(symbol,tf)
    data,complete=_read_cache(path,interval); done=False
    newest=_page(symbol,tf)
    if not newest:
        if data and complete:
            rows=sorted(data.values(),key=lambda x:x["timestamp_ms"])
            now=int(time.time()*1000)
            for row in rows: row["closed"]=row["timestamp_ms"]+interval<=now
            return rows
        raise BitgetError(f"No recent {tf} candles for {symbol}")
    _merge(data,newest)
    if complete:
        _validate(list(data.values()),interval,f"{symbol} refreshed cache")
        done=True
    else:
        end=min(data) if data else min(x["timestamp_ms"] for x in newest)
        pages=0
        try:
            while pages < MAX_HISTORY_PAGES:
                page=_page(symbol,tf,end); pages+=1
                if not page:
                    done=True
                    break
                older=[x for x in page if x["timestamp_ms"] < end]
                if not older: raise BitgetError(f"{tf} pagination made no backward progress for {symbol} (end={end})")
                _merge(data,older)
                _validate(list(data.values()),interval,f"{symbol} history")
                end=min(x["timestamp_ms"] for x in older)
            else: raise BitgetError(f"{tf} history exceeded safety page cap for {symbol}")
        except Exception:
            partial=sorted(data.values(),key=lambda x:x["timestamp_ms"])
            if partial:
                _validate(partial,interval,"partial cache")
                _save(path,partial,False)
            raise
    rows=sorted(data.values(),key=lambda x:x["timestamp_ms"])
    if not rows: raise BitgetError(f"No {tf} history available for {symbol}")
    try:
        _repair_gaps(data,symbol,tf,interval)
    except Exception:
        partial=sorted(data.values(),key=lambda x:x["timestamp_ms"])
        if partial: _save(path,partial,False)
        raise
    rows=sorted(data.values(),key=lambda x:x["timestamp_ms"])
    now=int(time.time()*1000)
    for row in rows: row["closed"]=row["timestamp_ms"]+interval<=now
    _save(path,rows,done)
    return rows
