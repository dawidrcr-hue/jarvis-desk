#!/usr/bin/env python3
"""PAPER copy book of a public wallet (default @unipcs). Simulation only: no wallet, no keys, no signing, no Phantom.
Watches the target's public transactions via a public Solana RPC (no API key) and mirrors them on its own paper
book (state/copybook.json, own start balance). Prices/liquidity from DexScreener public API. Notes go to the same
notify pipeline (desk/notify/notes.jsonl) with prefix "[PAPIER – kopia <label>]"."""
import os, sys, time, json, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import P, load, save, log, heartbeat, cfg, notify, now_iso, http_json
from lib.market import pair_prices, pairs_for_mints
from lib.strategy import sim_buy, sim_sell, features
from lib.copylogic import conf, classify, copy_size, book_equity

C = "copybook"
ST = P("state", "copybook.json")

def fmt(x): return f"${x:,.2f}".replace(",", " ")

def new_book(cc):
    return {"mode": "paper", "target": cc["target"], "label": cc["label"], "start_balance": cc["start_balance_usd"],
            "cash": cc["start_balance_usd"], "positions": [], "closed": [], "events": [], "handled_sigs": [],
            "last_sig": None, "created": now_iso(), "active_since_ts": int(time.time())}

_rpc_i = 0
def rpc(cc, method, params):
    global _rpc_i
    urls = cc["rpc_urls"]; err = None
    for k in range(len(urls) * 2):
        url = urls[(_rpc_i + k) % len(urls)]
        try:
            r = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
            if "error" in r: raise RuntimeError(str(r["error"])[:200])
            _rpc_i = (_rpc_i + k) % len(urls)
            return r["result"]
        except Exception as e:
            err = e; time.sleep(1.0 + k)
    raise RuntimeError(f"rpc {method} failed: {err!r}"[:300])

def prefix(cc): return f"[PAPIER – kopia {cc['label']}]"

def quote_for(mint):
    q = pairs_for_mints([mint], deadline=time.time() + 10).get(mint)
    return features(q) if q else None

def open_copy(book, cc, c, ev, sig):
    mint = ev["mint"]
    if any(p["mint"] == mint for p in book["positions"]): return "już w kopii"
    age = time.time() - (ev.get("block_time") or 0)
    if age > cc["max_signal_age_s"]: return f"sygnał za stary ({age:.0f} s)"
    ft = None
    for _ in range(3):
        ft = quote_for(mint)
        if ft and ft["price"] > 0: break
        time.sleep(3)
    if not ft or ft["price"] <= 0: return "brak ceny na DexScreener"
    eq = book_equity(book)
    size = copy_size(eq, book["cash"], len(book["positions"]), cc)
    if size <= 0:
        return "limit pozycji" if len(book["positions"]) >= int(cc["max_open_positions"]) else "brak gotówki"
    b = sim_buy(size, ft["price"], ft["liq"], c["costs"])
    pos = {"id": f"{mint[:6]}-{int(time.time())}", "mint": mint, "symbol": ft["symbol"], "pair": ft["pair"], "url": ft["url"],
           "entry_ts": time.time(), "entry_iso": now_iso(), "entry_price": ft["price"], "entry_fill": b["fill"], "qty": b["qty"],
           "qty0": b["qty"], "cost_usd": size, "cost_left": size, "realized": 0.0, "entry_slip": b["slip"], "entry_mcap": ft["mcap"],
           "entry_liq": ft["liq"], "last_price": ft["price"], "last_liq": ft["liq"], "last_quote_ts": time.time(),
           "their_sig": sig, "their_sol": ev.get("sol_spent"), "their_stable": ev.get("stable_spent")}
    book["cash"] -= size; book["positions"].append(pos)
    paid = f"{ev['sol_spent']:.3f} SOL" if (ev.get("sol_spent") or 0) >= cc["min_spend_sol"] else f"{ev.get('stable_spent', 0):.2f} USD w stablecoinach"
    notify("copy_buy", f"PAPIER kopia: kupno ${ft['symbol']} za {fmt(size)}",
           f"{prefix(cc)} {cc['label']} kupił ${ft['symbol']} (zapłacił {paid}). Kopia papierowa: kupno za {fmt(size)} "
           f"({int(round(cc['position_pct_of_equity']*100))}% equity kopii {fmt(eq)}). Kapitalizacja ~{fmt(ft['mcap'])}, płynność ~{fmt(ft['liq'])}, "
           f"poślizg {b['slip']*100:.1f}%. Sprzedaż tylko lustrzanie za {cc['label']}. {ft['url']} · https://solscan.io/tx/{sig}",
           {"mint": mint, "pos_id": pos["id"], "book": "copy"})
    log(C, "copy BUY", symbol=ft["symbol"], mint=mint, size=size, price=ft["price"])
    return None

