#!/usr/bin/env python3
"""Watchdog: starts scanner/filter/trader, restarts crashed or hung ones (stale heartbeat). PAPER desk."""
import os, sys, time, subprocess, signal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.common import P, load, save, log, heartbeat, notify, now_iso

AGENTS = {"scanner": 120, "filter": 120, "trader": 45, "publisher": 120, "copybook": 120}  # heartbeat timeout (s); trader is tighter so stops cannot stall long
procs, started, restarts = {}, {}, {}

def start(name):
    lf = open(P("logs", f"{name}.out"), "a")
    procs[name] = subprocess.Popen([sys.executable, P("agents", f"{name}.py")], cwd=P(), stdout=lf, stderr=lf, start_new_session=True)
    started[name] = time.time(); log("watchdog", f"started {name}", pid=procs[name].pid)

def kill(name):
    p = procs.get(name)
    if p and p.poll() is None:
        try: os.killpg(p.pid, signal.SIGKILL)
        except Exception: p.kill()
        p.wait(timeout=10)

def stop_all(*_):
    for n in list(procs): kill(n)
    sys.exit(0)

def main():
    signal.signal(signal.SIGTERM, stop_all)
    save(P("state", "watchdog.pid"), {"pid": os.getpid(), "started": now_iso()})
    log("watchdog", "watchdog started", pid=os.getpid())
    for n in AGENTS: start(n)
    while True:
        now = time.time()
        for n, tmo in AGENTS.items():
            p = procs[n]; hb = load(P("state", "hb", f"{n}.json"), {})
            reason = None
            if p.poll() is not None: reason = f"proces zakończony (kod {p.returncode})"
            elif now - started[n] > tmo and (hb.get("pid") != p.pid or now - hb.get("ts", 0) > tmo):
                reason = f"brak heartbeatu od {int(now - hb.get('ts', 0))} s"
            if reason:
                kill(n); restarts[n] = restarts.get(n, 0) + 1
                log("watchdog", f"restart {n}: {reason}")
                if restarts[n] in (3, 10, 30):
                    notify("watchdog", f"PAPIER: watchdog restartuje {n} ({restarts[n]}×)", f"Ostatni powód: {reason}")
                start(n)
        save(P("state", "watchdog.json"), {"restarts": restarts, "pids": {n: p.pid for n, p in procs.items()}, "updated": now_iso()})
        heartbeat("watchdog", status="ok")
        time.sleep(5)

if __name__ == "__main__":
    main()
