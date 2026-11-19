"""Pure strategy logic (no I/O): analyst scoring, filter rules, cost model, trailing stop ladder.

UPTREND definition (all must hold, thresholds in config.json -> filter):
  * priceChange m5 > 0 AND h1 > 0 AND h6 >= min_h6_change (0)       -> price rising on 5m, 1h, not falling on 6h
  * buys/sells m5 >= 1.0 AND buys/sells h1 >= 1.0                      -> net buy pressure
  * m5 txns >= 10                                                      -> it is actually trading now
  * volume acceleration = (vol_m5*12) / vol_h1 >= 0.8                  -> last 5m pace not dying vs the hour
  * NOT overextended: m5 change <= 25%, h1 change <= 300%              -> avoid buying a vertical candle top
Hygiene: $40k <= mcap (marketCap, else FDV) <= $300k (max cut 19.11 by user), liquidity >= $30k, vol h1 >= $5k, pair age >= 30 min,
         not already held (same mint or same ticker, case-insensitive), not exited in the last reentry_cooldown_min minutes.
"""
import time

def _f(x, d=0.0):
    try: return float(x)
    except Exception: return d

def features(pair, now=None):
    now = now or time.time()
    pc = pair.get("priceChange") or {}
    tx = pair.get("txns") or {}
    vol = pair.get("volume") or {}
    m5 = tx.get("m5") or {}; h1 = tx.get("h1") or {}
    b5, s5 = _f(m5.get("buys")), _f(m5.get("sells"))
    b1, s1 = _f(h1.get("buys")), _f(h1.get("sells"))
    vh1 = _f(vol.get("h1")); vm5 = _f(vol.get("m5"))
    created = pair.get("pairCreatedAt")
    return {
        "mint": (pair.get("baseToken") or {}).get("address"),
        "symbol": (pair.get("baseToken") or {}).get("symbol"),
        "pair": pair.get("pairAddress"), "dex": pair.get("dexId"), "url": pair.get("url"),
        "price": _f(pair.get("priceUsd")),
        "mcap": _f(pair.get("marketCap")) or _f(pair.get("fdv")),
        "liq": _f((pair.get("liquidity") or {}).get("usd")),
        "chg_m5": _f(pc.get("m5")), "chg_h1": _f(pc.get("h1")), "chg_h6": _f(pc.get("h6"), None) if pc.get("h6") is not None else None,
        "bs_m5": b5 / max(s5, 1.0), "bs_h1": b1 / max(s1, 1.0), "tx_m5": b5 + s5,
        "vol_m5": vm5, "vol_h1": vh1, "vol_accel": (vm5 * 12) / vh1 if vh1 > 0 else 0.0,
        "age_min": (now - created / 1000) / 60 if created else None,
    }

def score(ft):
    """Analyst score 0-100 (higher = stronger, cleaner momentum)."""
    clip = lambda v, lo, hi: max(lo, min(hi, v))
    s = 0.0
    s += 20 * clip(ft["chg_m5"] / 10, 0, 1)               # 5m move up to +10%
    s += 20 * clip(ft["chg_h1"] / 50, 0, 1)               # 1h move up to +50%
    s += 15 * clip((ft["bs_m5"] - 1) / 1.0, 0, 1)         # m5 buy/sell up to 2x
    s += 15 * clip((ft["bs_h1"] - 1) / 0.5, 0, 1)         # h1 buy/sell up to 1.5x
    s += 15 * clip(ft["vol_accel"] / 2, 0, 1)             # volume accelerating up to 2x
    s += 15 * clip(ft["liq"] / 100000, 0, 1)              # depth up to $100k
    return round(s, 1)

def norm_symbol(s):
    """Canonical ticker for already-held / cooldown checks (case-insensitive)."""
    return (s or "").strip().upper()

def _norm_symbols(items):
    return {norm_symbol(x) for x in items if norm_symbol(x)}

