"""Optional webhook ping for copy BUY/SELL notes (signal only; the desk itself never trades or signs).
URL and Authorization value come ONLY from env LIVE_COPY_WEBHOOK_URL / LIVE_COPY_WEBHOOK_AUTH (GitHub Actions secrets).
Missing -> silently skipped. Never prints/logs the URL or the auth value. Runs in a daemon thread (never blocks polling):
5 s timeout, up to 2 retries."""
import json, os, threading, time, urllib.request

def enabled():
    return bool(os.environ.get("LIVE_COPY_WEBHOOK_URL"))

def payload(note):
    d = note.get("data") or {}
    side = "buy" if note.get("type") == "copy_buy" else "sell"
    return {"note_id": note.get("id"), "side": side, "ticker": d.get("ticker"), "mint": d.get("mint"),
            "wallet": d.get("wallet"), "alloc_pct": d.get("alloc_pct"), "ts": note.get("ts")}

def _post(body, url, auth, log, retries=2, timeout=5, sleep=time.sleep):
    data = json.dumps(body).encode()
    for attempt in range(retries + 1):
        try:
            h = {"Content-Type": "application/json", "User-Agent": "jarvis-copy-desk/1.0"}
            if auth: h["Authorization"] = auth
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method="POST"), timeout=timeout) as r:
                if 200 <= r.status < 300:
                    log("webhook sent", note_id=body.get("note_id"), side=body.get("side"), status=r.status); return True
                code = r.status
        except Exception as e:                       # log only the exception type / HTTP code, never the URL
            code = getattr(e, "code", None) or type(e).__name__
        if attempt < retries: sleep(1.5 * (attempt + 1))
    log("webhook failed", note_id=body.get("note_id"), side=body.get("side"), err=str(code)); return False

def send(note, log, sync=False):
    if note.get("type") not in ("copy_buy", "copy_sell"): return None
    url = os.environ.get("LIVE_COPY_WEBHOOK_URL")
    if not url: return None
    auth = os.environ.get("LIVE_COPY_WEBHOOK_AUTH") or ""
    body = payload(note)
    if sync: return _post(body, url, auth, log)
    t = threading.Thread(target=_post, args=(body, url, auth, log), daemon=True); t.start()
    return t
