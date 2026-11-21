#!/usr/bin/env python3
"""Conflict-safe semantic merge for V12 workflow JSON state files."""
import json
import os
import sys
from datetime import datetime


def read_json(path):
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def stamp(value):
    if not value:
        return float('-inf')
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            from datetime import timezone
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return float("-inf")


def signal_state(local, remote):
    result = {**remote, **local}
    a = local.get("__daily_formal_signals__", {})
    b = remote.get("__daily_formal_signals__", {})
    if not isinstance(a, dict): a = {}
    if not isinstance(b, dict): b = {}
    day = max(str(a.get("date", "")), str(b.get("date", "")))
    if day:
        records = []
        for source in (b, a):
            if source.get("date") == day and isinstance(source.get("signals"), list):
                records.extend(source["signals"])
        unique = {}
        for item in records:
            if not isinstance(item, dict): continue
            try:
                key = (str(item["symbol"]).upper(), int(item["candle_timestamp_ms"]), str(item["filter_side"]))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            unique[key] = item
        result["__daily_formal_signals__"] = {
            "date": day,
            "signals": sorted(unique.values(), key=lambda x: (
                str(x.get("symbol", "")), int(x.get("candle_timestamp_ms", 0)), str(x.get("filter_side", ""))
            )),
        }
    versions = [x for x in (remote.get("__rule_version__"), local.get("__rule_version__")) if isinstance(x, dict)]
    if versions:
        result["__rule_version__"] = max(versions, key=lambda x: int(x.get("version", 0) or 0))
    return result


def merge(path, local, remote):
    if path.endswith(".signal-state.json"):
        return signal_state(local, remote)
    if path.endswith("public-signals.json"):
        return max((remote, local), key=lambda x: (
            stamp(x.get("finished_at")), stamp(x.get("started_at"))
        ))
    return {**remote, **local}


def main():
    if len(sys.argv) != 4:
        raise SystemExit("Usage: merge_v12_state_json.py <repo-path> <local-json> <remote-json>")
    destination, local_path, remote_path = sys.argv[1:]
    local, remote = read_json(local_path), read_json(remote_path)
    merged = merge(destination, local, remote)
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    temporary = destination + ".merge-tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(merged, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
    os.replace(temporary, destination)


if __name__ == "__main__":
    main()