def filter_reasons(ft, fc, held=(), cooldown=(), held_symbols=(), cooldown_symbols=()):
    """Return list of rejection reasons (empty list = pass).

    already_held / reentry_cooldown match the mint or the same ticker
    (memecoin copies often share a symbol with a different mint).
    """
    r = []
    sym = norm_symbol(ft.get("symbol"))
    if ft["mint"] in held or (sym and sym in _norm_symbols(held_symbols)):
        r.append("already_held")
    if ft["mint"] in cooldown or (sym and sym in _norm_symbols(cooldown_symbols)):
        r.append("reentry_cooldown")
    if ft["price"] <= 0: r.append("no_price")
    if ft["mcap"] < fc["min_mcap_usd"]: r.append(f"mcap<{fc['min_mcap_usd']:.0f} ({ft['mcap']:.0f})")
    if fc.get("max_mcap_usd") and ft["mcap"] > fc["max_mcap_usd"]: r.append(f"mcap>{fc['max_mcap_usd']:.0f} ({ft['mcap']:.0f})")
    if ft["liq"] < fc["min_liquidity_usd"]: r.append(f"liq<{fc['min_liquidity_usd']:.0f} ({ft['liq']:.0f})")
    if ft["vol_h1"] < fc["min_vol_h1_usd"]: r.append(f"vol_h1<{fc['min_vol_h1_usd']:.0f}")
    if ft["age_min"] is None or ft["age_min"] < fc["min_pair_age_min"]: r.append("pair_too_new")
    if fc["require_m5_up"] and ft["chg_m5"] <= 0: r.append(f"downtrend_m5 ({ft['chg_m5']}%)")
    if fc["require_h1_up"] and ft["chg_h1"] <= 0: r.append(f"downtrend_h1 ({ft['chg_h1']}%)")
    if ft["chg_h6"] is not None and ft["chg_h6"] < fc["min_h6_change"]: r.append(f"downtrend_h6 ({ft['chg_h6']}%)")
    if ft["bs_m5"] < fc["min_buy_sell_ratio_m5"]: r.append(f"sells>buys_m5 ({ft['bs_m5']:.2f})")
    if ft["bs_h1"] < fc["min_buy_sell_ratio_h1"]: r.append(f"sells>buys_h1 ({ft['bs_h1']:.2f})")
    if ft["tx_m5"] < fc["min_m5_txns"]: r.append("low_activity_m5")
    if ft["vol_accel"] < fc["min_vol_accel"]: r.append(f"volume_fading ({ft['vol_accel']:.2f})")
    if ft["chg_m5"] > fc["max_m5_change_pct"]: r.append(f"overextended_m5 ({ft['chg_m5']}%)")
    if ft["chg_h1"] > fc["max_h1_change_pct"]: r.append(f"overextended_h1 ({ft['chg_h1']}%)")
    if not r and score(ft) < fc["min_score"]: r.append(f"score<{fc['min_score']} ({score(ft)})")
    return r

# ---------------- cost model ----------------
def slippage(size_usd, liq_usd, cc):
    """One-side slippage: max(min_slippage, impact_mult * size / liquidity). Constant-product pool:
    impact ~ size / quote-side reserve ~= 2*size/total_liq. min 1% covers LP fee (0.25-1%) + spread."""
    if liq_usd <= 0: return 0.5
    return max(cc["min_slippage_pct"], cc["impact_mult"] * size_usd / liq_usd)

def sim_buy(size_usd, price, liq, cc):
    slip = slippage(size_usd, liq, cc)
    fill = price * (1 + slip)
    qty = (size_usd - cc["network_fee_usd"]) / fill
    return {"qty": qty, "fill": fill, "slip": slip, "fee": cc["network_fee_usd"], "cost_usd": size_usd}

def sim_sell(qty, price, liq, cc):
    gross = qty * price
    slip = slippage(gross, liq, cc)
    fill = price * (1 - slip)
    return {"proceeds": qty * fill - cc["network_fee_usd"], "fill": fill, "slip": slip, "fee": cc["network_fee_usd"]}

def position_size(equity, cash, cfg):
    size = round(equity * cfg["position_pct_of_equity"], 2)
    size = min(size, cash)
    return size if size >= cfg["min_position_usd"] else 0.0

# ---------------- trailing stop ladder ----------------
def trail_tiers(cfg):
    """[(peak threshold, gap), ...] ascending. Prefers config trail_tiers; else (trail_after, trail_gap)."""
    raw = cfg.get("trail_tiers")
    if raw:
        return [(float(a), float(g)) for a, g in raw]
    ta, tg = cfg.get("trail_after"), cfg.get("trail_gap")
    if ta is not None and tg is not None:
        return [(float(ta), float(tg))]
    return []

def trail_gap_for(peak, cfg):
    """Trailing gap for this peak, or None below the first trail tier."""
    gap = None
    for after, g in trail_tiers(cfg):
        if peak >= after - 1e-12:
            gap = g
    return gap

def ladder_note_pl(cfg):
    """Polish buy-note / site one-liner, driven by initial_stop, ladder and trail_tiers."""
    parts = [f"Stop {cfg.get('initial_stop', -0.06)*100:.0f}%"]
    for trig, new_stop in cfg.get("ladder") or []:
        if abs(new_stop) < 1e-12:
            parts.append(f"po +{trig*100:.0f}% → stop na wejściu")
        else:
            parts.append(f"po +{trig*100:.0f}% → stop {new_stop*100:+.0f}%")
    trail = [f"od +{a*100:.0f}% trailing {g*100:.0f} pkt" for a, g in trail_tiers(cfg)]
    if trail:
        parts.append(", ".join(trail))
    return "; ".join(parts)

