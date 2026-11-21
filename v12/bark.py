"""V12 Bark transport, daily deduplication and conservative retry handling."""
import hashlib,json,os,sys,time,urllib.error,urllib.request
from datetime import datetime,timedelta
from email.utils import parsedate_to_datetime
from .config import (
    BARK_ENDPOINT,
    BARK_GROUP,
    BARK_PENDING_PATH,
    BARK_STATE_PATH,
    BARK_WATCHDOG_STATE_PATH,
    BARK_TIMEOUT_SECONDS,
    CORE_SYMBOLS,
    REPORT_PATH,
    STOCK_SYMBOLS,
    TIMEZONE,
)
from .state import atomic_write_json,load_json

class DeliveryError(RuntimeError):
    def __init__(self,message,uncertain=False,retryable=False,retry_after=None):
        super().__init__(message); self.uncertain=uncertain; self.retryable=retryable; self.retry_after=retry_after

def _norm(value):
    symbol=str(value or "").strip().upper()
    return symbol[:-4] if symbol.endswith("USDT") else symbol

# Bark delivers plain text and cannot detect the receiving device's screen width.
# Wrap whole symbol tokens at a conservative, fixed display-column width.
_BARK_LINE_WIDTH = 32
_BARK_TOKEN_SEPARATOR = "   "


def _wrap_symbol_units(symbols):
    lines = []
    current = ""
    for symbol in symbols:
        unit = f"▰ {symbol} ▰"
        candidate = unit if not current else current + _BARK_TOKEN_SEPARATOR + unit
        if current and len(candidate) > _BARK_LINE_WIDTH:
            lines.append(current)
            current = unit
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines) if lines else "▰ No change ▰"


def build_body(report):
    found={_norm(x.get("symbol")) for x in report.get("signals",[]) if isinstance(x,dict) and x.get("formal_signal") is True}
    stocks=[x for x in STOCK_SYMBOLS if _norm("SKHYUSDT" if x=="SKHYNIX" else x) in found]; core=[x for x in CORE_SYMBOLS if x in found]
    stock_text = _wrap_symbol_units(stocks)
    core_text = _wrap_symbol_units(core)
    body="美股巨头\n"+stock_text+"\n\n核心能源\n"+core_text
    return body,stocks+core

def _date(report):
    day=report.get("signal_day")
    if isinstance(day,str):
        try:return datetime.fromisoformat(day).date().isoformat()
        except ValueError:pass
    for key in ("started_at","finished_at"):
        try:
            stamp=datetime.fromisoformat(str(report[key]).replace("Z","+00:00"))
            if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=TIMEZONE)
            return stamp.astimezone(TIMEZONE).date().isoformat()
        except (KeyError,TypeError,ValueError):pass
    return datetime.now(TIMEZONE).date().isoformat()

def _load(path):
    value=load_json(path,{})
    return value if isinstance(value,dict) else {}

def _save_ok(fingerprint,day,path=None,slot=None):
    target=path or BARK_STATE_PATH
    state=_load(target)
    accepted_at=datetime.now(TIMEZONE).isoformat()
    deliveries=state.get("deliveries",{})
    if not isinstance(deliveries,dict):
        deliveries={}
    if slot:
        deliveries[slot]={"fingerprint":fingerprint,"date":day,"accepted_at":accepted_at}
    # Keep a bounded history; slot keys begin with YYYYMMDD.
    cutoff=(datetime.now(TIMEZONE).date()-timedelta(days=14)).isoformat().replace("-","")
    deliveries={key:value for key,value in deliveries.items()
        if not str(key[:8]).isdigit() or str(key[:8])>=cutoff}
    atomic_write_json(target,{"fingerprint":fingerprint,"date":day,"accepted_at":accepted_at,"deliveries":deliveries})

def _pending_queue():
    state=_load(BARK_PENDING_PATH)
    queued=state.get("pending")
    if isinstance(queued,list):
        return [item for item in queued if isinstance(item,dict)]
    return []

def _save_pending_queue(queue):
    atomic_write_json(BARK_PENDING_PATH,{"pending":queue})

