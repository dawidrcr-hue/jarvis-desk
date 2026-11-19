#!/usr/bin/env python3
"""Publisher (PAPER): builds a public-safe data.json from jarvis-paper state and publishes the site to the
existing GitHub Pages repo used by the old desk (/workspace/jarvis-desk/site -> dawikoro-tech/jarvis-desk).
No wallet data, no keys/secrets. Paper desk only."""
import os, sys, time, json, hashlib, subprocess, shutil, collections, signal
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import P, load, save, cfg, log, heartbeat, now_iso
from lib.strategy import portfolio_equity, position_mark, trail_tiers, ladder_note_pl
from lib import safety

C = "publisher"
# DESK_SITE = repo root (GitHub Actions checkout). Falls back to the old box clone.
SITE = os.environ.get("DESK_SITE") or "/workspace/jarvis-desk/site"   # git repo -> GitHub Pages
ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))
NOTES_KEEP = 300
STATIC = ["index.html", "manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png", "apple-touch-icon.png"]
PUBLISH_S = 60
AG = [("watchdog", "Watchdog", "Pilnuje procesów, restartuje zawieszone", 120),
      ("scanner", "Scanner + Analityk", "Zbiera coiny z DexScreenera, Jupitera, Raydiuma, GeckoTerminala i ocenia momentum (0–100)", 120),
      ("filter", "Filtr", "Odrzuca coiny spoza zakresu kapitalizacji (config), bez trendu wzrostowego, z małą płynnością; loguje powody", 120),
      ("trader", "Trader (papier)", "Symuluje kupno/sprzedaż z poślizgiem i opłatami, pilnuje drabinki stopu co 3 s", 45),
      ("publisher", "Publisher", "Aktualizuje tę stronę co minutę (GitHub Actions)", 120)]
REASON_PL = {"mcap<40000": "kapitalizacja < $40 tys.", "mcap>2000000": "kapitalizacja > $2 mln", "liq<20000": "płynność < $20 tys.",
             "vol_h1<5000": "wolumen 1h < $5 tys.", "pair_too_new": "para młodsza niż 30 min", "downtrend_m5": "spadek 5m",
             "downtrend_h1": "spadek 1h", "downtrend_h6": "spadek 6h", "sells>buys_m5": "przewaga sprzedających 5m",
             "sells>buys_h1": "przewaga sprzedających 1h", "low_activity_m5": "< 10 transakcji w 5m", "volume_fading": "wolumen słabnie",
             "overextended_m5": "pionowa świeca 5m (> +25%)", "overextended_h1": "przegrzany 1h (> +300%)", "score<50": "ocena analityka < 50",
             "already_held": "już w portfelu", "reentry_cooldown": "niedawno sprzedany (60 min)", "no_price": "brak ceny",
             "rug_flag": "RugCheck: oznaczony jako rug", "rug_mint_authority": "RugCheck: mint nie odebrany",
             "rug_freeze_authority": "RugCheck: freeze nie odebrany", "rug_token_ext": "RugCheck: niebezpieczne rozszerzenia tokena",
             "rug_transfer_fee": "RugCheck: opłata od transferu", "rug_no_market": "RugCheck: brak puli",
             "rug_lp_unlocked": "RugCheck: LP niezablokowane", "rug_top10": "RugCheck: top 10 holderów za dużo",
             "rug_single_holder": "RugCheck: jeden holder za dużo", "rug_creator": "RugCheck: twórca trzyma za dużo",
             "rug_score": "RugCheck: wysoki wynik ryzyka", "rug_danger": "RugCheck: ryzyko „danger”",
             "rugcheck_unavailable": "RugCheck niedostępny (kupno pominięte)", "rugcheck_deferred": "RugCheck: limit sprawdzeń w rundzie"}

def reason_pl(c):
    fc = c.get("filter", {}); rp = dict(REASON_PL)
    rp[f"score<{fc.get('min_score')}"] = f"ocena analityka < {fc.get('min_score')}"
    if fc.get("max_mcap_usd"): rp[f"mcap>{fc['max_mcap_usd']:.0f}"] = f"kapitalizacja > ${fc['max_mcap_usd']/1e3:.0f} tys."
    if fc.get("min_liquidity_usd") is not None:
        liq = fc["min_liquidity_usd"]
        rp[f"liq<{liq:.0f}"] = f"płynność < ${liq/1e3:.0f} tys."
    return rp

