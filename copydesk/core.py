"""PAPER copy desk – pure logic (no network, no I/O). Simulation only: no wallet, no keys, no signing.

Transaction parsing works on getTransaction(jsonParsed) results of ANY version (legacy, 0, 1):
only meta.pre/postTokenBalances, meta.pre/postBalances and the account key list are used.
Account keys may be dicts ({"pubkey": ...}) or plain strings; loaded (lookup-table) addresses from
meta.loadedAddresses are appended when the RPC returns them separately (json encoding).

Events per tx and copied wallet (owner):
  new      token balance 0 -> >0 and the wallet paid >= min_spend_sol SOL or >= min_spend_stable USDC/USDT
  addon    already held and paid the same minimum (copied only into a paper position we already hold)
  airdrop  balance went up but nothing was paid (airdrop / free transfer) -> always skipped
  sell     balance went down; frac = sold / pre (>= full_exit_fraction or post == 0 -> 1.0)
"""
import math, time

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = {WSOL, USDC, USDT}
PAPER_TAG = "[PAPIER – bez prawdziwych pieniędzy]"

# ---------------------------------------------------------------- tx parsing
def account_keys(tx):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("accountKeys") or [])]
    la = ((tx or {}).get("meta") or {}).get("loadedAddresses") or {}
    if la and not any(isinstance(k, dict) and k.get("source") for k in (msg.get("accountKeys") or [])):
        keys += list(la.get("writable") or []) + list(la.get("readonly") or [])
    return keys

def ui_amount(b):
    ta = (b or {}).get("uiTokenAmount") or {}
    v = ta.get("uiAmount")
    if v is not None:
        try: return float(v)
        except (TypeError, ValueError): pass
    s = ta.get("uiAmountString")
    if s not in (None, ""):
        try: return float(s)
        except (TypeError, ValueError): pass
    try: return int(ta.get("amount") or 0) / (10 ** int(ta.get("decimals") or 0))
    except (TypeError, ValueError): return 0.0

def _decimals(b):
    try: return int(((b or {}).get("uiTokenAmount") or {}).get("decimals"))
    except (TypeError, ValueError): return None

def token_deltas(tx, owner):
    m = (tx or {}).get("meta") or {}
    pre, post, dec = {}, {}, {}
    for side, store in (("preTokenBalances", pre), ("postTokenBalances", post)):
        for b in m.get(side) or []:
            if b.get("owner") != owner or not b.get("mint"): continue
            store[b["mint"]] = store.get(b["mint"], 0.0) + ui_amount(b)
            d = _decimals(b)
            if d is not None: dec[b["mint"]] = d
    out = []
    for mint in sorted(set(pre) | set(post)):
        if mint in QUOTES: continue
        a, b = pre.get(mint, 0.0), post.get(mint, 0.0)
        if abs(b - a) <= max(1e-12, 1e-9 * max(a, b)): continue
        out.append({"mint": mint, "pre": a, "post": b, "decimals": dec.get(mint),
                    "side": "buy" if b > a else "sell", "frac": (a - b) / a if b < a and a > 0 else None})
    return out

def quote_spent(tx, owner):
    """(SOL spent incl. wSOL, USDC+USDT spent) by owner in tx. Negative = received."""
    m = (tx or {}).get("meta") or {}
    keys = account_keys(tx)
    sol = 0.0
    if owner in keys:
        i = keys.index(owner)
        try: sol = (int(m["preBalances"][i]) - int(m["postBalances"][i])) / 1e9
        except (KeyError, IndexError, TypeError, ValueError): sol = 0.0
    def tok(side, mint):
        return sum(ui_amount(b) for b in m.get(side) or [] if b.get("owner") == owner and b.get("mint") == mint)
    sol += tok("preTokenBalances", WSOL) - tok("postTokenBalances", WSOL)
    stable = sum(tok("preTokenBalances", s) - tok("postTokenBalances", s) for s in (USDC, USDT))
    return sol, stable