def _pending(fingerprint,day,body,symbols,status="sending",error=None,*,kind="scan",retry_after=None,retry_attempts=None,next_retry_at=None,slot=None):
    queue=_pending_queue()
    previous=next((item for item in queue if item.get("fingerprint")==fingerprint),{})
    attempts=previous.get("retry_attempts",0) if retry_attempts is None else retry_attempts
    record={"fingerprint":fingerprint,"date":day,"body":body,"symbols":symbols,
        "kind":kind,"slot":slot,"status":status,"error":error,"retry_attempts":attempts,
        "retry_after":retry_after,"next_retry_at":next_retry_at,
        "updated_at":datetime.now(TIMEZONE).isoformat()}
    for index,item in enumerate(queue):
        if item.get("fingerprint")==fingerprint:
            queue[index]=record
            break
    else:
        queue.append(record)
    _save_pending_queue(queue)

def _clear(fingerprint=None):
    if fingerprint is None:
        _save_pending_queue([])
        return
    _save_pending_queue([item for item in _pending_queue() if item.get("fingerprint")!=fingerprint])

def _post(payload):
    request=urllib.request.Request(BARK_ENDPOINT,data=json.dumps(payload,ensure_ascii=False).encode(),
        headers={"Content-Type":"application/json","User-Agent":"VEGAS-V12/1.0"},method="POST")
    try:
        with urllib.request.urlopen(request,timeout=BARK_TIMEOUT_SECONDS) as response:
            raw=response.read().decode("utf-8",errors="replace")
            if not 200<=response.status<300: raise DeliveryError(f"Bark HTTP {response.status}",uncertain=True)
            if raw.strip():
                try: result=json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise DeliveryError("Bark response invalid; acceptance is uncertain",uncertain=True) from exc
                if isinstance(result,dict) and result.get("code") not in (None,200,"200"):
                    raise DeliveryError("Bark explicitly rejected notification")
            return
    except urllib.error.HTTPError as exc:
        if exc.code==429:
            raise DeliveryError("Bark rate limited",retryable=True,
                retry_after=exc.headers.get("Retry-After") if exc.headers else None) from exc
        if 400<=exc.code<500: raise DeliveryError(f"Bark rejected notification (HTTP {exc.code})") from exc
        raise DeliveryError(f"Bark server HTTP {exc.code}; acceptance is uncertain",uncertain=True) from exc
    except (urllib.error.URLError,TimeoutError,ConnectionError) as exc:
        raise DeliveryError(f"Bark network/timeout result is ambiguous: {exc}",uncertain=True) from exc

def _send(body,key):
    if not key: raise RuntimeError("Repository secret V12_BARK_KEY is missing.")
    _post({"device_key":key,"body":body,"group":BARK_GROUP,"level":"active"})

def send_watchdog_alert(body: str, key: str, alert_id=None) -> None:
    """Queue watchdog alerts through the same durable Bark transport as scan notices."""
    day=datetime.now(TIMEZONE).date().isoformat()
    identity=str(alert_id or hashlib.sha256(body.encode()).hexdigest())
    fingerprint=hashlib.sha256(f"watchdog\n{day}\n{identity}".encode()).hexdigest()
    if _accepted(fingerprint,day,BARK_WATCHDOG_STATE_PATH):
        print("Watchdog Bark alert already accepted; duplicate suppressed.")
        return
    previous=next((item for item in _pending_queue() if item.get("fingerprint")==fingerprint),None)
    if previous:
        print("Watchdog Bark alert already queued; duplicate suppressed.")
        return
    _pending(fingerprint,day,body,[],"sending",kind="watchdog")
    try:
        _send(body,key)
    except DeliveryError as exc:
        status="uncertain" if exc.uncertain else ("retryable" if exc.retryable else "rejected")
        attempts=1 if exc.retryable else 0
        due=_retry_at(exc.retry_after,attempts) if exc.retryable else None
        _pending(fingerprint,day,body,[],status,str(exc),kind="watchdog",
            retry_after=exc.retry_after,retry_attempts=attempts,next_retry_at=due)
        raise
    except Exception as exc:
        _pending(fingerprint,day,body,[],"rejected",str(exc),kind="watchdog")
        raise
    _save_ok(fingerprint,day,BARK_WATCHDOG_STATE_PATH)
    _clear(fingerprint)
    print("V12 Bark watchdog alert accepted.")


