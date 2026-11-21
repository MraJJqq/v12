"""V12 scan-only watchdog. It recovers failed scans/publication without Bark coupling."""
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import SCAN_HOURS_BEIJING, TIMEZONE, WATCHDOG_GRACE_MINUTES

RECOVERY_LOCK_TIMEOUT_MINUTES = 15
FAILED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required"}


def _base():
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if repository.count("/") != 1:
        raise RuntimeError("GITHUB_REPOSITORY must be owner/repository")
    return "https://api.github.com/repos/" + repository


def _headers():
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GH_TOKEN required")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(url, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = _headers()
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def _issue(items, title):
    return next((item for item in items if not item.get("pull_request") and item.get("title") == title), None)


def _created(item):
    return datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))


def _scan_state(api, run):
    run_id = run.get("id")
    if not run_id:
        raise RuntimeError("Completed V12 scan run has no id")
    payload = _request(api + f"/actions/runs/{run_id}/jobs?per_page=100") or {}
    steps = {}
    for job in payload.get("jobs", []):
        for step in job.get("steps", []) or []:
            name = str(step.get("name", "")).strip()
            if name:
                steps[name] = step.get("conclusion") or step.get("status")
    scan = steps.get("Scan Bitget history")
    persist = steps.get("Persist signal state")
    publish = steps.get("Publish complete signal snapshot to relay")
    if scan == "success" and persist == "success" and publish == "success":
        return "complete"
    if scan in FAILED_CONCLUSIONS:
        return "scan_failed"
    if scan == "success" and (persist in FAILED_CONCLUSIONS or publish in FAILED_CONCLUSIONS):
        return "publish_failed"
    if scan == "success" and (persist in (None, "skipped") or publish in (None, "skipped")):
        return "publish_unconfirmed"
    return "unknown"


def _later_success(runs, failed, slot_end, marker):
    start = _created(failed)
    for item in runs:
        if item is failed or item.get("status") != "completed" or item.get("conclusion") != "success":
            continue
        created = _created(item)
        if created <= start:
            continue
        if item.get("event") == "schedule" and created < slot_end:
            return True
        if item.get("event") == "workflow_dispatch" and marker in (item.get("display_title") or ""):
            return True
    return False


def _close_lock(api, issues, lock):
    _request(api + f"/issues/{lock['number']}", "PATCH", {"state": "closed"})
    issues[:] = [item for item in issues if item.get("number") != lock.get("number")]


def _dispatch(api, issues, marker, slot, lock=None):
    title = f"[V12 Scan Recovery Lock] {slot}"
    if lock is None:
        lock = _request(api + "/issues", "POST", {
            "title": title,
            "body": f"Durable V12 scan recovery lock: {marker}. Created before dispatch to prevent duplicate recovery runs.",
        })
        if not lock or "number" not in lock:
            raise RuntimeError("Could not create V12 scan recovery lock")
        issues.append(lock)
    try:
        _request(api + "/actions/workflows/v12-bitget-scan.yml/dispatches", "POST",
                 {"ref": "main", "inputs": {"recovery_slot": marker}})
        return f"Beijing {slot[-4:-2]}:00 scan recovery dispatched."
    except Exception as exc:
        return f"Beijing {slot[-4:-2]}:00 recovery dispatch ambiguous; lock retained: {exc}"


def main(now=None):
    api = _base()
    run_payload = _request(api + "/actions/workflows/v12-bitget-scan.yml/runs?per_page=100") or {}
    runs = run_payload.get("workflow_runs", [])
    issues = _request(api + "/issues?state=open&per_page=100") or []
    current = (now or datetime.now(timezone.utc)).astimezone(TIMEZONE)
    problems, pending = [], []

    for index, hour in enumerate(SCAN_HOURS_BEIJING):
        due = current.replace(hour=hour, minute=0, second=0, microsecond=0)
        if current < due + timedelta(minutes=WATCHDOG_GRACE_MINUTES):
            continue
        next_due = (
            due.replace(hour=SCAN_HOURS_BEIJING[index + 1])
            if index + 1 < len(SCAN_HOURS_BEIJING)
            else (due + timedelta(days=1)).replace(hour=SCAN_HOURS_BEIJING[0])
        )
        utc_start, utc_end = due.astimezone(timezone.utc), next_due.astimezone(timezone.utc)
        slot = due.strftime("%Y%m%d-%H%M")
        marker = "recovery:" + slot
        lock = _issue(issues, f"[V12 Scan Recovery Lock] {slot}")
        matches = []
        for run in runs:
            created = _created(run)
            if run.get("event") == "schedule" and utc_start - timedelta(minutes=WATCHDOG_GRACE_MINUTES) <= created < utc_end - timedelta(minutes=WATCHDOG_GRACE_MINUTES):
                matches.append(run)
            elif run.get("event") == "workflow_dispatch" and marker in (run.get("display_title") or ""):
                matches.append(run)

        if not matches:
            if lock:
                age = current.astimezone(timezone.utc) - _created(lock)
                if age < timedelta(minutes=RECOVERY_LOCK_TIMEOUT_MINUTES):
                    pending.append(f"Beijing {hour:02d}:00 recovery lock active; awaiting GitHub run registration.")
                else:
                    _close_lock(api, issues, lock)
                    problems.append(f"Beijing {hour:02d}:00 recovery dispatch was not registered within {RECOVERY_LOCK_TIMEOUT_MINUTES} minutes.")
                continue
            pending.append(_dispatch(api, issues, marker, slot))
            continue

        failed_matches = [
            item for item in matches
            if item.get("status") == "completed" and item.get("conclusion") != "success"
        ]
        if any(_later_success(runs, item, utc_end, marker) for item in failed_matches):
            if lock:
                _close_lock(api, issues, lock)
            pending.append(f"Beijing {hour:02d}:00 later successful run superseded the earlier failure.")
            continue

        latest = max(matches, key=lambda item: item["created_at"])
        is_recovery = latest.get("event") == "workflow_dispatch"
        if latest.get("status") != "completed":
            pending.append(f"Beijing {hour:02d}:00 scan is running.")
            continue
        if lock:
            _close_lock(api, issues, lock)

        state = _scan_state(api, latest)
        if state == "complete":
            pending.append(f"Beijing {hour:02d}:00 scan and relay publication succeeded.")
        elif state in ("scan_failed", "publish_failed"):
            if not is_recovery:
                pending.append(_dispatch(api, issues, marker, slot))
            else:
                problems.append(f"Beijing {hour:02d}:00 recovery did not complete: run #{latest.get('run_number')} / {latest.get('conclusion')}.")
        else:
            problems.append(f"Beijing {hour:02d}:00 run #{latest.get('run_number')} has no conclusive scan/publication state; automatic recovery suppressed.")

    alert = _issue(issues, "[V12 Scan Watchdog] Health alert")
    if problems and not alert:
        detail = "\n".join("- " + problem for problem in problems)
        created = _request(api + "/issues", "POST", {
            "title": "[V12 Scan Watchdog] Health alert",
            "body": "V12 scheduled scan health issue.\n\n" + detail,
        })
        print(created.get("html_url", "V12 scan watchdog issue created"))
    for message in pending:
        print(message)
    if not problems:
        print("No failed due V12 scans detected.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
