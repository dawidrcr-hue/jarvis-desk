"""Pure strategy logic (no I/O): analyst scoring, filter rules, cost model, trailing stop ladder.

UPTREND definition (all must hold, thresholds in config.json -> filter):
  * priceChange m5 > 0 AND h1 > 0 AND h6 >= min_h6_change (0)       -> price rising on 5m, 1h, not falling on 6h
  * buys/sells m5 >= 1.0 AND buys/sells h1 >= 1.0                      -> net buy pressure
  * m5 txns >= 10                                                      -> it is actually trading now
  * volume acceleration = (vol_m5*12) / vol_h1 >= 0.8                  -> last 5m pace not dying vs the hour
  * NOT overextended: m5 change <= 25%, h1 change <= 300%              -> avoid buying a vertical candle top
Hygiene: $40k <= mcap (marketCap, else FDV) <= $200k (max cut 19.11 by user), liquidity >= $30k, vol h1 >= $5k, pair age >= 20 min,
         analyst score >= 69, not already held (same mint or same ticker, case-insensitive), not exited in the last reentry_cooldown_min minutes.
         After a losing exit (stop or net loss), the mint and ticker are banned until midnight Europe/Warsaw when reentry_ban_after_loss is end_of_day.
         If cfg stop_at is set, after that instant no new entries (open positions still managed). Unset/null keeps entries open.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

def _f(x, d=0.0):
    try: return float(x)
    except Exception: return d


def stop_at_ts(cfg):
    """Unix timestamp of cfg['stop_at'] (ISO-8601), or None if unset/empty."""
    raw = (cfg or {}).get("stop_at")
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.0  # fail closed: unparsable stop_at blocks new entries
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
    return t.timestamp()

def entries_allowed(cfg, now=None):
    """True until stop_at. At or after that instant, no new paper buys. Missing stop_at keeps entries open."""
    t = stop_at_ts(cfg)
    if t is None:
        return True
    if now is None:
        now = time.time()
    return now < t

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

def warsaw_date(ts, tz=None):
    """Calendar date of a unix timestamp in Europe/Warsaw."""
    tz = tz or ZoneInfo("Europe/Warsaw")
    return datetime.fromtimestamp(float(ts), tz=tz).date()

def is_loss_exit(trade):
    """True if a closed trade should trigger the end-of-day reentry ban.

    Covers the initial stop, a stop at entry that finished in the red, rugs,
    and any other exit with a net loss. A profitable trailing-stop exit does not.
    """
    try:
        if float(trade.get("pnl_usd") or 0) < 0:
            return True
        if float(trade.get("pnl_pct") or 0) < 0:
            return True
    except (TypeError, ValueError):
        return False
    return False

def reentry_blocks(closed, cfg, now=None):
    """Cooldown and end-of-day ban sets (mints and tickers) from closed trades.

    Returns (cooldown, cooldown_symbols, banned, banned_symbols).
    A loss exit is banned until midnight Europe/Warsaw when
    cfg['reentry_ban_after_loss'] == 'end_of_day'. Profitable exits (and losses
    if that setting is off) stay on the reentry_cooldown_min window only.
    A mint/ticker in the ban set is omitted from the cooldown set so the
    filter can show the more specific reason.
    """
    now = time.time() if now is None else now
    cool_s = float((cfg or {}).get("reentry_cooldown_min") or 60) * 60
    ban_on = str((cfg or {}).get("reentry_ban_after_loss") or "").lower() == "end_of_day"
    cooldown, cooldown_symbols = set(), set()
    banned, banned_symbols = set(), set()
    today = None
    if ban_on:
        try:
            today = warsaw_date(now)
        except (TypeError, ValueError, OSError, OverflowError):
            today = None
    for t in closed or []:
        mint = t.get("mint")
        sym = norm_symbol(t.get("symbol"))
        exit_ts = t.get("exit_ts") or 0
        if ban_on and today is not None and is_loss_exit(t):
            try:
                if warsaw_date(exit_ts) == today:
                    if mint:
                        banned.add(mint)
                    if sym:
                        banned_symbols.add(sym)
                    continue
            except (TypeError, ValueError, OSError, OverflowError):
                pass
        if now - exit_ts < cool_s:
            if mint:
                cooldown.add(mint)
            if sym:
                cooldown_symbols.add(sym)
    return cooldown, cooldown_symbols, banned, banned_symbols

def reentry_note_pl(cfg):
    """Polish one-liner for the site describing the reentry rules."""
    cool = int((cfg or {}).get("reentry_cooldown_min") or 60)
    if str((cfg or {}).get("reentry_ban_after_loss") or "").lower() == "end_of_day":
        return (f"Po stracie (stop lub wynik netto na minusie) ten sam mint i ticker "
                f"są zablokowane do północy (Warszawa). Po zysku blokada {cool} min.")
    return f"Po sprzedaży ten sam mint i ticker są zablokowane {cool} min."

def filter_reasons(ft, fc, held=(), cooldown=(), held_symbols=(), cooldown_symbols=(), banned=(), banned_symbols=()):
    """Return list of rejection reasons (empty list = pass).

    already_held / reentry_cooldown / reentry_ban match the mint or the same ticker
    (memecoin copies often share a symbol with a different mint).
    """
    r = []
    sym = norm_symbol(ft.get("symbol"))
    if ft["mint"] in held or (sym and sym in _norm_symbols(held_symbols)):
        r.append("already_held")
    if ft["mint"] in banned or (sym and sym in _norm_symbols(banned_symbols)):
        r.append("reentry_ban")
    elif ft["mint"] in cooldown or (sym and sym in _norm_symbols(cooldown_symbols)):
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
    User rules (19.11): stop -8% (was -6%); peak >= +12% -> stop 0% (entry); peak >= +24% -> trail 12 pp below peak;
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

# ---------------- entry mode: buy now vs. buy after the first pullback ----------------
# config.entry = {"mode": "now" | "pullback", "pullback": {dip_pct, bounce_pct, window_min, cancel_pct, max_setups}}
# "now" (default) = current live behaviour: buy right after the filter + RugCheck pass.
# "pullback": the pass only opens a SETUP. Then, on live quotes:
#   1) track the local high H (starts at the signal price);
#   2) wait until price retraces >= dip_pct from H  -> remember the low L (updated while it keeps falling);
#   3) buy when price bounces >= bounce_pct above L;
#   cancel if price falls >= cancel_pct below H, or nothing happened within window_min minutes.
PULLBACK_DEFAULTS = {"dip_pct": 0.05, "bounce_pct": 0.02, "window_min": 15, "cancel_pct": 0.15, "max_setups": 8}

def entry_mode(cfg):
    m = str(((cfg or {}).get("entry") or {}).get("mode") or "now").lower()
    return m if m in ("now", "pullback") else "now"

def pullback_params(cfg):
    p = dict(PULLBACK_DEFAULTS)
    p.update(((cfg or {}).get("entry") or {}).get("pullback") or {})
    return p

def entry_note_pl(cfg):
    """Polish one-liner for the site describing the live entry mode."""
    if entry_mode(cfg) != "pullback":
        return ""
    p = pullback_params(cfg)
    dip = int(round(float(p["dip_pct"]) * 100))
    bounce = int(round(float(p["bounce_pct"]) * 100))
    cancel = int(round(float(p["cancel_pct"]) * 100))
    window = int(p["window_min"])
    return (f"Wejście po cofnięciu: -{dip}% od szczytu, odbicie +{bounce}%, "
            f"rezygnacja po -{cancel}% lub {window} min")

def pullback_new(cand, price, now, cfg):
    """New pullback setup from a filter pass (cand = features dict)."""
    p = pullback_params(cfg)
    return {"mint": cand.get("mint"), "symbol": cand.get("symbol"), "pair": cand.get("pair"), "cand": cand,
            "signal_ts": now, "signal_price": price, "high": price, "low": None, "state": "wait_dip",
            "expires_ts": now + p["window_min"] * 60, "last_price": price}

def pullback_step(setup, price, now, cfg):
    """Feed one live price. Returns 'wait', 'buy' or 'cancel:<reason>'. Mutates setup."""
    p = pullback_params(cfg)
    if price is None or price <= 0:
        return "cancel:timeout" if now >= setup["expires_ts"] else "wait"
    setup["last_price"] = price
    if setup["state"] == "wait_dip":
        setup["high"] = max(setup["high"], price)
    else:
        setup["low"] = min(setup["low"], price)
    hi = setup["high"]
    if price <= hi * (1 - p["cancel_pct"]) + 1e-15:
        setup["state"] = "cancelled"
        return "cancel:too_deep"
    if setup["state"] == "wait_dip" and price <= hi * (1 - p["dip_pct"]) + 1e-15:
        setup["state"] = "dip"; setup["low"] = price; setup["dip_ts"] = now
    elif setup["state"] == "dip" and price >= setup["low"] * (1 + p["bounce_pct"]) - 1e-15:
        setup["state"] = "bought"
        return "buy"
    if now >= setup["expires_ts"]:
        setup["state"] = "cancelled"
        return "cancel:timeout"
    return "wait"

PULLBACK_HYGIENE = ("already_held", "reentry_cooldown", "reentry_ban", "no_price", "mcap<", "mcap>", "liq<", "vol_h1<", "pair_too_new", "downtrend_h1")

def pullback_entry_reasons(ft, fc, *a, **kw):
    """Requote check for a pullback buy: hygiene + 1h uptrend only. The short-term momentum checks
    (m5 up, m5 buy/sell, score, vol accel) are expected to dip during the pullback, so they are not re-applied."""
    return [r for r in filter_reasons(dict(ft), dict(fc, min_score=-1e9), *a, **kw) if r.startswith(PULLBACK_HYGIENE)]