def sell_copy(book, cc, c, pos, frac, reason, price=None, liq=None, dead=False):
    """Sell `frac` of the qty we still hold (same fraction the wallet sold of its holding; frac>=full_exit_fraction -> everything)."""
    if price is None or liq is None:
        ft = quote_for(pos["mint"])
        if ft and ft["price"] > 0: price, liq = ft["price"], ft["liq"]
        else: price, liq = pos.get("last_price") or pos["entry_price"], pos.get("last_liq") or pos["entry_liq"]
    full = dead or frac >= cc["full_exit_fraction"] - 1e-9
    qty = pos["qty"] if full else min(pos["qty"], pos["qty"] * frac)
    if dead:
        proceeds, slip = 0.0, 1.0
    else:
        s = sim_sell(qty, price, liq, c["costs"]); proceeds, slip = max(0.0, s["proceeds"]), s["slip"]
    part_cost = pos["cost_left"] * (qty / pos["qty"]) if pos["qty"] > 0 else pos["cost_left"]
    pos["qty"] -= qty; pos["cost_left"] -= part_cost; pos["realized"] += proceeds - part_cost
    book["cash"] += proceeds
    gross = price / pos["entry_price"] - 1 if pos["entry_price"] else 0
    if full or pos["qty"] <= 1e-12:
        rec = {**pos, "exit_ts": time.time(), "exit_iso": now_iso(), "exit_price": 0.0 if dead else price, "pnl_usd": pos["realized"],
               "pnl_pct": pos["realized"] / pos["cost_usd"], "gross_pct": -1.0 if dead else gross, "reason": reason,
               "hold_min": (time.time() - pos["entry_ts"]) / 60}
        book["positions"] = [p for p in book["positions"] if p["id"] != pos["id"]]
        book["closed"].append(rec)
        notify("copy_sell", f"PAPIER kopia: sprzedaż ${pos['symbol']} {rec['pnl_pct']*100:+.1f}%",
               f"{prefix(cc)} Sprzedaż całości ${pos['symbol']}: {reason}. Ruch ceny {rec['gross_pct']*100:+.1f}%, wynik netto po kosztach "
               f"{fmt(rec['pnl_usd'])} ({rec['pnl_pct']*100:+.1f}%). Czas w pozycji {rec['hold_min']:.0f} min. Equity kopii: {fmt(book_equity(book))}.",
               {"mint": pos["mint"], "pos_id": pos["id"], "book": "copy", "pnl_usd": rec["pnl_usd"]})
    else:
        notify("copy_sell", f"PAPIER kopia: częściowa sprzedaż ${pos['symbol']} {frac*100:.0f}%",
               f"{prefix(cc)} Częściowa sprzedaż ${pos['symbol']} ({frac*100:.0f}% pozycji): {reason}. Ruch ceny {gross*100:+.1f}%, "
               f"wpływ {fmt(proceeds)}, poślizg {slip*100:.1f}%. Equity kopii: {fmt(book_equity(book))}.",
               {"mint": pos["mint"], "pos_id": pos["id"], "book": "copy"})
    log(C, "copy SELL", symbol=pos["symbol"], frac=frac, full=full, proceeds=round(proceeds, 4))