def classify(tx, owner, cfg):
    if not tx or ((tx.get("meta") or {}).get("err")):
        return []
    sol, stable = quote_spent(tx, owner)
    paid = sol >= float(cfg["min_spend_sol"]) or stable >= float(cfg["min_spend_stable"])
    evs = []
    for d in token_deltas(tx, owner):
        ev = dict(d, sol_spent=round(sol, 9), stable_spent=round(stable, 6), block_time=tx.get("blockTime"))
        if d["side"] == "buy":
            ev["kind"] = ("addon" if d["pre"] > 1e-12 else "new") if paid else "airdrop"
        else:
            ev["kind"] = "sell"
            f = 1.0 if d["post"] <= 1e-12 else float(d["frac"] or 0)
            ev["sell_frac"] = 1.0 if f >= float(cfg["full_exit_fraction"]) else f
            ev["transfer_only"] = sol >= -1e-6 and stable >= -1e-6   # nothing received -> moved tokens out
        evs.append(ev)
    return evs

def his_usd(ev, sol_usd):
    """USD the wallet spent (buy) or received (sell) in quote currency, >= 0."""
    sol, st = float(ev.get("sol_spent") or 0), float(ev.get("stable_spent") or 0)
    if ev.get("kind") == "sell":
        return max(0.0, -sol) * float(sol_usd or 0) + max(0.0, -st)
    return max(0.0, sol) * float(sol_usd or 0) + max(0.0, st)

def his_price(ev, sol_usd):
    qty = abs(float(ev.get("post") or 0) - float(ev.get("pre") or 0))
    u = his_usd(ev, sol_usd)
    return u / qty if qty > 0 and u > 0 else None

# ---------------------------------------------------------------- book
def new_book(cfg, now=None):
    now = now or time.time()
    return {"mode": "paper", "version": 1, "start_ts": now, "start_equity": float(cfg["start_equity_usd"]),
            "cash": float(cfg["start_equity_usd"]), "positions": [], "closed": [], "feed": [], "equity_history": [],
            "wallets": {}, "fees_paid_usd": 0.0, "seq": 0}

def wallet_state(book, w, now=None):
    st = book["wallets"].setdefault(w["key"], {})
    st.setdefault("start_ts", now or time.time())
    for k, v in (("last_sig", None), ("queue", []), ("handled", []), ("last_activity_ts", None), ("last_trade_ts", None),
                 ("last_poll_ts", None), ("errors", 0), ("last_error", None), ("airdrops_skipped", 0), ("copied_buys", 0),
                 ("copied_sells", 0), ("skipped", 0)):
        st.setdefault(k, v)
    return st

def mark_value(p):
    return 0.0 if p.get("dead") else float(p["qty"]) * float(p.get("last_price") or p.get("entry_price") or 0)

def equity(book):
    return float(book.get("cash") or 0) + sum(mark_value(p) for p in book.get("positions") or [])

def token_cost(book, mint):
    return sum(float(p.get("cost_left") or 0) for p in book["positions"] if p["mint"] == mint)

def find_pos(book, wallet_key, mint):
    return next((p for p in book["positions"] if p["wallet"] == wallet_key and p["mint"] == mint), None)

def size_new(book, alloc_pct, mint, cfg):
    """(usd, reason). alloc% of equity; sized down to free cash; per-token cap; skip below min_position_usd."""
    eq = equity(book)
    want = round(eq * float(alloc_pct), 2)
    cap_room = float(cfg["max_token_pct_of_equity"]) * eq - token_cost(book, mint)
    size = min(want, float(book["cash"]), cap_room)
    size = math.floor(max(0.0, size) * 100) / 100
    if size < float(cfg["min_position_usd"]):
        if cap_room < float(cfg["min_position_usd"]): return 0.0, "limit 40% equity na ten token"
        return 0.0, f"za mało wolnej gotówki ({fmt_usd(book['cash'])})"
    return size, None

def size_addon(book, pos, their_add_usd, cfg):
    """Paid add-on: our initial * (their add / their initial), capped at 40% equity per token and by cash."""
    their_init = float(pos.get("their_initial_usd") or 0)
    our_init = float(pos.get("initial_cost") or 0)
    if their_init <= 0 or their_add_usd <= 0 or our_init <= 0:
        return 0.0, "brak kwoty portfela do proporcji"
    eq = equity(book)
    room = float(cfg["max_token_pct_of_equity"]) * eq - token_cost(book, pos["mint"])
    size = min(our_init * their_add_usd / their_init, room, float(book["cash"]))
    size = math.floor(max(0.0, size) * 100) / 100
    if size < float(cfg["min_position_usd"]):
        return 0.0, ("limit 40% equity na token" if room < float(cfg["min_position_usd"]) else "za mało gotówki / za mała kwota")
    return size, None

