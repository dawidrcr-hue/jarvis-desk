"""Token safety gate (PAPER desk): RugCheck public API, no key.
Final gate in the filter, run only for coins that already passed the momentum filter + score.

check(mint, pair, cfg) -> {"ok": bool, "reasons": [...], "summary": {...}, "note_pl": "..."}
- fail-closed: if RugCheck is unreachable / bad response -> ok=False, reason "rugcheck_unavailable"
- cached per mint for cache_s (default 10 min); failures are cached only fail_cache_s (default 60 s)
"""
import time, json, urllib.request, urllib.error

URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
UA = {"User-Agent": "Mozilla/5.0 jarvis-paper/1.0", "Accept": "application/json"}
NON_HOLDER_TYPES = {"AMM", "LOCKER", "BURN", "PROGRAM", "POOL", "VAULT"}   # pool / LP / locker accounts, not people
DEFAULTS = {
    "enabled": True, "timeout_s": 8, "cache_s": 600, "fail_cache_s": 60,
    "max_top10_pct": 30.0, "max_single_holder_pct": 15.0, "max_creator_pct": 10.0,
    "min_lp_locked_pct": 80.0, "max_score_normalised": 30,
    "reject_danger_risks": True, "ignore_danger_risks": [],
    "reject_mint_authority": True, "reject_freeze_authority": True,
    "reject_token_extensions": True, "reject_transfer_fee": True,
}
_cache = {}   # mint -> (ts, result)

def conf(c):
    d = dict(DEFAULTS); d.update((c or {}).get("rugcheck") or {}); return d