def send_test_notification():
    key=os.environ.get("V12_BARK_KEY","").strip()
    if not key:
        raise RuntimeError("Repository secret V12_BARK_KEY is missing.")
    _send("VEGAS V12 Bark 测试通知\n单次连通性测试，不代表正式信号。",key)
    print("Bark test notification accepted.")


def _state_path(kind="scan"):
    return BARK_WATCHDOG_STATE_PATH if kind == "watchdog" else BARK_STATE_PATH


def _accepted(fingerprint,day,path=None,slot=None):
    state=_load(path or BARK_STATE_PATH)
    if slot:
        deliveries=state.get("deliveries",{})
        record=deliveries.get(slot) if isinstance(deliveries,dict) else None
        return isinstance(record,dict) and record.get("date")==day and record.get("fingerprint")==fingerprint
    return state.get("date")==day and state.get("fingerprint")==fingerprint


def _retry_delay(value=None,attempt=1,now=None):
    """Honor Retry-After; otherwise use bounded exponential backoff."""
    if value is not None:
        try:
            return max(1,int(float(str(value).strip())))
        except (TypeError,ValueError,OverflowError):
            try:
                when=parsedate_to_datetime(str(value))
                if when.tzinfo is None:
                    from datetime import timezone
                    when=when.replace(tzinfo=timezone.utc)
                current=now or datetime.now(when.tzinfo)
                return max(1,int((when-current.astimezone(when.tzinfo)).total_seconds()))
            except (TypeError,ValueError,OverflowError):
                pass
    return min(3600,60*(2**max(0,int(attempt)-1)))


def _retry_at(retry_after=None,attempt=1):
    return (datetime.now(TIMEZONE)+timedelta(seconds=_retry_delay(retry_after,attempt))).isoformat()


def _retry_due(pending,now=None):
    value=pending.get("next_retry_at")
    if not value:
        return True
    try:
        due=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if due.tzinfo is None:
            due=due.replace(tzinfo=TIMEZONE)
        current=now or datetime.now(TIMEZONE)
        return current>=due.astimezone(TIMEZONE)
    except (TypeError,ValueError,OverflowError):
        return True