# ---------------------------------------------------------------- cost model (Phantom)
def network_fee_usd(costs, sol_usd):
    return (float(costs["base_fee_sol"]) + float(costs["priority_fee_sol"])) * float(sol_usd or 0)

def buy_plan(size_usd, sol_usd, costs, new_account=True):
    """Split what leaves the paper cash for a buy of size_usd: Phantom fee 0.85% of the swap, network fee, token account rent."""
    net = network_fee_usd(costs, sol_usd)
    rent = float(costs["token_account_rent_sol"]) * float(sol_usd or 0) if new_account else 0.0
    swap_gross = max(0.0, size_usd - net - rent)
    phantom = swap_gross * float(costs["phantom_fee_pct"])
    return {"size": size_usd, "network_usd": net, "rent_usd": rent, "phantom_usd": phantom,
            "swap_in_usd": swap_gross - phantom}

def fallback_impact(usd, liq, costs):
    liq = float(liq or 0)
    imp = float(costs["fallback_pool_fee_pct"]) + (float(costs["fallback_impact_mult"]) * usd / liq if liq > 0 else 0.05)
    return min(imp, 0.5)

def buy_fill(plan, market_price, quote_tokens=None, liq=0.0, costs=None):
    """Tokens received. quote_tokens = Jupiter outAmount (already includes pool fee + price impact) for swap_in_usd."""
    if quote_tokens and quote_tokens > 0:
        tokens, src = float(quote_tokens), "jupiter"
    else:
        imp = fallback_impact(plan["swap_in_usd"], liq, costs)
        tokens, src = plan["swap_in_usd"] * (1 - imp) / market_price, "szacunek"
    fill = plan["swap_in_usd"] / tokens if tokens > 0 else 0.0
    impact = fill / market_price - 1 if market_price else 0.0
    eff = plan["size"] / tokens if tokens > 0 else 0.0          # all-in price per token incl. every cost
    return {"tokens": tokens, "fill_price": fill, "all_in_price": eff, "impact_pct": impact, "source": src}

def sell_fill(qty, market_price, sol_usd, costs, quote_usd=None, liq=0.0, refund_rent=False):
    """USD credited for selling qty: swap output (Jupiter quote incl. pool fee + impact, or estimate) minus
    Phantom 0.85%, minus network fee, plus token-account rent refund on a full exit."""
    gross_mkt = qty * market_price
    if quote_usd is not None and quote_usd >= 0:
        out, src = float(quote_usd), "jupiter"
    else:
        out, src = gross_mkt * (1 - fallback_impact(gross_mkt, liq, costs)), "szacunek"
    phantom = out * float(costs["phantom_fee_pct"])
    net = network_fee_usd(costs, sol_usd)
    rent = float(costs["token_account_rent_sol"]) * float(sol_usd or 0) if refund_rent else 0.0
    proceeds = max(0.0, out - phantom - net) + rent
    impact = out / gross_mkt - 1 if gross_mkt > 0 else 0.0
    return {"proceeds": proceeds, "swap_out_usd": out, "phantom_usd": phantom, "network_usd": net, "rent_refund_usd": rent,
            "impact_pct": impact, "fill_price": out / qty if qty > 0 else 0.0, "source": src}

# ---------------------------------------------------------------- book mutations
def _next_id(book, mint):
    book["seq"] = int(book.get("seq") or 0) + 1
    return f"{mint[:6]}-{book['seq']}"

def apply_buy(book, w, ev, plan, fill, info, now=None):
    """Open a paper position for wallet w. info: symbol, market_price, liq, url, sig, detect_ts, sol_usd."""
    now = now or time.time()
    pos = {"id": _next_id(book, ev["mint"]), "wallet": w["key"], "label": w["label"], "mint": ev["mint"],
           "symbol": info.get("symbol") or ev["mint"][:6], "decimals": ev.get("decimals"), "url": info.get("url"),
           "entry_ts": now, "entry_price": fill["fill_price"], "entry_market_price": info["market_price"],
           "qty": fill["tokens"], "qty0": fill["tokens"], "cost_usd": plan["size"], "cost_left": plan["size"],
           "initial_cost": plan["size"], "realized": 0.0, "rent_usd": plan["rent_usd"], "rent_sol": plan["rent_usd"] / info["sol_usd"] if info.get("sol_usd") else 0,
           "fees_usd": plan["phantom_usd"] + plan["network_usd"], "impact_pct": fill["impact_pct"], "fill_source": fill["source"],
           "their_sig": info.get("sig"), "their_ts": ev.get("block_time"), "their_price": info.get("their_price"),
           "their_initial_usd": info.get("their_usd") or 0.0, "delay_s": (info.get("detect_ts") or now) - (ev.get("block_time") or now),
           "last_price": info["market_price"], "last_liq": info.get("liq"), "last_quote_ts": now, "adds": 0}
    book["cash"] -= plan["size"]
    book["fees_paid_usd"] = float(book.get("fees_paid_usd") or 0) + plan["phantom_usd"] + plan["network_usd"]
    book["positions"].append(pos)
    return pos

