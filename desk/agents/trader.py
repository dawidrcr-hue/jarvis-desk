#!/usr/bin/env python3
"""PAPER trader. No wallet, no signing - only simulated fills on public prices.
- consumes signals/approved/, re-quotes, re-checks rules, sizes = position_pct_of_equity * current equity
- polls open positions every price_poll_s and applies the trailing stop ladder
- stop monitoring runs on the main thread and is never blocked by scanner, filter, or entry requotes
- single writer of state/portfolio.json; trade log logs/trades.jsonl; Polish notes in notify/pending"""
import os, sys, time, json, glob, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import P, load, save, log, heartbeat, cfg, notify, now_iso, HTTP_TIMEOUT
from lib.market import pair_prices, pairs_for_mints
from lib.strategy import features, filter_reasons, sim_buy, sim_sell, position_size, ladder_update, ladder_note_pl, dead_liquidity_reason, portfolio_equity, dead_exit, DEAD_REASON, norm_symbol, entries_allowed

LOCK = threading.Lock()

def new_pf(c):
    return {"mode": "paper", "start_balance": c["start_balance_usd"], "cash": c["start_balance_usd"],
            "positions": [], "closed": [], "created": now_iso()}

def equity(pf, c=None):
    return portfolio_equity(pf, cfg=c)

