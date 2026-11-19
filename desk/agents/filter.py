#!/usr/bin/env python3
"""Filter: applies hard rules to each new candidates snapshot, logs every decision with reasons
(logs/decisions.jsonl), and queues passes into signals/approved/ for the paper trader."""
import os, sys, time, json, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import P, load, save, log, heartbeat, cfg, now_iso
from lib.strategy import filter_reasons, norm_symbol

def decision_log_many(recs):
    if not recs:
        return
    with open(P("logs", "decisions.jsonl"), "a") as f:
        for rec in recs:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

def main():
    last_ts = 0
    while True:
        heartbeat("filter", status="ok")
        try:
            snap = load(P("state", "candidates.json"), {})
            if snap.get("ts", 0) > last_ts:
                last_ts = snap["ts"]; c = cfg()
                heartbeat("filter", status="filtering", n=len(snap.get("candidates", [])))
                pf = load(P("state", "portfolio.json"), {})
                held = {p["mint"] for p in pf.get("positions", [])}
                held_symbols = {norm_symbol(p.get("symbol")) for p in pf.get("positions", [])}
                cool_s = c.get("reentry_cooldown_min", 60) * 60
                cooldown = {t["mint"] for t in pf.get("closed", []) if time.time() - t.get("exit_ts", 0) < cool_s}
                cooldown_symbols = {norm_symbol(t.get("symbol")) for t in pf.get("closed", []) if time.time() - t.get("exit_ts", 0) < cool_s}
                # Signals already queued, not yet filled, also occupy a mint/ticker slot.
                for f in glob.glob(P("signals", "approved", "*.json")):
                    cand = (load(f) or {}).get("cand") or {}
                    if cand.get("mint"): held.add(cand["mint"])
                    if cand.get("symbol"): held_symbols.add(norm_symbol(cand["symbol"]))
                npass = 0; stats = {}; buf = []
                cands = snap.get("candidates", [])
                for i, ft in enumerate(cands, 1):
                    if i % 80 == 0:
                        heartbeat("filter", status="filtering", n=i)
                        decision_log_many(buf); buf = []
                    rs = filter_reasons(ft, c["filter"], held, cooldown, held_symbols, cooldown_symbols)
                    for r in rs: stats[r.split(" ")[0]] = stats.get(r.split(" ")[0], 0) + 1
                    buf.append({"ts": now_iso(), "mint": ft["mint"], "symbol": ft["symbol"], "score": ft["score"],
                                "mcap": round(ft["mcap"]), "liq": round(ft["liq"]), "chg_m5": ft["chg_m5"], "chg_h1": ft["chg_h1"],
                                "decision": "PASS" if not rs else "REJECT", "reasons": rs})
                    if not rs:
                        npass += 1
                        held.add(ft["mint"])
                        if ft.get("symbol"): held_symbols.add(norm_symbol(ft["symbol"]))
                        save(P("signals", "approved", f"{int(time.time()*1000)}_{ft['mint']}.json"), {"ts": time.time(), "cand": ft})
                decision_log_many(buf)
                log("filter", "snapshot filtered", n=len(cands), passed=npass, reject_counts=stats)
                heartbeat("filter", status="ok")
        except Exception as e:
            log("filter", "error", err=repr(e)[:300])
        time.sleep(5)

if __name__ == "__main__":
    main()