def apply_addon(book, pos, plan, fill, info, now=None):
    now = now or time.time()
    q = pos["qty"] + fill["tokens"]
    if q > 0:
        pos["entry_price"] = (pos["qty"] * pos["entry_price"] + fill["tokens"] * fill["fill_price"]) / q
    pos["qty"] = q; pos["qty0"] = float(pos.get("qty0") or 0) + fill["tokens"]
    pos["cost_usd"] += plan["size"]; pos["cost_left"] += plan["size"]
    pos["fees_usd"] = float(pos.get("fees_usd") or 0) + plan["phantom_usd"] + plan["network_usd"]
    pos["last_price"] = info["market_price"]; pos["last_quote_ts"] = now; pos["adds"] = int(pos.get("adds") or 0) + 1
    book["cash"] -= plan["size"]
    book["fees_paid_usd"] = float(book.get("fees_paid_usd") or 0) + plan["phantom_usd"] + plan["network_usd"]
    return pos

def apply_sell(book, pos, frac, sf, info, cfg, now=None, dead=False):
    """Sell frac of OUR remaining qty (same fraction the wallet sold of its holding). Returns closed record or None."""
    now = now or time.time()
    full = dead or frac >= float(cfg["full_exit_fraction"]) - 1e-12
    qty = pos["qty"] if full else pos["qty"] * frac
    part_cost = pos["cost_left"] * (qty / pos["qty"]) if pos["qty"] > 0 else pos["cost_left"]
    proceeds = 0.0 if dead else sf["proceeds"]
    pos["qty"] -= qty; pos["cost_left"] -= part_cost
    pos["realized"] += proceeds - part_cost
    pos["fees_usd"] = float(pos.get("fees_usd") or 0) + (0 if dead else sf["phantom_usd"] + sf["network_usd"])
    book["cash"] += proceeds
    book["fees_paid_usd"] = float(book.get("fees_paid_usd") or 0) + (0 if dead else sf["phantom_usd"] + sf["network_usd"])
    pos.setdefault("sells", []).append({"ts": now, "frac": frac, "qty": qty, "proceeds": proceeds, "price": info.get("market_price"),
                                        "delay_s": info.get("delay_s"), "sig": info.get("sig")})
    if full or pos["qty"] <= 1e-15:
        rec = {k: v for k, v in pos.items() if k != "sells"}
        rec.update(exit_ts=now, exit_price=0.0 if dead else info.get("market_price"), pnl_usd=pos["realized"],
                   pnl_pct=pos["realized"] / pos["cost_usd"] if pos["cost_usd"] else 0.0, reason=info.get("reason"),
                   hold_min=(now - pos["entry_ts"]) / 60, exit_delay_s=info.get("delay_s"), n_sells=len(pos["sells"]), qty=0.0)
        book["positions"] = [p for p in book["positions"] if p["id"] != pos["id"]]
        book["closed"].append(rec)
        book["closed"] = book["closed"][-int(cfg.get("closed_keep", 200)):]
        return rec
    return None

# ---------------------------------------------------------------- Polish text
def fmt_usd(x, sign=False):
    x = float(x or 0)
    s = f"{abs(x):,.2f}".replace(",", " ")
    if sign: return ("+" if x >= 0 else "−") + "$" + s
    return ("−" if x < 0 else "") + "$" + s

def fmt_price(p):
    p = float(p or 0)
    if p == 0: return "$0"
    if p >= 1: return f"${p:,.4f}".replace(",", " ")
    digits = max(2, -int(math.floor(math.log10(p))) + 3)
    return f"${p:.{min(digits, 12)}f}"