def trade_log(rec):
    with open(P("logs", "trades.jsonl"), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

def fmt(x): return f"${x:,.2f}".replace(",", " ")

def close(pf, c, pos, price, liq, reason):
    if reason == DEAD_REASON:
        d = dead_exit(pos)
        s = {"proceeds": d["proceeds"], "fill": d["fill"], "slip": d["slip"]}
        pnl, gross, price = d["pnl_usd"], d["gross_pct"], 0.0
    else:
        s = sim_sell(pos["qty"], price, liq or pos["entry_liq"], c["costs"])
        pnl = s["proceeds"] - pos["cost_usd"]
        gross = price / pos["entry_price"] - 1
    rec = {**pos, "exit_ts": time.time(), "exit_iso": now_iso(), "exit_price": price, "exit_fill": s["fill"], "exit_slip": s["slip"],
           "proceeds": s["proceeds"], "pnl_usd": pnl, "pnl_pct": pnl / pos["cost_usd"], "gross_pct": gross,
           "reason": reason, "hold_min": (time.time() - pos["entry_ts"]) / 60}
    pf["cash"] += s["proceeds"]
    pf["positions"] = [p for p in pf["positions"] if p["id"] != pos["id"]]
    pf["closed"].append(rec)
    trade_log({"ts": now_iso(), "side": "SELL", **{k: rec[k] for k in ("id", "mint", "symbol", "entry_price", "exit_price", "exit_fill", "exit_slip", "proceeds", "pnl_usd", "pnl_pct", "gross_pct", "peak_gain", "reason", "hold_min")}})
    notify("paper_sell", f"PAPIER: sprzedaż ${pos['symbol']} {rec['pnl_pct']*100:+.1f}%",
           f"[PAPIER – bez prawdziwych pieniędzy] Sprzedaż ${pos['symbol']}: {reason}. Ruch ceny {rec['gross_pct']*100:+.1f}% "
           f"(szczyt {pos['peak_gain']*100:+.1f}%), wynik netto po kosztach {fmt(pnl)} ({rec['pnl_pct']*100:+.1f}%). "
           f"Czas w pozycji {rec['hold_min']:.0f} min. Equity papierowe: {fmt(equity(pf, c))}.",
           {"mint": pos["mint"], "pos_id": pos["id"], "pnl_usd": pnl})
    log("trader", "paper SELL", symbol=pos["symbol"], reason=reason, pnl=round(pnl, 4))

def signal_loop(pf):
    """Background: consume approved signals. A hung requote must not block stop polling on the main thread."""
    while True:
        try:
            files = sorted(glob.glob(P("signals", "approved", "*.json")))
            for f in files:
                c = cfg()
                sig = load(f)
                try:
                    if os.path.isdir(P("signals", "done")):
                        os.replace(f, P("signals", "done", os.path.basename(f)))
                    else:
                        os.remove(f)
                except FileNotFoundError:
                    continue
                if not sig:
                    continue
                with LOCK:
                    why = _enter_locked(pf, c, sig)
                if why:
                    log("trader", "entry skipped", symbol=(sig.get("cand") or {}).get("symbol"), reason=why)
        except Exception as e:
            log("trader", "signal loop error", err=repr(e)[:300])
        time.sleep(0.5)

def _enter_locked(pf, c, sig):
    """Must be called with LOCK held. HTTP requote runs with LOCK released inside."""
    return _try_enter_two_phase(pf, c, sig)

def _try_enter_two_phase(pf, c, sig):
    """LOCK held on entry and exit. HTTP runs with LOCK released."""
    cand = sig["cand"]; mint = cand["mint"]
    if not entries_allowed(c): return "entries_closed"
    if time.time() - sig["ts"] > 300: return "stale_signal"
    if any(p["mint"] == mint for p in pf["positions"]): return "already_held"
    sym = norm_symbol(cand.get("symbol"))
    if sym and any(norm_symbol(p.get("symbol")) == sym for p in pf["positions"]):
        return "already_held"
    if len(pf["positions"]) >= c["max_open_positions"]: return "max_positions"
    LOCK.release()
    q = None
    err = None
    try:
        q = pairs_for_mints([mint], deadline=time.time() + HTTP_TIMEOUT).get(mint)
    except Exception as e:
        err = e
    LOCK.acquire()
    if err:
        return "no_requote"
    if not q: return "no_requote"
    # Re-check after the wait — a stop may have closed something, or another entry filled.
    if not entries_allowed(c): return "entries_closed"
    if any(p["mint"] == mint for p in pf["positions"]): return "already_held"
    if sym and any(norm_symbol(p.get("symbol")) == sym for p in pf["positions"]):
        return "already_held"
    if len(pf["positions"]) >= c["max_open_positions"]: return "max_positions"
    ft = features(q)
    cool_s = c["reentry_cooldown_min"] * 60
    cool = {t["mint"] for t in pf["closed"] if time.time() - t.get("exit_ts", 0) < cool_s}
    cool_sym = {norm_symbol(t.get("symbol")) for t in pf["closed"] if time.time() - t.get("exit_ts", 0) < cool_s}
    held = {p["mint"] for p in pf["positions"]}
    held_sym = {norm_symbol(p.get("symbol")) for p in pf["positions"]}
    if norm_symbol(ft.get("symbol")) and norm_symbol(ft.get("symbol")) in held_sym and mint not in held:
        return "already_held"
    rs = filter_reasons(ft, c["filter"], held, cool, held_sym, cool_sym)
    if rs: return "requote_reject: " + "; ".join(rs)
    eq = equity(pf, c)
    size = position_size(eq, pf["cash"], c)
    if size <= 0: return "no_cash"
    b = sim_buy(size, ft["price"], ft["liq"], c["costs"])
    pos = {"id": f"{mint[:6]}-{int(time.time())}", "mint": mint, "symbol": ft["symbol"], "pair": ft["pair"], "url": ft["url"],
           "entry_ts": time.time(), "entry_iso": now_iso(), "entry_price": ft["price"], "entry_fill": b["fill"],
           "qty": b["qty"], "cost_usd": size, "entry_slip": b["slip"], "entry_mcap": ft["mcap"], "entry_liq": ft["liq"],
           "score": cand.get("score"), "stop_pct": c["initial_stop"], "peak_gain": 0.0, "last_price": ft["price"],
           "equity_at_entry": eq, "safety_note": (sig.get("safety") or {}).get("note_pl")}
    pf["cash"] -= size; pf["positions"].append(pos)
    trade_log({"ts": now_iso(), "side": "BUY", **{k: pos[k] for k in ("id", "mint", "symbol", "entry_price", "entry_fill", "qty", "cost_usd", "entry_slip", "entry_mcap", "entry_liq", "score")}})
    pct = int(round(c["position_pct_of_equity"] * 100))
    notify("paper_buy", f"PAPIER: kupno ${ft['symbol']} za {fmt(size)}",
           f"[PAPIER – bez prawdziwych pieniędzy] Kupno ${ft['symbol']} za {fmt(size)} ({pct}% equity {fmt(eq)}). "
           f"Kapitalizacja ~{fmt(ft['mcap'])}, płynność ~{fmt(ft['liq'])}, zmiana 5m {ft['chg_m5']:+.1f}%, 1h {ft['chg_h1']:+.1f}%, "
           f"wynik analityka {cand.get('score')}. Poślizg {b['slip']*100:.1f}%. {ladder_note_pl(c)}. "
           + (f"{pos['safety_note']}. " if pos.get("safety_note") else "") + f"{ft['url']}",
           {"mint": mint, "pos_id": pos["id"]})
    log("trader", "paper BUY", symbol=ft["symbol"], mint=mint, size=size, price=ft["price"], slip=b["slip"])
    save(P("state", "portfolio.json"), pf)
    return None

def poll_positions(pf, last_poll):
    """Main-thread stop check. HTTP runs with LOCK released. Returns updated last_poll time."""
    c = cfg()
    with LOCK:
        nopen = len(pf["positions"])
        heartbeat("trader", status="ok", open=nopen, last_poll=now_iso())
        if not pf["positions"] or time.time() - last_poll < c["price_poll_s"]:
            return last_poll
        pairs = [p["pair"] for p in pf["positions"]]
        poll_started = time.time()
    quotes = {}
    try:
        quotes = pair_prices(pairs, deadline=time.time() + HTTP_TIMEOUT)
    except Exception as e:
        log("trader", "quote error", err=repr(e)[:300])
    now = time.time()
    with LOCK:
        for pos in list(pf["positions"]):
            q = quotes.get(pos["pair"])
            if not q or not q.get("priceUsd"):
                pos["missed_quotes"] = pos.get("missed_quotes", 0) + 1
                reason = dead_liquidity_reason(pos, quote=None, now=now, cfg=c)
                if reason:
                    close(pf, c, pos, 0.0, 0.0, reason)
                continue
            price = float(q["priceUsd"]); liq = float((q.get("liquidity") or {}).get("usd") or 0)
            pos["last_liq"] = liq; pos["last_quote_iso"] = now_iso(); pos["last_quote_ts"] = now
            pos["missed_quotes"] = 0
            reason = dead_liquidity_reason(pos, quote=q, now=now, cfg=c)
            if reason:
                close(pf, c, pos, 0.0, liq, reason)
                continue
            ex, reason = ladder_update(pos, price, c)
            if ex: close(pf, c, pos, price, liq, reason)
        pf["equity"] = equity(pf, c); pf["updated"] = now_iso()
        save(P("state", "portfolio.json"), pf)
        heartbeat("trader", status="ok", open=len(pf["positions"]), last_poll=now_iso())
    return poll_started

def main():
    c = cfg()
    pf = load(P("state", "portfolio.json")) or new_pf(c)
    save(P("state", "portfolio.json"), pf)
    heartbeat("trader", status="ok", open=len(pf.get("positions") or []))
    threading.Thread(target=signal_loop, args=(pf,), name="paper-signals", daemon=True).start()
    last_poll = 0
    while True:
        try:
            last_poll = poll_positions(pf, last_poll)
        except Exception as e:
            log("trader", "loop error", err=repr(e)[:300])
            heartbeat("trader", status="error")
        time.sleep(0.25)

if __name__ == "__main__":
    main()
