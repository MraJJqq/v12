"""Read-only live Bitget history validation for the full V12 contract pool.

This deliberately does not run signal detection, change signal state, send Bark,
or enable the scheduled scanner. Candle cache is isolated under the runner's
temporary workspace and only the compact JSON report is uploaded.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import local
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from v12 import bitget
from v12.config import SCAN_WORKERS, SYMBOLS, TIMEFRAMES

VALIDATION_SYMBOLS = SYMBOLS
_PAGE_COUNTER = local()
_ORIGINAL_PAGE = bitget._page


def _counted_page(*args, **kwargs):
    _PAGE_COUNTER.calls = getattr(_PAGE_COUNTER, "calls", 0) + 1
    return _ORIGINAL_PAGE(*args, **kwargs)

OUTPUT = Path(os.environ.get("V12_VALIDATION_REPORT", "artifacts/v12-bitget-live-validation.json"))
CACHE = Path(os.environ.get("V12_VALIDATION_CACHE", ".cache/v12-live-validation-candles"))


def iso_ms(value):
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def validate_one(symbol, timeframe):
    _PAGE_COUNTER.calls = 0
    started = time.monotonic()
    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "failed",
        "page_requests": 0,
    }
    try:
        candles = bitget.fetch_candles(symbol, timeframe)
        interval = TIMEFRAMES[timeframe]
        gaps = bitget._validate(candles, interval, f"live validation {symbol} {timeframe}")
        cache_path = bitget._path(symbol, timeframe)
        cached, complete = bitget._read_cache(cache_path, interval)
        timestamps = [int(row["timestamp_ms"]) for row in candles]
        result.update({
            "status": "complete" if complete else "incomplete",
            "history_complete": bool(complete),
            "candles_total": len(candles),
            "candles_closed": sum(bool(row.get("closed")) for row in candles),
            "oldest_utc": iso_ms(min(timestamps)),
            "newest_utc": iso_ms(max(timestamps)),
            "cache_candles": len(cached),
            "cache_version": 2 if Path(cache_path).exists() else None,
            "gap_count": len(gaps),
            "gap_examples_utc": [
                {"previous": iso_ms(a), "next": iso_ms(b)}
                for a, b in gaps[:20]
            ],
        })
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        cache_path = bitget._path(symbol, timeframe)
        if Path(cache_path).exists():
            cached, complete = bitget._read_cache(cache_path, TIMEFRAMES[timeframe])
            timestamps = sorted(cached)
            result.update({
                "partial_cache_candles": len(cached),
                "partial_cache_complete": bool(complete),
                "partial_oldest_utc": iso_ms(timestamps[0]) if timestamps else None,
                "partial_newest_utc": iso_ms(timestamps[-1]) if timestamps else None,
            })
    finally:
        result["page_requests"] = _PAGE_COUNTER.calls
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    # Isolate validation cache from the normal V12 scanner cache.
    bitget.CACHE_DIR = str(CACHE)
    started = datetime.now(timezone.utc)
    pairs = [(symbol, timeframe) for symbol in VALIDATION_SYMBOLS for timeframe in ("1H", "4H")]
    original_page = bitget._page
    bitget._page = _counted_page
    try:
        # Thread-local counters preserve per-pair request totals while Bitget's global throttle remains active.
        with ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(pairs))) as pool:
            checks = list(pool.map(lambda pair: validate_one(*pair), pairs))
    finally:
        bitget._page = original_page
    for item in checks:
        print(f"[LIVE VALIDATION] {item['symbol']} {item['timeframe']}", flush=True)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    complete = sum(item["status"] == "complete" for item in checks)
    report = {
        "validation": "V12 live Bitget historical candles",
        "read_only": True,
        "signal_rules_executed": False,
        "bark_sent": False,
        "scheduled_scanner_enabled": False,
        "data_source": "Bitget Mix USDT-FUTURES",
        "started_at_utc": started.isoformat(),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbols": list(VALIDATION_SYMBOLS),
        "timeframes": ["1H", "4H"],
        "checks_total": len(checks),
        "checks_complete": complete,
        "checks_failed_or_incomplete": len(checks) - complete,
        "overall_status": "complete" if complete == len(checks) else "partial",
        "checks": checks,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Validation report: {OUTPUT}", flush=True)
    return 0 if complete == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