def fetch_report(mint, timeout):
    req = urllib.request.Request(URL.format(mint=mint), headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def _f(x, d=0.0):
    try: return float(x)
    except Exception: return d

def holder_stats(rep):
    """Top-10 / largest holder % excluding pool, LP-vault and locker accounts."""
    known = rep.get("known_accounts") or rep.get("knownAccounts") or {}
    pool_accts = set()
    for m in rep.get("markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB"):
            if m.get(k): pool_accts.add(m[k])
        for k in ("liquidityAAccount", "liquidityBAccount"):
            o = (m.get(k) or {}).get("owner")
            if o: pool_accts.add(o)
    people = []
    for h in rep.get("topHolders") or []:
        a, o, pct = h.get("address"), h.get("owner"), _f(h.get("pct"))
        t = {(known.get(a) or {}).get("type"), (known.get(o) or {}).get("type")}
        if a in pool_accts or o in pool_accts or (t & NON_HOLDER_TYPES) or pct > 100:
            continue
        people.append(pct)
    people.sort(reverse=True)
    return round(sum(people[:10]), 2), round(people[0] if people else 0.0, 2)

def lp_locked_pct(rep, pair=None):
    """LP locked/burned % of the pair the desk trades (fallback: largest-liquidity market).
    PumpSwap pools of coins that graduated from pump.fun have LP burned -> RugCheck reports ~100%.
    Pools someone created by hand on PumpSwap/Raydium report the real share (often ~0% = creator can pull it)."""
    ms = rep.get("markets") or []
    if not ms: return None, None
    def liq(m): lp = m.get("lp") or {}; return _f(lp.get("quoteUSD")) + _f(lp.get("baseUSD"))
    m = next((m for m in ms if pair and (m.get("pubkey") or "").lower() == pair.lower()), None) or max(ms, key=liq)
    return round(_f((m.get("lp") or {}).get("lpLockedPct")), 1), m.get("marketType")

def evaluate(rep, rc, pair=None):
    reasons = []
    tok = rep.get("token") or {}
    if rep.get("rugged"): reasons.append("rug_flag (RugCheck: rugged)")
    if rc["reject_mint_authority"] and (rep.get("mintAuthority") or tok.get("mintAuthority")):
        reasons.append("rug_mint_authority (mint nie odebrany)")
    if rc["reject_freeze_authority"] and (rep.get("freezeAuthority") or tok.get("freezeAuthority")):
        reasons.append("rug_freeze_authority (freeze nie odebrany)")
    ext = rep.get("token_extensions") or {}
    if rc["reject_token_extensions"]:
        bad = [k for k in ("permanentDelegate", "transferHook", "nonTransferable") if ext.get(k)]
        if (ext.get("defaultAccountState") or 0) == 2: bad.append("defaultFrozen")
        if bad: reasons.append("rug_token_ext (" + ",".join(bad) + ")")
    fee = _f((rep.get("transferFee") or {}).get("pct"))
    if rc["reject_transfer_fee"] and fee > 0: reasons.append(f"rug_transfer_fee ({fee:g}%)")
    lp, mtype = lp_locked_pct(rep, pair)
    if lp is None: reasons.append("rug_no_market (brak puli w RugCheck)")
    elif lp < rc["min_lp_locked_pct"]: reasons.append(f"rug_lp_unlocked (LP {lp:.0f}% < {rc['min_lp_locked_pct']:.0f}%, {mtype})")
    top10, top1 = holder_stats(rep)
    if top10 > rc["max_top10_pct"]: reasons.append(f"rug_top10 ({top10:.0f}% > {rc['max_top10_pct']:.0f}%)")
    if top1 > rc["max_single_holder_pct"]: reasons.append(f"rug_single_holder ({top1:.0f}% > {rc['max_single_holder_pct']:.0f}%)")
    supply = _f(tok.get("supply"))
    creator_pct = round(_f(rep.get("creatorBalance")) / supply * 100, 2) if supply > 0 else 0.0
    if creator_pct > rc["max_creator_pct"]: reasons.append(f"rug_creator ({creator_pct:.0f}% > {rc['max_creator_pct']:.0f}%)")
    sn = rep.get("score_normalised")
    if sn is not None and _f(sn) > rc["max_score_normalised"]: reasons.append(f"rug_score ({sn} > {rc['max_score_normalised']})")
    dangers = [r.get("name") for r in rep.get("risks") or [] if r.get("level") == "danger" and r.get("name") not in rc["ignore_danger_risks"]]
    if rc["reject_danger_risks"] and dangers: reasons.append("rug_danger (" + "; ".join(dangers)[:160] + ")")
    summary = {"score_norm": sn, "top10_pct": top10, "top1_pct": top1, "creator_pct": creator_pct, "lp_locked_pct": lp,
               "market": mtype, "dangers": dangers, "rugged": bool(rep.get("rugged")),
               "launchpad": (rep.get("launchpad") or {}).get("platform"), "insiders": rep.get("graphInsidersDetected")}
    return reasons, summary

def note_pl(ok, s, reasons):
    if s is None: return "RugCheck: niedostępny – kupno pominięte"
    lp = s.get("lp_locked_pct")
    lp_txt = "LP zablokowane/spalone" if lp is not None and lp >= 99 else (f"LP zablokowane {lp:.0f}%" if lp is not None else "LP ?")
    base = f"top10 {s['top10_pct']:.0f}%, największy {s['top1_pct']:.0f}%, {lp_txt}, ryzyko {s.get('score_norm')}/100"
    return ("RugCheck: OK, " if ok else "RugCheck: ODRZUT, ") + base

def check(mint, pair=None, c=None, fetch=None, now=None):
    rc = conf(c); now = time.time() if now is None else now
    if not rc["enabled"]:
        return {"ok": True, "reasons": [], "summary": None, "note_pl": "RugCheck: wyłączony", "cached": False}
    hit = _cache.get(mint)
    if hit and now - hit[0] < (rc["cache_s"] if hit[1]["summary"] is not None else rc["fail_cache_s"]):
        return dict(hit[1], cached=True)
    try:
        rep = (fetch or fetch_report)(mint, rc["timeout_s"])
        if not isinstance(rep, dict) or not (rep.get("mint") or rep.get("token") or rep.get("markets")):
            raise ValueError("unexpected RugCheck response")
        reasons, summary = evaluate(rep, rc, pair)
        res = {"ok": not reasons, "reasons": reasons, "summary": summary, "note_pl": note_pl(not reasons, summary, reasons)}
    except Exception as e:
        res = {"ok": False, "reasons": [f"rugcheck_unavailable ({type(e).__name__}: {str(e)[:80]})"], "summary": None,
               "note_pl": note_pl(False, None, None), "error": repr(e)[:200]}
    _cache[mint] = (now, res)
    if len(_cache) > 2000:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:500]: _cache.pop(k, None)
    return dict(res, cached=False)