def poll_wallet(book, cc, c):
    sigs = rpc(cc, "getSignaturesForAddress", [cc["target"], {"limit": 25}])
    if not book.get("last_sig"):
        book["last_sig"] = sigs[0]["signature"] if sigs else None
        book["baseline_iso"] = now_iso(); return
    new = []
    for s in sigs:
        if s["signature"] == book["last_sig"]: break
        new.append(s)
    if sigs and len(new) == len(sigs):
        log(C, "gap: last_sig not in latest 25", last=book["last_sig"])
    for s in reversed(new):
        if not s.get("err") and s["signature"] not in book["handled_sigs"]:
            tx = None
            time.sleep(0.4)   # be gentle with the public RPC
            for _ in range(3):
                tx = rpc(cc, "getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
                if tx: break
                time.sleep(1.5)
            if not tx:
                log(C, "tx not available yet", sig=s["signature"]); return
            for ev in classify(tx, cc["target"], cc):
                if (ev.get("block_time") or 0) < book["active_since_ts"]: continue
                pos = next((p for p in book["positions"] if p["mint"] == ev["mint"]), None)
                dec = None
                if ev["kind"] == "new":
                    why = open_copy(book, cc, c, ev, s["signature"])
                    dec = "kupno" if not why else "pominięte: " + why
                    if why:
                        notify("copy_skip", f"PAPIER kopia: pominięte {ev['mint'][:6]}",
                               f"{prefix(cc)} {cc['label']} otworzył nową pozycję ({ev['mint'][:6]}…), kopia papierowa NIE kupiła: {why}. https://dexscreener.com/solana/{ev['mint']}",
                               {"mint": ev["mint"], "book": "copy"})
                elif ev["kind"] == "sell" and pos:
                    f = ev["sell_frac"]
                    if f >= cc["min_sell_fraction"]:
                        t = time.strftime("%H:%M", time.localtime(ev.get("block_time") or time.time()))
                        sell_copy(book, cc, c, pos, f, f"{cc['label']} sprzedał {f*100:.0f}% o {t}"); dec = "lustrzana sprzedaż"
                else:
                    dec = {"addon": "dokupienie – pomijam", "airdrop": "transfer/airdrop – pomijam"}.get(ev["kind"], "sprzedaż poza kopią")
                book["events"].append({"sig": s["signature"], "ts": ev.get("block_time"), "mint": ev["mint"], "kind": ev["kind"],
                                       "sol": ev.get("sol_spent"), "decision": dec})
            book["handled_sigs"] = (book["handled_sigs"] + [s["signature"]])[-200:]
        book["last_sig"] = s["signature"]
    book["events"] = book["events"][-60:]

def mark(book, cc, c):
    if not book["positions"]: return
    q = pair_prices([p["pair"] for p in book["positions"]], deadline=time.time() + 10)
    now = time.time()
    for p in list(book["positions"]):
        x = q.get(p["pair"])
        if x and x.get("priceUsd"):
            p["last_price"] = float(x["priceUsd"]); p["last_liq"] = float((x.get("liquidity") or {}).get("usd") or 0)
            p["last_quote_ts"] = now; p["last_gain"] = p["last_price"] / p["entry_price"] - 1
        dead = (p.get("last_liq") or 0) < cc["dead_liq_usd"] or now - (p.get("last_quote_ts") or now) > cc["no_quote_timeout_s"]
        if dead:
            sell_copy(book, cc, c, p, 1.0, "rug/brak płynności", dead=True)

def main():
    c = cfg(); cc = conf(c)
    book = load(ST) or new_book(cc)
    save(ST, book)
    log(C, "copybook started", target=cc["target"], enabled=cc["enabled"])
    last_px = 0
    while True:
        c = cfg(); cc = conf(c)
        heartbeat(C, status="ok" if cc["enabled"] else "disabled", open=len(book["positions"]))
        if cc["enabled"] and cc.get("target"):
            try:
                poll_wallet(book, cc, c)
                if time.time() - last_px >= cc["price_poll_s"]:
                    last_px = time.time(); mark(book, cc, c)
                book["equity"] = book_equity(book); book["updated"] = now_iso(); book["last_poll"] = now_iso()
                book.pop("last_error", None)
            except Exception as e:
                book["last_error"] = repr(e)[:200]
                log(C, "cycle error", err=repr(e)[:300])
            save(ST, book)
        time.sleep(cc["poll_s"])

if __name__ == "__main__":
    main()
