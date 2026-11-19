#!/usr/bin/env python3
"""GitHub Actions runner for the PAPER desk (no wallet, no keys, no secrets).
- restores persisted state from desk/persist/, starts watchdog.py (scanner, filter, trader, publisher)
- publisher commits data.json + desk state to main about once a minute (GITHUB_TOKEN from actions/checkout)
- after DESK_RUN_SECONDS (default 5h40m) stops everything, saves state, makes a final commit and exits 0;
  the next queued scheduled run (same concurrency group) resumes from the committed state."""
import os, sys, time, shutil, signal, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.common import P, log, now_iso, save

RUN_S = int(os.environ.get("DESK_RUN_SECONDS", str(5 * 3600 + 40 * 60)))
PERSIST = ["watchlist.json"]          # big, change every scan -> committed only at shutdown
for d in ("logs", "state/hb", "notify/pending", "notify/sent", "signals/approved", "signals/done", "tmp", "persist"):
    os.makedirs(P(*d.split("/")), exist_ok=True)

def restore():
    for n in PERSIST:
        src = P("persist", n)
        if os.path.isfile(src) and not os.path.isfile(P("state", n)):
            shutil.copy(src, P("state", n))

def persist():
    for n in PERSIST:
        if os.path.isfile(P("state", n)): shutil.copy(P("state", n), P("persist", n))

def trim_logs():
    fp = P("logs", "decisions.jsonl")
    try:
        if os.path.getsize(fp) > 30_000_000:
            with open(fp, "rb") as f: f.seek(-5_000_000, 2); tail = f.read()
            with open(fp, "wb") as f: f.write(tail[tail.find(b"\n") + 1:])
    except FileNotFoundError: pass

def main():
    t0 = time.time(); restore()
    save(P("state", "run.json"), {"run_id": os.environ.get("GITHUB_RUN_ID"), "started": now_iso(), "planned_s": RUN_S})
    log("runner", "actions run start", run_id=os.environ.get("GITHUB_RUN_ID"), planned_s=RUN_S)
    wd = subprocess.Popen([sys.executable, P("watchdog.py")], cwd=P(), stdout=open(P("logs", "watchdog.out"), "a"), stderr=subprocess.STDOUT)
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    n = 0
    while not stop["flag"] and time.time() - t0 < RUN_S:
        time.sleep(5); n += 1
        if wd.poll() is not None:
            log("runner", "watchdog died, restarting", code=wd.returncode)
            wd = subprocess.Popen([sys.executable, P("watchdog.py")], cwd=P(), stdout=open(P("logs", "watchdog.out"), "a"), stderr=subprocess.STDOUT)
        if n % 60 == 0:
            trim_logs(); persist() if n % 720 == 0 else None   # watchlist snapshot every hour too
    log("runner", "actions run stopping", ran_s=round(time.time() - t0))
    wd.terminate()
    try: wd.wait(timeout=30)
    except subprocess.TimeoutExpired: wd.kill()
    subprocess.run(["pkill", "-f", P("agents") + "/"], check=False)
    persist()
    lock = os.path.join(os.environ.get("DESK_SITE", os.path.dirname(P())), ".git", "index.lock")
    if os.path.exists(lock): os.remove(lock)
    r = subprocess.run([sys.executable, P("agents", "publisher.py"), "once"], cwd=P(), capture_output=True, text=True, timeout=180)
    print("final publish:", r.stdout.strip(), r.stderr.strip()[-300:])

if __name__ == "__main__":
    main()
