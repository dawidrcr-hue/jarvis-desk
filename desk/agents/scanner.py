#!/usr/bin/env python3
"""Scanner + analyst: discovers Solana mints, pulls DexScreener pair data, computes features + score,
writes state/candidates.json. Keeps a rolling watchlist of in-range tokens so they get re-checked."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import P, load, save, log, heartbeat, cfg, now_iso
from lib.market import discover_mints, pairs_for_mints, select_mints_to_price, annotate_and_trim
from lib.strategy import features, score

WATCH_TTL = 6 * 3600
WATCH_MAX = 400

def cycle():
    c = cfg()
    budget = float(c.get("scan_budget_s", 70))
    t0 = time.time()
    deadline = t0 + budget
    def hb(stage):
        heartbeat("scanner", status=stage, elapsed=round(time.time() - t0, 1))

    hb("discover")
    wl = load(P("state", "watchlist.json"), {}) or {}
    now = time.time()
    # Leave ~half the budget (min 20s) for pricing after discovery.
    discover_deadline = min(deadline, t0 + max(20.0, budget * 0.55))
    found, src_stats = discover_mints(deadline=discover_deadline, on_progress=hb)
    t_disc = time.time()
    for m, src in found.items():
        e = wl.setdefault(m, {"first_seen": now, "sources": []})
        e["last_seen"] = now; e["sources"] = sorted(set(e.get("sources") or []) | set(src))
    to_price = select_mints_to_price(wl, set(found), now, c)
    hb("price")
    pairs = pairs_for_mints(to_price, deadline=deadline, on_progress=hb)
    t_price = time.time()
    cands = []
    priced_meta = {}
    for m, p in pairs.items():
        ft = features(p); ft["score"] = score(ft); ft["sources"] = (wl.get(m) or {}).get("sources") or []
        cands.append(ft)
        priced_meta[m] = {"mcap": ft.get("mcap") or 0, "liq": ft.get("liq") or 0}
    cands.sort(key=lambda x: -x["score"])
    wl = annotate_and_trim(wl, priced_meta, set(found), set(to_price), time.time(), c,
                           watch_max=WATCH_MAX, watch_ttl=WATCH_TTL)
    save(P("state", "watchlist.json"), wl)
    dur = time.time() - t0
    save(P("state", "candidates.json"), {"ts": time.time(), "iso": now_iso(), "n_discovered": len(found),
                                         "n_watch": len(wl), "source_counts": src_stats, "candidates": cands,
                                         "duration_s": round(dur, 2)})
    log("scanner", "scan done", discovered=len(found), watch=len(wl), priced=len(cands),
        priced_of=len(to_price), duration_s=round(dur, 2),
        discover_s=round(t_disc - t0, 2), price_s=round(t_price - t_disc, 2),
        sources=src_stats, top=[(x["symbol"], x["score"]) for x in cands[:5]])

def main():
    while True:
        heartbeat("scanner", status="scanning")
        try: cycle()
        except Exception as e: log("scanner", "cycle error", err=repr(e)[:300])
        heartbeat("scanner", status="idle")
        time.sleep(cfg().get("scan_interval_s", 15))

if __name__ == "__main__":
    main()