def ladder_update(pos, price, cfg):
    """Update a position with a new market price. Ladder is measured on MARKET price vs entry market price
    (gross move of the coin). Stop never moves down. Returns (exit: bool, reason: str|None).
    User rules (19.11): stop -6%; peak >= +12% -> stop 0% (entry); peak >= +24% -> trail 12 pp below peak;
    peak >= +100% -> trail 20 pp; peak >= +200% -> trail 25 pp. When switching tiers,
    stop = max(previous stop, peak - new gap). Stop never moves down."""
    entry = pos["entry_price"]
    gain = price / entry - 1
    pos["last_price"] = price
    pos["last_gain"] = gain
    pos["peak_gain"] = max(pos.get("peak_gain", 0.0), gain)
    stop = pos.get("stop_pct", cfg["initial_stop"])
    for trig, new_stop in cfg["ladder"]:
        if pos["peak_gain"] >= trig and new_stop > stop:
            stop = new_stop
            pos.setdefault("ladder_log", []).append({"ts": time.time(), "trigger": trig, "stop": new_stop})
    gap = trail_gap_for(pos["peak_gain"], cfg)
    if gap is not None:
        trail = round(pos["peak_gain"] - gap, 6)
        if trail > stop:
            stop = trail
    pos["stop_pct"] = stop
    if gain <= stop + 1e-12:
        if stop < 0: return True, f"stop {stop*100:+.0f}%"
        if stop == 0: return True, "stop na wejściu (0%)"
        used = pos["peak_gain"] - stop
        return True, f"trailing stop {stop*100:+.1f}% (szczyt {pos['peak_gain']*100:+.1f}% − {used*100:.0f} pkt)"
    return False, None

# ---------------- dead liquidity / rug ----------------
DEAD_REASON = "rug/brak płynności"

def _quote_liq(quote):
    if not quote: return None
    try: return float((quote.get("liquidity") or {}).get("usd") or 0)
    except Exception: return None

def _last_seen_ts(pos):
    if pos.get("last_quote_ts") is not None:
        try: return float(pos["last_quote_ts"])
        except Exception: pass
    return pos.get("entry_ts")

def missed_quote_limit(cfg=None):
    cfg = cfg or {}
    timeout = cfg.get("no_quote_timeout_s", 600)
    poll = max(1, int(cfg.get("price_poll_s", 20)))
    return max(1, int(timeout / poll))

def is_dead_liquidity(pos, quote=None, now=None, cfg=None):
    """True if the pair is untradeable: liquidity < $1,000, or no quote for 10+ minutes."""
    return dead_liquidity_reason(pos, quote=quote, now=now, cfg=cfg) is not None

def dead_liquidity_reason(pos, quote=None, now=None, cfg=None):
    """Return 'rug/brak płynności' when liquidity is gone or the pair has vanished, else None."""
    cfg = cfg or {}
    now = now if now is not None else time.time()
    dead_liq = cfg.get("dead_liq_usd", 1000)
    timeout = cfg.get("no_quote_timeout_s", 600)
    liq = _quote_liq(quote)
    if liq is None and pos.get("last_liq") is not None:
        try: liq = float(pos["last_liq"])
        except Exception: liq = None
    if liq is not None and liq < dead_liq:
        return DEAD_REASON
    missing = quote is None or not quote.get("priceUsd")
    if missing:
        missed = int(pos.get("missed_quotes") or 0)
        if missed >= missed_quote_limit(cfg):
            return DEAD_REASON
        last = _last_seen_ts(pos)
        if last is not None and (now - last) >= timeout:
            return DEAD_REASON
    return None

def position_mark(pos, now=None, cfg=None):
    """Mark-to-market USD value. Dead-liquidity / vanished pairs mark at $0 so equity is not inflated."""
    if is_dead_liquidity(pos, quote=None, now=now, cfg=cfg):
        return 0.0
    return pos["qty"] * pos.get("last_price", pos["entry_price"])

def portfolio_equity(pf, now=None, cfg=None):
    return pf["cash"] + sum(position_mark(p, now=now, cfg=cfg) for p in pf.get("positions", []))

def dead_exit(pos):
    """Total loss close: proceeds $0, pnl -100%."""
    cost = pos["cost_usd"]
    return {"proceeds": 0.0, "fill": 0.0, "slip": 1.0, "fee": 0.0,
            "pnl_usd": -cost, "pnl_pct": -1.0, "gross_pct": -1.0, "reason": DEAD_REASON}
