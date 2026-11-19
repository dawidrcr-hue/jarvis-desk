"""Shared helpers: paths, atomic JSON, logging, heartbeats, notifications, HTTP."""
import json, os, time, uuid, urllib.request, urllib.error, datetime, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)
_lock = threading.Lock()
HTTP_TIMEOUT = 8

def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False, default=str)
        f.flush()
        try: os.fsync(f.fileno())
        except Exception: pass
    os.replace(tmp, path)

def cfg():
    return load(P("config.json"), {})

def log(component, msg, **kw):
    line = {"ts": now_iso(), "c": component, "msg": msg, **kw}
    s = json.dumps(line, ensure_ascii=False, default=str)
    with _lock:
        with open(P("logs", f"{component}.log"), "a") as f:
            f.write(s + "\n")
        with open(P("logs", "activity.jsonl"), "a") as f:
            f.write(s + "\n")

def heartbeat(component, **status):
    save(P("state", "hb", f"{component}.json"), {"ts": time.time(), "iso": now_iso(), "pid": os.getpid(), **status})

def notify(kind, title, message, data=None):
    """Queue a user notification (Polish) for the parent agent to relay."""
    ev = {"id": uuid.uuid4().hex[:12], "ts": now_iso(), "type": kind, "title": title, "message": message, "data": data or {}}
    name = f"{time.time_ns()}_{kind}_{ev['id']}.json"
    save(P("notify", "pending", name), ev)
    # Append-only feed committed to the repo (desk/notify/notes.jsonl) so the box can relay notes from GitHub.
    with _lock:
        with open(P("notify", "notes.jsonl"), "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    log("notify", f"{kind}: {title}", id=ev["id"])
    return ev

def push_signal(sig):
    sig.setdefault("id", uuid.uuid4().hex[:12])
    sig.setdefault("created_at", now_iso())
    name = f"{int(time.time()*1000)}_{sig['source']}_{sig['id']}.json"
    save(P("signals", "inbox", name), sig)
    return sig

UA = {"User-Agent": "Mozilla/5.0 jarvis-paper/1.0", "Accept": "application/json"}

def http_json(url, data=None, timeout=None, headers=None, deadline=None):
    """GET/POST JSON. Default timeout 8s, or remaining time until deadline, whichever is shorter. No long 429 backoff."""
    if timeout is None:
        timeout = HTTP_TIMEOUT
    if deadline is not None:
        remain = deadline - time.time()
        if remain <= 0:
            raise TimeoutError("scan deadline")
        timeout = min(timeout, max(0.2, remain))
    h = dict(UA); h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def b58decode_len(s):
    n = 0
    for ch in s:
        i = B58.find(ch)
        if i < 0: return -1
        n = n * 58 + i
    full = (n.bit_length() + 7) // 8
    return full + (len(s) - len(s.lstrip("1")))

def is_solana_address(s):
    return 32 <= len(s) <= 44 and b58decode_len(s) == 32