def retry_pending():
    queue=_pending_queue()
    if not queue:
        print("No pending V12 Bark notification."); return 0
    today=datetime.now(TIMEZONE).date().isoformat()
    remaining=[]; failed=False
    for index,pending in enumerate(queue):
        future=queue[index+1:]
        fingerprint=pending.get("fingerprint")
        if not fingerprint:
            _save_pending_queue(remaining+future)
            continue
        day=str(pending.get("date",""))
        if day!=today:
            print("Discarded stale V12 Bark retry.")
            _save_pending_queue(remaining+future)
            continue
        state_path=_state_path(pending.get("kind","scan"))
        if _accepted(fingerprint,day,state_path,pending.get("slot")):
            print("Already accepted; duplicate retry suppressed.")
            _save_pending_queue(remaining+future)
            continue
        status=pending.get("status","sending")
        if status=="sending":
            try:
                updated=datetime.fromisoformat(str(pending.get("updated_at","")).replace("Z","+00:00"))
                if updated.tzinfo is None:
                    updated=updated.replace(tzinfo=TIMEZONE)
                age=(datetime.now(TIMEZONE)-updated.astimezone(TIMEZONE)).total_seconds()
            except (TypeError,ValueError):
                age=1801
            if age>1800:
                pending=dict(pending)
                pending.update({"status":"uncertain","error":"Stale sending intent; delivery outcome requires manual reconciliation.","updated_at":datetime.now(TIMEZONE).isoformat()})
                print(f"ALERT: stale Bark sending intent requires manual review: {fingerprint}",file=sys.stderr)
                failed=True
            remaining.append(pending)
            _save_pending_queue(remaining+future)
            continue
        if status=="rejected":
            pending=dict(pending)
            pending.update({"status":"dead_letter","error":pending.get("error") or "Permanent Bark rejection.","updated_at":datetime.now(TIMEZONE).isoformat()})
            print(f"ALERT: rejected Bark notice archived: {fingerprint}",file=sys.stderr)
            remaining.append(pending)
            _save_pending_queue(remaining+future)
            failed=True
            continue
        if status!="retryable" or not _retry_due(pending):
            remaining.append(pending)
            _save_pending_queue(remaining+future)
            continue
        if int(pending.get("retry_attempts",0))>=5:
            pending=dict(pending)
            pending.update({"status":"dead_letter","error":"Maximum Bark retry attempts reached.","updated_at":datetime.now(TIMEZONE).isoformat()})
            print(f"ALERT: Bark retry moved to dead letter: {fingerprint}",file=sys.stderr)
            remaining.append(pending)
            _save_pending_queue(remaining+future)
            failed=True
            continue
        pending=dict(pending)
        pending.update({"status":"sending","updated_at":datetime.now(TIMEZONE).isoformat()})
        _save_pending_queue(remaining+[pending]+future)
        try:
            _send(str(pending.get("body","")),os.environ.get("V12_BARK_KEY","").strip())
        except DeliveryError as exc:
            pending=dict(pending)
            if exc.retryable:
                attempts=int(pending.get("retry_attempts",0))+1
                next_status="retryable" if attempts<5 else "dead_letter"
                pending.update({"status":next_status,"retry_attempts":attempts,
                    "retry_after":exc.retry_after,"next_retry_at":_retry_at(exc.retry_after,attempts)})
            else:
                pending.update({"status":"uncertain" if exc.uncertain else "rejected"})
            pending.update({"error":str(exc),"updated_at":datetime.now(TIMEZONE).isoformat()})
            remaining.append(pending)
            failed=True
            print(f"ERROR: V12 Bark retry failed: {exc}",file=sys.stderr)
        except Exception as exc:
            pending=dict(pending)
            pending.update({"status":"rejected","error":str(exc),"updated_at":datetime.now(TIMEZONE).isoformat()})
            remaining.append(pending)
            failed=True
            print(f"ERROR: V12 Bark retry failed: {exc}",file=sys.stderr)
        else:
            _save_ok(fingerprint,day,state_path,pending.get("slot"))
            print("V12 Bark retry accepted.")
        _save_pending_queue(remaining+future)
    return 1 if failed else 0

def _delivery_slot(report):
    """Map the exact GitHub schedule cron to its Beijing slot."""
    recovery=os.environ.get("V12_BARK_RECOVERY_SLOT","").strip()
    if recovery.startswith("recovery:"):
        return recovery.split(":",1)[1]
    cron=os.environ.get("V12_BARK_SCHEDULE_CRON","").strip()
    if os.environ.get("GITHUB_EVENT_NAME")=="schedule" and cron:
        fields=cron.split()
        if len(fields)==5 and fields[0]=="0" and fields[2:]==["*","*","*"]:
            try:
                utc_hour=int(fields[1])
                local_hour=(utc_hour+8)%24
                if local_hour in (8,10,16,20,22):
                    stamp=datetime.fromisoformat(str(report["started_at"]).replace("Z","+00:00"))
                    if stamp.tzinfo is None:
                        stamp=stamp.replace(tzinfo=TIMEZONE)
                    local_day=str(report.get("signal_day") or stamp.astimezone(TIMEZONE).date().isoformat()).replace("-","")
                    return f"{local_day}-{local_hour:02d}00"
            except (KeyError,TypeError,ValueError):
                pass
    run_id=os.environ.get("GITHUB_RUN_ID","").strip()
    if run_id:
        day=str(report.get("signal_day") or _date(report)).replace("-","")
        return f"{day}-manual-{run_id}"
    return str(report.get("started_at") or datetime.now(TIMEZONE).isoformat())


def _report_delivery(path):
    with open(path,encoding="utf-8") as stream:
        report=json.load(stream)
    if not isinstance(report,dict) or not isinstance(report.get("signals",[]),list):
        raise ValueError("Invalid V12 scan report")
    body,symbols=build_body(report)
    day=_date(report)
    slot=_delivery_slot(report)
    fingerprint=hashlib.sha256(f"{day}\\n{slot}\\n{body}".encode()).hexdigest()
    return body,symbols,day,slot,fingerprint