def tail_jsonl(path, n):
    try:
        with open(path) as f: return [json.loads(l) for l in collections.deque(f, n)]
    except Exception: return []

def build():
    c = cfg(); pf = load(P("state", "portfolio.json"), {}) or {}; rcc = safety.conf(c)
    pos, closed = pf.get("positions", []), pf.get("closed", [])
    now = time.time()
    eq = portfolio_equity(pf, now=now, cfg=c)
    wins = sum(1 for t in closed if t["pnl_usd"] > 0)
    costs = sum(t["cost_usd"] - t["qty"] * t["entry_price"] + t["qty"] * t["exit_price"] - t["proceeds"] for t in closed)
    hist = load(P("state", "equity_history.json"), []) or []
    if not hist or now - hist[-1]["t"] >= 60:
        hist.append({"t": round(now), "e": round(eq, 4)}); hist = hist[-3000:]; save(P("state", "equity_history.json"), hist)
    step = max(1, len(hist) // 300); eh = hist[::step]
    if eh[-1] != hist[-1]: eh.append(hist[-1])
    keep_p = ("symbol", "mint", "url", "entry_iso", "cost_usd", "entry_price", "last_price", "last_gain", "peak_gain", "stop_pct",
              "entry_mcap", "entry_liq", "score", "last_quote_iso")
    keep_c = ("symbol", "mint", "url", "entry_iso", "exit_iso", "cost_usd", "entry_price", "exit_price", "gross_pct", "pnl_pct",
              "pnl_usd", "peak_gain", "reason", "hold_min", "entry_mcap")
    fl = [x for x in tail_jsonl(P("logs", "filter.log"), 80) if x.get("msg") == "snapshot filtered"]
    f = fl[-1] if fl else {}
    rc = collections.Counter()
    for k, v in (f.get("reject_counts") or {}).items(): rc[k] += v
    cand = load(P("state", "candidates.json"), {}) or {}
    snap_ts = cand.get("ts", 0)
    dec = [d for d in tail_jsonl(P("logs", "decisions.jsonl"), 1500) if d.get("decision") == "REJECT"]
    seen, near = set(), []
    for d in sorted(dec[-700:], key=lambda d: (len(d["reasons"]), -d.get("score", 0))):
        if d["mint"] in seen or any(r.startswith(("mcap<", "liq<", "already_held")) for r in d["reasons"]): continue
        seen.add(d["mint"]); near.append({k: d.get(k) for k in ("symbol", "score", "mcap", "liq", "chg_m5", "chg_h1", "reasons")})
        if len(near) >= 12: break
    wd = load(P("state", "watchdog.json"), {}) or {}
    agents = []
    for key, name, desc, tmo in AG:
        hb = load(P("state", "hb", f"{key}.json"), {}) or {}
        age = now - hb.get("ts", 0)
        agents.append({"key": key, "name": name, "desc": desc, "age_s": round(age), "ok": age < tmo,
                       "restarts": (wd.get("restarts") or {}).get(key, 0)})
    agents[-1].update(age_s=0, ok=True)
    return {
        "generated": now_iso(), "mode": "PAPER",
        "stats": {"equity": round(eq, 4), "start": pf.get("start_balance", 100), "cash": round(pf.get("cash", 0), 4),
                  "realized": round(sum(t["pnl_usd"] for t in closed), 4),
                  "unrealized": round(sum(position_mark(p, now=now, cfg=c) - p["cost_usd"] for p in pos), 4),
                  "open": len(pos), "closed": len(closed), "wins": wins, "win_rate": wins / len(closed) if closed else None,
                  "avg_pnl_pct": sum(t["pnl_pct"] for t in closed) / len(closed) if closed else None, "costs_paid": round(costs, 4)},
        "rules": {"size_pct": c["position_pct_of_equity"], "max_open": c["max_open_positions"], "initial_stop": c["initial_stop"],
                  "ladder": c["ladder"], "trail_after": c.get("trail_after"), "trail_gap": c.get("trail_gap"), "trail_tiers": trail_tiers(c), "ladder_note_pl": ladder_note_pl(c), "min_mcap": c["filter"]["min_mcap_usd"], "max_mcap": c["filter"].get("max_mcap_usd"),
                  "filter": {k: c["filter"].get(k) for k in ("min_liquidity_usd", "min_vol_h1_usd", "min_pair_age_min", "min_m5_txns", "max_m5_change_pct", "max_h1_change_pct", "min_score")},
                  "rugcheck": {k: rcc.get(k) for k in ("enabled", "max_top10_pct", "max_single_holder_pct", "max_creator_pct", "min_lp_locked_pct", "max_score_normalised")}},
        "positions": [{k: p.get(k) for k in keep_p} for p in pos],
        "closed": [{k: t.get(k) for k in keep_c} for t in closed[-60:][::-1]],
        "equity_history": eh,
        "filter": {"ts": f.get("ts"), "n": f.get("n"), "passed": f.get("passed"), "reject_counts": dict(rc)},
        "rugcheck_recent": [{k: r.get(k) for k in ("ts", "symbol", "mint", "ok", "cached", "reasons", "summary", "err")}
                            for r in tail_jsonl(P("logs", "filter.log"), 400) if r.get("msg") == "rugcheck"][-12:][::-1],
        "reason_pl": reason_pl(c), "near": near,
        "sources": {"counts": cand.get("source_counts"), "discovered": cand.get("n_discovered"), "watch": cand.get("n_watch"),
                    "priced": len(cand.get("candidates", [])), "ts": snap_ts},
        "agents": agents,
    }

def git(*a, t=60):
    return subprocess.run(["git", "-C", SITE, *a], timeout=t, capture_output=True, text=True)

def publish(data, force=False):
    for n in STATIC:
        sp = P("site", n)
        if os.path.isfile(sp): shutil.copy(sp, os.path.join(SITE, n))
    save(os.path.join(SITE, "data.json"), data)
    core = {k: v for k, v in data.items() if k not in ("generated", "agents", "equity_history")}
    h = hashlib.sha1(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()
    st = load(P("state", "publisher.json"), {}) or {}
    if not force and h == st.get("hash") and time.time() - st.get("ts", 0) < 300:
        return "unchanged"
    trim_notes()
    git("add", "-A")
    if git("diff", "--cached", "--quiet").returncode == 0: return "no diff"
    git("commit", "-qm", f"desk paper {time.strftime('%H:%M:%S')}" + (" [actions]" if ACTIONS else ""))
    r = git("push", "-q", "origin", "HEAD:main")
    if r.returncode != 0:
        git("fetch", "-q", "origin", "main"); git("merge", "-X", "ours", "--no-edit", "FETCH_HEAD", "-m", "sync remote")
        r = git("push", "-q", "origin", "HEAD:main")
        if r.returncode != 0: return f"push failed: {(r.stderr or '')[-200:]}"
    save(P("state", "publisher.json"), {"hash": h, "ts": time.time(), "iso": now_iso()})
    return "published"

def trim_notes():
    """Keep notify/notes.jsonl (Polish trade notes for the box relay) to the last NOTES_KEEP lines."""
    fp = P("notify", "notes.jsonl")
    try:
        with open(fp) as f: lines = f.readlines()
    except FileNotFoundError: return
    if len(lines) > NOTES_KEEP:
        tmp = fp + ".tmp"
        with open(tmp, "w") as f: f.writelines(lines[-NOTES_KEEP:])
        os.replace(tmp, fp)

def _alarm(*_): raise TimeoutError("timeout")

def main():
    signal.signal(signal.SIGALRM, _alarm)
    log(C, "publisher started", pid=os.getpid()); first = True
    while True:
        heartbeat(C, status="publishing")
        signal.alarm(150)
        try: res = publish(build(), first); first = False
        except Exception as e: res = f"error: {repr(e)[:150]}"
        finally: signal.alarm(0)
        if res not in ("unchanged", "no diff"): log(C, res)
        heartbeat(C, status=res)
        time.sleep(PUBLISH_S)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        print(json.dumps(build(), ensure_ascii=False, default=str)[:3000])
    elif len(sys.argv) > 1 and sys.argv[1] == "once":
        print(publish(build(), True))
    else:
        main()
