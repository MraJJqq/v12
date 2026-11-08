"""V12 Bark transport, daily deduplication and conservative retry handling."""
import hashlib,json,os,sys,time,urllib.error,urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime
from .config import (
    BARK_ENDPOINT,
    BARK_GROUP,
    BARK_PENDING_PATH,
    BARK_STATE_PATH,
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
    return "\\n".join(lines) if lines else "▰ No change ▰"


def build_body(report):
    found={_norm(x.get("symbol")) for x in report.get("signals",[]) if isinstance(x,dict) and x.get("formal_signal") is True}
    stocks=[x for x in STOCK_SYMBOLS if _norm("SKHYUSDT" if x=="SKHYNIX" else x) in found]; core=[x for x in CORE_SYMBOLS if x in found]
    stock_text = _wrap_symbol_units(stocks)
    core_text = _wrap_symbol_units(core)
    body="美股巨头\\n"+stock_text+"\\n\\n核心能源\\n"+core_text
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

def _save_ok(fingerprint,day):
    atomic_write_json(BARK_STATE_PATH,{"version":2,"fingerprint":fingerprint,"date":day,"accepted_at":datetime.now(TIMEZONE).isoformat()})

def _pending(fingerprint,day,body,symbols,status="sending",error=None):
    atomic_write_json(BARK_PENDING_PATH,{"version":2,"fingerprint":fingerprint,"date":day,"body":body,"symbols":symbols,"status":status,"error":error,"updated_at":datetime.now(TIMEZONE).isoformat()})

def _clear():
    atomic_write_json(BARK_PENDING_PATH,{"version":2,"pending":None})

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

def send_watchdog_alert(body: str, key: str) -> None:
    """Send a watchdog alert through the same Bark transport as scan notices."""
    _send(body, key)


def _accepted(fingerprint,day):
    state=_load(BARK_STATE_PATH)
    return state.get("date")==day and state.get("fingerprint")==fingerprint

def _retry_delay(value):
    if not value:return 60
    try:return min(300,max(1,int(float(value))))
    except (TypeError,ValueError):
        try:
            when=parsedate_to_datetime(value)
            return min(300,max(1,int((when-datetime.now(when.tzinfo)).total_seconds())))
        except (TypeError,ValueError,OverflowError):return 60

def retry_pending():
    pending=_load(BARK_PENDING_PATH); fingerprint=pending.get("fingerprint")
    if not fingerprint:
        print("No pending V12 Bark notification."); return 0
    day=str(pending.get("date",""))
    if day!=datetime.now(TIMEZONE).date().isoformat():
        _clear(); print("Discarded stale V12 Bark retry."); return 0
    if _accepted(fingerprint,day):
        _clear(); print("Already accepted; duplicate retry suppressed."); return 0
    status=pending.get("status","sending")
    if status in ("sending","uncertain"):
        print("Bark outcome is ambiguous; automatic resend suppressed to prevent duplicate notifications."); return 0
    if status!="retryable":
        print("Pending Bark notification is not safe for automatic retry."); return 0
    try:
        _send(str(pending.get("body","")),os.environ.get("V12_BARK_KEY","").strip())
        _save_ok(fingerprint,day); _clear(); return 0
    except DeliveryError as exc:
        next_status="uncertain" if exc.uncertain else ("retryable" if exc.retryable else "rejected")
        _pending(fingerprint,day,str(pending.get("body","")),list(pending.get("symbols",[])),next_status,str(exc))
        if exc.retryable: time.sleep(_retry_delay(exc.retry_after))
        print(f"ERROR: V12 Bark retry failed: {exc}",file=sys.stderr); return 1
    except Exception as exc:
        _pending(fingerprint,day,str(pending.get("body","")),list(pending.get("symbols",[])),"rejected",str(exc))
        print(f"ERROR: V12 Bark retry failed: {exc}",file=sys.stderr); return 1

def send_report(path):
    try:
        with open(path,encoding="utf-8") as stream: report=json.load(stream)
        if not isinstance(report,dict) or not isinstance(report.get("signals",[]),list):
            raise ValueError("Invalid V12 scan report")
        body,symbols=build_body(report); day=_date(report)
        scan_id=str(report.get("started_at") or datetime.now(TIMEZONE).isoformat())
        fingerprint=hashlib.sha256(f"{day}\n{scan_id}\n{body}".encode()).hexdigest()
        if _accepted(fingerprint,day):
            print("V12 Bark content unchanged; duplicate notification suppressed."); return 0
        previous=_load(BARK_PENDING_PATH)
        if previous.get("fingerprint")==fingerprint and previous.get("status") in ("sending","uncertain"):
            print("Matching Bark delivery is unresolved; duplicate send suppressed."); return 1
        _pending(fingerprint,day,body,symbols,"sending")
        try:_send(body,os.environ.get("V12_BARK_KEY","").strip())
        except DeliveryError as exc:
            status="uncertain" if exc.uncertain else ("retryable" if exc.retryable else "rejected")
            _pending(fingerprint,day,body,symbols,status,str(exc)); raise
        except Exception as exc:
            _pending(fingerprint,day,body,symbols,"rejected",str(exc)); raise
        _save_ok(fingerprint,day); _clear()
        print("V12 Bark accepted; "+(", ".join(symbols) if symbols else "No change")); return 0
    except Exception as exc:
        print(f"ERROR: V12 Bark notification failed: {exc}",file=sys.stderr); return 1

if __name__=="__main__":
    sys.exit(retry_pending() if len(sys.argv)>1 and sys.argv[1]=="--retry-pending" else send_report(sys.argv[1] if len(sys.argv)>1 else REPORT_PATH))
