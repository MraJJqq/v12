import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tools.merge_v12_state_json import merge


class StateMergeTests(unittest.TestCase):
    def test_daily_signal_state_unions_same_day_records(self):
        local = {"__daily_formal_signals__": {"date": "2026-11-21", "signals": [
            {"symbol": "BTCUSDT", "candle_timestamp_ms": 200, "filter_side": "long"}
        ]}}
        remote = {"__daily_formal_signals__": {"date": "2026-11-21", "signals": [
            {"symbol": "ETHUSDT", "candle_timestamp_ms": 100, "filter_side": "short"}
        ]}}
        result = merge("v12/.signal-state.json", local, remote)
        self.assertEqual(len(result["__daily_formal_signals__"]["signals"]), 2)

    def test_newer_signal_day_resets_previous_day_records(self):
        local = {"__daily_formal_signals__": {"date": "2026-11-22", "signals": []}}
        remote = {"__daily_formal_signals__": {"date": "2026-11-21", "signals": [
            {"symbol": "BTCUSDT", "candle_timestamp_ms": 100, "filter_side": "long"}
        ]}}
        result = merge("v12/.signal-state.json", local, remote)
        self.assertEqual(result["__daily_formal_signals__"], {"date": "2026-11-22", "signals": []})

    def test_bark_slot_deliveries_are_merged_without_losing_other_slots(self):
        local = {"deliveries": {"20261121-1000": {"fingerprint": "b", "date": "2026-11-21", "accepted_at": "2026-11-21T10:00:00+08:00"}}}
        remote = {"deliveries": {"20261121-0800": {"fingerprint": "a", "date": "2026-11-21", "accepted_at": "2026-11-21T08:00:00+08:00"}}}
        result = merge("v12/.bark-notification-state.json", local, remote)
        self.assertEqual(set(result["deliveries"]), {"20261121-0800", "20261121-1000"})

    def test_pending_queue_merges_by_fingerprint_and_latest_update(self):
        local = {"pending": [{"fingerprint": "a", "status": "sending", "updated_at": "2026-11-21T10:00:00+08:00"}]}
        remote = {"pending": [
            {"fingerprint": "a", "status": "retryable", "updated_at": "2026-11-21T09:00:00+08:00"},
            {"fingerprint": "b", "status": "retryable", "updated_at": "2026-11-21T09:00:00+08:00"}
        ]}
        result = merge("v12/.bark-pending-notification-state.json", local, remote)
        self.assertEqual({x["fingerprint"] for x in result["pending"]}, {"a", "b"})
        self.assertEqual(next(x for x in result["pending"] if x["fingerprint"] == "a")["status"], "sending")

    def test_public_feed_handles_missing_timestamps(self):
        result = merge("v12/public-signals.json", {"signals": ["local"]}, {"signals": ["remote"]})
        self.assertIn(result["signals"], (["local"], ["remote"]))

    def test_public_feed_keeps_newest_scan_snapshot(self):
        local = {"finished_at": "2026-11-21T10:00:00+08:00", "signals": ["new"]}
        remote = {"finished_at": "2026-11-21T08:00:00+08:00", "signals": ["old"]}
        result = merge("v12/public-signals.json", local, remote)
        self.assertEqual(result["signals"], ["new"])


    def test_persistence_helper_resolves_real_concurrent_signal_state_conflict(self):
        repository_root = os.path.dirname(os.path.dirname(__file__))
        with tempfile.TemporaryDirectory() as folder:
            remote = os.path.join(folder, "remote.git")
            seed = os.path.join(folder, "seed")
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            subprocess.run(["git", "--git-dir", remote, "symbolic-ref", "HEAD", "refs/heads/main"], check=True, capture_output=True)
            os.makedirs(os.path.join(seed, "tools"))
            os.makedirs(os.path.join(seed, "v12"))
            for name in ("persist_v12_state.sh", "merge_v12_state_json.py"):
                shutil.copy(os.path.join(repository_root, "tools", name), os.path.join(seed, "tools", name))
            base_state = {"__daily_formal_signals__": {"date": "2026-11-21", "signals": []}}
            with open(os.path.join(seed, "v12", ".signal-state.json"), "w", encoding="utf-8") as stream:
                json.dump(base_state, stream)
            def git(cwd, *args):
                subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
            git(seed, "init")
            git(seed, "config", "user.name", "test")
            git(seed, "config", "user.email", "test@example.com")
            git(seed, "add", ".")
            git(seed, "commit", "-m", "base")
            git(seed, "branch", "-M", "main")
            git(seed, "remote", "add", "origin", remote)
            git(seed, "push", "-u", "origin", "main")
            local = os.path.join(folder, "local")
            concurrent = os.path.join(folder, "concurrent")
            subprocess.run(["git", "clone", remote, local], check=True, capture_output=True)
            subprocess.run(["git", "clone", remote, concurrent], check=True, capture_output=True)
            for cwd in (local, concurrent):
                git(cwd, "config", "user.name", "test")
                git(cwd, "config", "user.email", "test@example.com")
            local_path = os.path.join(local, "v12", ".signal-state.json")
            concurrent_path = os.path.join(concurrent, "v12", ".signal-state.json")
            local_state = {"__daily_formal_signals__": {"date": "2026-11-21", "signals": [
                {"symbol": "BTCUSDT", "candle_timestamp_ms": 100, "filter_side": "long"}
            ]}}
            concurrent_state = {"__daily_formal_signals__": {"date": "2026-11-21", "signals": [
                {"symbol": "AMDUSDT", "candle_timestamp_ms": 200, "filter_side": "short"}
            ]}}
            with open(local_path, "w", encoding="utf-8") as stream:
                json.dump(local_state, stream)
            with open(concurrent_path, "w", encoding="utf-8") as stream:
                json.dump(concurrent_state, stream)
            git(concurrent, "add", "v12/.signal-state.json")
            git(concurrent, "commit", "-m", "concurrent scan")
            git(concurrent, "push", "origin", "main")
            persisted = subprocess.run(
                ["bash", "tools/persist_v12_state.sh", "merge local scan", "v12/.signal-state.json"],
                cwd=local, check=True, capture_output=True, text=True,
            )
            subprocess.run(["git", "fetch", "origin", "main"], cwd=local, check=True, capture_output=True)
            merged_raw = subprocess.run(
                ["git", "show", "origin/main:v12/.signal-state.json"],
                cwd=local, check=True, capture_output=True, text=True,
            ).stdout
            merged = json.loads(merged_raw)["__daily_formal_signals__"]["signals"]
            self.assertEqual({item["symbol"] for item in merged}, {"BTCUSDT", "AMDUSDT"}, f"persist stdout={persisted.stdout}; stderr={persisted.stderr}; merged={merged_raw}")

    def test_persistence_script_is_valid_bash(self):
        script = os.path.join(os.path.dirname(__file__), "..", "tools", "persist_v12_state.sh")
        subprocess.run(["bash", "-n", script], check=True)


if __name__ == "__main__":
    unittest.main()