def prepare_report(path):
    """Write the delivery intent before the Bark POST is allowed to run."""
    try:
        body,symbols,day,slot,fingerprint=_report_delivery(path)
        if _accepted(fingerprint,day,slot=slot):
            print("V12 Bark already accepted for this slot; duplicate suppressed.")
            return 0
        previous=next((item for item in _pending_queue() if item.get("fingerprint")==fingerprint),None)
        if previous:
            print(f"V12 Bark delivery already recorded as {previous.get('status')}; refusing duplicate send.")
            return 1
        _pending(fingerprint,day,body,symbols,"sending",kind="scan",slot=slot)
        print(f"Prepared durable Bark intent for {slot}; delivery waits for GitHub commit.")
        return 0
    except Exception as exc:
        print(f"ERROR: V12 Bark prepare failed: {exc}",file=sys.stderr)
        return 1


def deliver_prepared(path):
    """Send only after the workflow has committed the sending intent."""
    try:
        body,symbols,day,slot,fingerprint=_report_delivery(path)
        if _accepted(fingerprint,day,slot=slot):
            _clear(fingerprint)
            print("V12 Bark already accepted; duplicate suppressed.")
            return 0
        intent=next((item for item in _pending_queue()
            if item.get("fingerprint")==fingerprint and item.get("status")=="sending"),None)
        if not intent:
            raise RuntimeError("Durable Bark sending intent missing; refusing to POST.")
        try:
            _send(body,os.environ.get("V12_BARK_KEY","").strip())
        except DeliveryError as exc:
            status="uncertain" if exc.uncertain else ("retryable" if exc.retryable else "rejected")
            attempts=1 if exc.retryable else 0
            due=_retry_at(exc.retry_after,attempts) if exc.retryable else None
            _pending(fingerprint,day,body,symbols,status,str(exc),kind="scan",
                retry_after=exc.retry_after,retry_attempts=attempts,next_retry_at=due,slot=slot)
            raise
        except Exception as exc:
            _pending(fingerprint,day,body,symbols,"rejected",str(exc),kind="scan",slot=slot)
            raise
        _save_ok(fingerprint,day,slot=slot)
        _clear(fingerprint)
        print("V12 Bark accepted; "+(", ".join(symbols) if symbols else "No change"))
        return 0
    except Exception as exc:
        print(f"ERROR: V12 Bark notification failed: {exc}",file=sys.stderr)
        return 1


def send_report(path):
    try:
        with open(path,encoding="utf-8") as stream: report=json.load(stream)
        if not isinstance(report,dict) or not isinstance(report.get("signals",[]),list):
            raise ValueError("Invalid V12 scan report")
        body,symbols=build_body(report); day=_date(report)
        slot=_delivery_slot(report)
        fingerprint=hashlib.sha256(f"{day}\n{slot}\n{body}".encode()).hexdigest()
        if _accepted(fingerprint,day,slot=slot):
            _clear(fingerprint)
            print("V12 Bark content unchanged; duplicate notification suppressed."); return 0
        previous=next((item for item in _pending_queue() if item.get("fingerprint")==fingerprint),None)
        if previous and previous.get("status") in ("sending","uncertain","retryable","rejected","dead_letter"):
            print("Matching Bark delivery is unresolved; duplicate send suppressed."); return 1
        _pending(fingerprint,day,body,symbols,"sending",slot=slot)
        try:_send(body,os.environ.get("V12_BARK_KEY","").strip())
        except DeliveryError as exc:
            status="uncertain" if exc.uncertain else ("retryable" if exc.retryable else "rejected")
            attempts=1 if exc.retryable else 0
            due=_retry_at(exc.retry_after,attempts) if exc.retryable else None
            _pending(fingerprint,day,body,symbols,status,str(exc),kind="scan",
                retry_after=exc.retry_after,retry_attempts=attempts,next_retry_at=due,slot=slot); raise
        except Exception as exc:
            _pending(fingerprint,day,body,symbols,"rejected",str(exc),kind="scan"); raise
        _save_ok(fingerprint,day,slot=slot); _clear(fingerprint)
        print("V12 Bark accepted; "+(", ".join(symbols) if symbols else "No change")); return 0
    except Exception as exc:
        print(f"ERROR: V12 Bark notification failed: {exc}",file=sys.stderr); return 1


