"""PAPER copy book of a public wallet (pure logic, no I/O). No wallet, no keys, no signing.

Reads the target wallet's public transactions (getTransaction jsonParsed from a public RPC) and mirrors them
on a separate paper book:
  * new position = token balance goes 0 -> >0 AND the wallet paid >= min_spend_sol SOL or >= min_spend_stable USD
    in USDC/USDT in the same tx. Airdrops/transfers (nothing paid) are always skipped.
  * add-on = already holding and paid the same minimum; copied only when copy_addons is true.
  * sell = token balance drops; fraction = sold / pre. >= full_exit_fraction (0.9) -> full exit;
    >= min_sell_fraction (0.05) -> proportional partial sell of our paper qty.
Fills use the desk cost model (lib.strategy.sim_buy / sim_sell: slippage max(1%, 2*size/liq) + network fee per side).
"""
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = {WSOL, USDC, USDT}

DEFAULTS = {"enabled": False, "target": None, "label": "@wallet", "start_balance_usd": 25.0,
            "position_pct_of_equity": 0.2, "max_open_positions": 4, "min_position_usd": 1.0,
            "min_spend_sol": 0.005, "min_spend_stable": 1.0, "min_sell_fraction": 0.05,
            "full_exit_fraction": 0.9, "max_signal_age_s": 180, "poll_s": 5, "price_poll_s": 15,
            "rpc_urls": ["https://solana-rpc.publicnode.com", "https://api.mainnet-beta.solana.com"],
            "dead_liq_usd": 1000, "no_quote_timeout_s": 900,
            "copy_addons": False, "copy_addon_mode": "proportional", "max_token_pct_of_equity": 0.40}

def conf(cfg):
    c = dict(DEFAULTS); c.update((cfg or {}).get("copybook") or {})
    return c

def _amt(b):
    try: return float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    except (TypeError, ValueError): return 0.0

def token_deltas(tx, owner):
    """Per-mint balance changes of `owner` in tx (quote mints excluded)."""
    m = tx.get("meta") or {}
    pre, post = {}, {}
    for b in m.get("preTokenBalances") or []:
        if b.get("owner") == owner: pre[b["mint"]] = pre.get(b["mint"], 0.0) + _amt(b)
    for b in m.get("postTokenBalances") or []:
        if b.get("owner") == owner: post[b["mint"]] = post.get(b["mint"], 0.0) + _amt(b)
    out = []
    for mint in sorted(set(pre) | set(post)):
        if mint in QUOTES: continue
        a, b = pre.get(mint, 0.0), post.get(mint, 0.0)
        if abs(b - a) <= 1e-9: continue
        out.append({"mint": mint, "pre": a, "post": b, "side": "buy" if b > a else "sell",
                    "fraction": round((a - b) / a, 6) if b < a and a > 0 else None})
    return out

def quote_spent(tx, owner):
    """(SOL spent incl. wSOL, stablecoins spent) by owner in tx. Negative = received."""
    m = tx.get("meta") or {}
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
    sol = 0.0
    if owner in keys:
        i = keys.index(owner)
        try: sol = (m["preBalances"][i] - m["postBalances"][i]) / 1e9
        except (KeyError, IndexError, TypeError): sol = 0.0
    def tok(lst, mint):
        return sum(_amt(b) for b in lst or [] if b.get("owner") == owner and b.get("mint") == mint)
    sol += tok(m.get("preTokenBalances"), WSOL) - tok(m.get("postTokenBalances"), WSOL)
    stable = sum(tok(m.get("preTokenBalances"), s) - tok(m.get("postTokenBalances"), s) for s in (USDC, USDT))
    return sol, stable

def classify(tx, owner, cc):
    """List of events: kind in {'new', 'addon', 'airdrop', 'sell'}; sell events carry sell_frac (1.0 = full exit)."""
    if not tx or (tx.get("meta") or {}).get("err"):
        return []
    sol, stable = quote_spent(tx, owner)
    evs = []
    for d in token_deltas(tx, owner):
        ev = dict(d, sol_spent=round(sol, 9), stable_spent=round(stable, 6), block_time=tx.get("blockTime"))
        if d["side"] == "buy":
            if d["pre"] > 1e-9: ev["kind"] = "addon"
            elif sol >= cc["min_spend_sol"] or stable >= cc["min_spend_stable"]: ev["kind"] = "new"
            else: ev["kind"] = "airdrop"
        else:
            ev["kind"] = "sell"
            f = 1.0 if d["post"] <= 1e-9 else float(d["fraction"] or 0)
            ev["sell_frac"] = 1.0 if f >= cc["full_exit_fraction"] else f
        evs.append(ev)
    return evs

def copy_size(equity, cash, n_open, cc):
    """Paper USD size for a new copy: pct of copy-book equity, capped by cash; 0 if max open or below min."""
    if n_open >= int(cc["max_open_positions"]): return 0.0
    size = min(round(equity * float(cc["position_pct_of_equity"]), 2), cash)
    return size if size >= float(cc["min_position_usd"]) else 0.0

def book_equity(book):
    return float(book.get("cash") or 0) + sum(float(p["qty"]) * float(p.get("last_price") or p["entry_price"]) * (0.0 if p.get("dead") else 1.0)
                                               for p in book.get("positions") or [])

def his_spend_usd(ev, sol_usd):
    """USD the target spent in this event (SOL * sol_usd + USDC/USDT). Received (negative) counts as 0."""
    return max(0.0, float(ev.get("sol_spent") or 0)) * float(sol_usd or 0) + max(0.0, float(ev.get("stable_spent") or 0))

def addon_paid(ev, cc):
    """True when the add-on (or buy) paid the configured minimum in SOL or stables. Unpaid transfers stay skipped."""
    return float(ev.get("sol_spent") or 0) >= float(cc["min_spend_sol"]) or float(ev.get("stable_spent") or 0) >= float(cc["min_spend_stable"])

def addon_size(equity, cash, pos, ev, cc, sol_usd=0.0):
    """Paper USD size for a paid add-on. 0 if disabled, unpaid, below min, or the token is already at the cap.

    copy_addon_mode 'proportional' (default): our initial copy * (his add spend / his initial spend).
    'fixed': same USD as our initial copy of this token.
    Hard cap: cost_left + size <= max_token_pct_of_equity * equity (default 40%). Never exceeds cash.
    Does not consume an extra open-position slot (the token is already held).
    """
    if not cc.get("copy_addons"):
        return 0.0
    if not addon_paid(ev, cc):
        return 0.0
    cap_pct = float(cc.get("max_token_pct_of_equity") or 0.40)
    room = cap_pct * float(equity) - float(pos.get("cost_left") or 0)
    if room < float(cc["min_position_usd"]):
        return 0.0
    our_initial = float(pos.get("initial_cost") or pos.get("cost_usd") or 0)
    mode = (cc.get("copy_addon_mode") or "proportional").lower()
    if mode == "fixed":
        size = our_initial
    else:
        their_init = float(pos.get("their_initial_spend") or 0)
        their_add = his_spend_usd(ev, sol_usd)
        if their_init <= 1e-9 or their_add <= 1e-9 or our_initial <= 0:
            return 0.0
        size = our_initial * (their_add / their_init)
    size = min(size, room, float(cash))
    size = round(size, 2)
    return size if size >= float(cc["min_position_usd"]) else 0.0
