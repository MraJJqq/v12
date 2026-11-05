"""Read-only V12 full-pool EMA and signal-rule validation."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from signal_rules import _ema, detect_signals
from v12 import bitget
from v12.config import SYMBOLS, TIMEFRAMES

OUTPUT = Path(os.environ.get("V12_BATCH_REPORT", "artifacts/v12-batch-validation.json"))
CACHE = Path(os.environ.get("V12_BATCH_CACHE", ".cache/v12-batch-validation"))
REQUIRED = {"1H": (12,), "4H": (144, 169, 576, 676)}


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    bitget.CACHE_DIR = str(CACHE)
    started = datetime.now(timezone.utc)
    history, checks, errors = {}, [], []

    for symbol in SYMBOLS:
        for timeframe in ("1H", "4H"):
            print(f"[V12 BATCH] {symbol} {timeframe}", flush=True)
            try:
                rows = bitget.fetch_candles(symbol, timeframe)
                closed = [row for row in rows if row.get("closed")]
                gaps = bitget._validate(rows, TIMEFRAMES[timeframe], f"{symbol} {timeframe}")
                closes = pd.Series([float(row["close"]) for row in closed], dtype="float64")
                ema_checks = {}
                for span in REQUIRED[timeframe]:
                    values = _ema(closes, span)
                    first = values.first_valid_index()
                    ok = len(closed) >= span and first == span - 1 and pd.notna(values.iloc[-1])
                    ema_checks[str(span)] = {
                        "bars_required": span, "closed_bars": len(closed),
                        "first_valid_index": first, "latest_value": float(values.iloc[-1]) if pd.notna(values.iloc[-1]) else None,
                        "passed": bool(ok),
                    }
                    if not ok:
                        errors.append(f"{symbol} {timeframe} EMA{span}: warmup or latest value failed")
                if gaps:
                    errors.append(f"{symbol} {timeframe}: {len(gaps)} candle gaps")
                checks.append({
                    "symbol": symbol, "timeframe": timeframe,
                    "candles_total": len(rows), "candles_closed": len(closed),
                    "oldest_timestamp_ms": int(rows[0]["timestamp_ms"]),
                    "newest_timestamp_ms": int(rows[-1]["timestamp_ms"]),
                    "gap_count": len(gaps), "ema": ema_checks,
                })
                history[(symbol, timeframe)] = closed
            except Exception as exc:
                errors.append(f"{symbol} {timeframe}: {type(exc).__name__}: {exc}")

    signal_results = []
    if len(history) == len(SYMBOLS) * 2:
        for symbol in SYMBOLS:
            try:
                frames = {}
                for timeframe in ("1H", "4H"):
                    frame = pd.DataFrame(history[(symbol, timeframe)])
                    frame["open_time"] = pd.to_datetime(frame["timestamp_ms"], unit="ms", utc=True).dt.tz_localize(None)
                    frames[timeframe] = frame[["open_time", "open", "high", "low", "close"]]
                found = detect_signals(frames["4H"], frames["1H"])
                signal_results.append({"symbol": symbol, "status": "ok", "historical_signal_rows": len(found)})
            except Exception as exc:
                errors.append(f"{symbol} signal-rule execution: {type(exc).__name__}: {exc}")
                signal_results.append({"symbol": symbol, "status": "failed", "error": str(exc)})

    report = {
        "validation": "V12 batch 1+2 read-only validation",
        "read_only": True, "signal_state_written": False, "bark_sent": False,
        "scheduled_scanner_enabled": False, "data_source": "Bitget Mix USDT-FUTURES",
        "symbols_total": len(SYMBOLS), "history_checks": len(checks),
        "signal_rule_checks": len(signal_results), "errors": errors,
        "overall_status": "passed" if not errors and len(checks) == 54 and len(signal_results) == 27 else "failed",
        "started_at_utc": started.isoformat(),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "checks": checks, "signal_results": signal_results,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["overall_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