def ds_link(mint): return f"https://dexscreener.com/solana/{mint}"

def note_buy(w, pos, plan, fill, eq_before, addon=False):
    pct = int(round(float(w["alloc_pct"]) * 100))
    vs = ""
    if pos.get("their_price"):
        vs = f" (portfel: {fmt_price(pos['their_price'])}, różnica {(fill['fill_price']/pos['their_price']-1)*100:+.1f}%)"
    what = "dokupienie" if addon else "kupno"
    size_txt = f"{fmt_usd(plan['size'])} ({pct}%)" if not addon else f"{fmt_usd(plan['size'])} (proporcjonalnie, limit 40% na token)"
    return (f"{PAPER_TAG} Kopia {w['label']}: {what} ${pos['symbol']} za {size_txt}. "
            f"Opóźnienie kopii {max(0, pos['delay_s'] if not addon else 0):.0f} s, cena wejścia {fmt_price(fill['fill_price'])}{vs}. "
            f"Koszty: Phantom {fmt_usd(plan['phantom_usd'])}, pula/poślizg {fill['impact_pct']*100:+.2f}% ({fill['source']}), "
            f"sieć {fmt_usd(plan['network_usd'])}" + (f", konto tokena {fmt_usd(plan['rent_usd'])} (zwrotne)" if plan['rent_usd'] > 0 else "") +
            f". Equity przed: {fmt_usd(eq_before)}. {ds_link(pos['mint'])}")

def note_sell(w, pos, frac, sf, rec, eq_after, delay_s, transfer_only=False):
    why = "przelał tokeny z portfela" if transfer_only else "sprzedał"
    if rec:
        return (f"{PAPER_TAG} Kopia {w['label']}: sprzedaż całości ${pos['symbol']} "
                f"(portfel {why} {frac*100:.0f}%). Wynik netto {fmt_usd(rec['pnl_usd'], True)} ({rec['pnl_pct']*100:+.1f}%) po kosztach, "
                f"czas w pozycji {rec['hold_min']:.0f} min, opóźnienie {max(0, delay_s or 0):.0f} s. Equity: {fmt_usd(eq_after)}. {ds_link(pos['mint'])}")
    gain = (sf['fill_price'] / pos['entry_price'] - 1) * 100 if pos.get('entry_price') else 0
    return (f"{PAPER_TAG} Kopia {w['label']}: sprzedaż {frac*100:.0f}% ${pos['symbol']} (portfel {why} {frac*100:.0f}%). "
            f"Wpływ {fmt_usd(sf['proceeds'])}, cena {gain:+.1f}% vs wejście, zrealizowane dotąd {fmt_usd(pos['realized'], True)}, "
            f"opóźnienie {max(0, delay_s or 0):.0f} s. Equity: {fmt_usd(eq_after)}. {ds_link(pos['mint'])}")

def note_dead(pos, rec, eq_after):
    return (f"{PAPER_TAG} Kopia {pos['label']}: ${pos['symbol']} bez płynności (rug/brak notowań) – pozycja zamknięta jako strata "
            f"{fmt_usd(rec['pnl_usd'], True)} ({rec['pnl_pct']*100:+.1f}%). Equity: {fmt_usd(eq_after)}. {ds_link(pos['mint'])}")

# ---------------------------------------------------------------- misc
def thin_history(hist, max_points=2500):
    """Keep the newest half at full resolution, drop every other point of the older half when too long."""
    if len(hist) <= max_points: return hist
    half = len(hist) // 2
    return hist[:half:2] + hist[half:]

def merge_queue(queue, handled, sigs, start_ts):
    """Add new signature infos (newest-first list from RPC) to the per-wallet FIFO queue (oldest first).
    Skips handled sigs, already-queued ones, failed txs and anything before the copy start."""
    have = set(handled) | {q["sig"] for q in queue}
    add = []
    for s in sigs:
        sig = s.get("signature")
        if not sig or sig in have: continue
        bt = s.get("blockTime")
        if bt is not None and bt < start_ts: continue
        if s.get("err") is not None: continue
        add.append({"sig": sig, "bt": bt, "tries": 0, "first_seen": time.time()})
        have.add(sig)
    add.sort(key=lambda q: (q["bt"] or 0))
    return queue + add
