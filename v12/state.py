import json
import os
import tempfile
from typing import Any

def load_json(path:str,default:Any)->Any:
    try:
        with open(path,encoding="utf-8") as f:return json.load(f)
    except (OSError,json.JSONDecodeError):return default

def atomic_write_json(path: str, value: Any) -> None:
    """Write JSON atomically using a unique temporary file beside the target."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise

def load_signal_state(path:str)->dict[str,Any]:
    x=load_json(path,{})
    return x if isinstance(x,dict) else {}

def save_signal_state(path:str,state:dict[str,Any])->None:atomic_write_json(path,state)

def merge_daily_signals(existing:dict[str,Any],new:list[dict[str,Any]],day:str)->dict[str,Any]:
    old=existing.get("signals",[]) if existing.get("date")==day else []
    keyed={}
    for x in [*(old if isinstance(old,list) else []),*new]:
        if not isinstance(x,dict) or x.get("formal_signal") is not True:continue
        try:key=(str(x["symbol"]).upper(),int(x["candle_timestamp_ms"]),str(x["filter_side"]))
        except (KeyError,TypeError,ValueError,OverflowError):continue
        if key[0] and key[2] and key[1]>=0:keyed[key]=x
    return {"date":day,"signals":sorted(keyed.values(),key=lambda x:(x["symbol"],int(x["candle_timestamp_ms"]),x["filter_side"]))}