def _github_request(path,method="GET",payload=None):
    repo=os.environ.get("GITHUB_REPOSITORY","").strip()
    token=os.environ.get("GH_TOKEN","").strip()
    if repo.count("/")!=1 or not token:
        raise RuntimeError("GITHUB_REPOSITORY and GH_TOKEN are required to process Watchdog alerts.")
    url=f"https://api.github.com/repos/{repo}{path}"
    data=None if payload is None else json.dumps(payload).encode()
    headers={"Accept":"application/vnd.github+json","Authorization":"Bearer "+token,
        "X-GitHub-Api-Version":"2022-11-28"}
    if payload is not None: headers["Content-Type"]="application/json"
    request=urllib.request.Request(url,data=data,headers=headers,method=method)
    with urllib.request.urlopen(request,timeout=20) as response:
        raw=response.read()
        return json.loads(raw) if raw else None


def process_watchdog_issues():
    """Send and close Watchdog alert issues only from the serialized Bark retry worker."""
    issues=_github_request("/issues?state=open&per_page=100") or []
    key=os.environ.get("V12_BARK_KEY","").strip()
    failed=False
    for issue in issues:
        if issue.get("pull_request") or issue.get("title")!="[V12 Watchdog] Scan health alert":
            continue
        number=issue.get("number")
        if not number:
            continue
        day=datetime.now(TIMEZONE).date().isoformat()
        fingerprint=hashlib.sha256(f"watchdog\n{day}\n{number}".encode()).hexdigest()
        if _accepted(fingerprint,day,BARK_WATCHDOG_STATE_PATH):
            _github_request(f"/issues/{number}","PATCH",{"state":"closed"})
            print(f"Watchdog alert issue #{number} closed after Bark acceptance.")
            continue
        queued=any(item.get("fingerprint")==fingerprint for item in _pending_queue())
        if not queued:
            try:
                send_watchdog_alert(str(issue.get("body") or "V12 watchdog scan health alert"),key,number)
            except Exception as exc:
                failed=True
                print(f"ERROR: Watchdog Bark alert #{number} remains queued or failed: {exc}",file=sys.stderr)
                continue
        if _accepted(fingerprint,day,BARK_WATCHDOG_STATE_PATH):
            _github_request(f"/issues/{number}","PATCH",{"state":"closed"})
            print(f"Watchdog alert issue #{number} closed after Bark acceptance.")
        else:
            print(f"Watchdog alert issue #{number} remains open until Bark acceptance.")
    return 1 if failed else 0

if __name__=="__main__":
    if len(sys.argv)>1 and sys.argv[1]=="--prepare-report":
        sys.exit(prepare_report(sys.argv[2] if len(sys.argv)>2 else REPORT_PATH))
    if len(sys.argv)>1 and sys.argv[1]=="--deliver-prepared":
        sys.exit(deliver_prepared(sys.argv[2] if len(sys.argv)>2 else REPORT_PATH))
    if len(sys.argv)>1 and sys.argv[1]=="--retry-pending":
        sys.exit(retry_pending())
    if len(sys.argv)>1 and sys.argv[1]=="--process-watchdog-issues":
        try:
            sys.exit(process_watchdog_issues())
        except Exception as exc:
            print(f"ERROR: Watchdog Bark issue processing failed: {exc}",file=sys.stderr)
            sys.exit(1)
    if len(sys.argv)>1 and sys.argv[1]=="--test":
        try:
            send_test_notification()
            sys.exit(0)
        except Exception as exc:
            print(f"ERROR: Bark test failed: {exc}",file=sys.stderr)
            sys.exit(1)
    sys.exit(send_report(sys.argv[1] if len(sys.argv)>1 else REPORT_PATH))
