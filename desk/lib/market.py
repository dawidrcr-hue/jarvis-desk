"""Public market data (DexScreener primary, GeckoTerminal for extra discovery). Read-only, no wallet."""
import time, threading
from concurrent.futures import ThreadPoolExecutor, wait
from lib.common import http_json, log

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
_last = {}
_throttle_lock = threading.Lock()
_pump_blocked = False

def _throttle(key, min_gap, deadline=None):
    """Reserve the next slot for this rate-limit key. Returns False if it would start after deadline."""
    with _throttle_lock:
        now = time.time()
        t = _last.get(key, 0)
        start = now if now >= t + min_gap else t + min_gap
        if deadline is not None and start > deadline:
            return False
        _last[key] = start
        w = start - now
    if w > 0:
        time.sleep(w)
    return True

def _over_deadline(deadline):
    return deadline is not None and time.time() >= deadline

JUP = "https://lite-api.jup.ag"
PUMP_V3 = "https://frontend-api-v3.pump.fun"
PUMP_ADV = "https://advanced-api-v2.pump.fun"
RAY_POOLS = "https://api-v3.raydium.io/pools/info/list"
RAY_LAUNCH = "https://launch-mint-v1.raydium.io/get/list"
QUOTE_MINTS = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
PUMP_H = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; jarvis-paper/1.0; paper-research)",
          "Origin": "https://pump.fun", "Referer": "https://pump.fun/"}

# Discovery feeds = the "old search" of /workspace/jarvis-desk scout (DexScreener lists + searches, pump.fun,
# Jupiter, Raydium/LaunchLab, GeckoTerminal) + the GT trending lists added in jarvis-paper. Public, read-only.
DS_LISTS = [("ds_profiles_latest", "/token-profiles/latest/v1"), ("ds_profiles_recent", "/token-profiles/recent-updates/v1"),
            ("ds_boosts_latest", "/token-boosts/latest/v1"), ("ds_boosts_top", "/token-boosts/top/v1"),
            ("ds_cto", "/community-takeovers/latest/v1")]
DS_SEARCH = ["pump", "SOL", "moon", "meme", "pumpswap", "pumpfun", "raydium", "letsbonk"]
JUP_LISTS = [("jup_toptrending_1h", "/tokens/v2/toptrending/1h"), ("jup_toporganic_1h", "/tokens/v2/toporganicscore/1h"),
             ("jup_toptrending_6h", "/tokens/v2/toptrending/6h"), ("jup_toporganic_6h", "/tokens/v2/toporganicscore/6h"),
             ("jup_recent", "/tokens/v2/recent")]
PUMP_LISTS = [("pump_graduated_p0", f"{PUMP_V3}/coins/graduated?offset=0&limit=50&includeNsfw=false"),
              ("pump_graduated_p1", f"{PUMP_V3}/coins/graduated?offset=50&limit=50&includeNsfw=false"),
              ("pump_complete_mcap", f"{PUMP_V3}/coins?offset=0&limit=50&sort=market_cap&order=DESC&includeNsfw=false&complete=true"),
              ("pump_adv_graduated", f"{PUMP_ADV}/coins/graduated")]
RAY_LISTS = [("ray_volume", f"{RAY_POOLS}?poolType=all&poolSortField=volume24h&sortType=desc&pageSize=50&page=1"),
             ("ray_standard", f"{RAY_POOLS}?poolType=standard&poolSortField=volume24h&sortType=desc&pageSize=50&page=1"),
             ("launchlab_lasttrade", f"{RAY_LAUNCH}?page=0&pageSize=50&sort=lastTrade"),
             ("launchlab_new", f"{RAY_LAUNCH}?page=0&pageSize=50&sort=new")]
GT_LISTS = [("gt_trending", "/networks/solana/trending_pools?page=1"), ("gt_trending", "/networks/solana/trending_pools?page=2"),
            ("gt_trend5m", "/networks/solana/trending_pools?duration=5m"), ("gt_trend1h", "/networks/solana/trending_pools?duration=1h"),
            ("gt_trend6h", "/networks/solana/trending_pools?duration=6h"), ("gt_top_tx", "/networks/solana/pools?sort=h24_tx_count_desc"),
            ("gt_new", "/networks/solana/new_pools?page=1"), ("gt_pumpswap_trend", "/networks/solana/trending_pools?dex=pumpswap"),
            ("gt_pumpfun", "/networks/solana/dexes/pump-fun/pools?page=1"), ("gt_pumpswap", "/networks/solana/dexes/pumpswap/pools?page=1"),
            ("gt_raydium", "/networks/solana/dexes/raydium/pools?page=1"), ("gt_raydium_clmm", "/networks/solana/dexes/raydium-clmm/pools?page=1"),
            ("gt_launchlab", "/networks/solana/dexes/raydium-launchlab/pools?page=1"), ("gt_letsbonk", "/networks/solana/dexes/letsbonk-fun/pools?page=1")]

def _rows(d):
    if isinstance(d, list): return [r for r in d if isinstance(r, dict)]
    if isinstance(d, dict):
        for k in ("coins", "data", "results", "items", "rows"):
            v = d.get(k)
            if isinstance(v, list): return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):
                r = _rows(v)
                if r: return r
    return []

def _add_stat(stats, src, n):
    prev = stats.get(src, 0)
    stats[src] = (prev if isinstance(prev, int) else 0) + n

def select_mints_to_price(wl, discovered, now, cfg):
    """Price newly discovered mints plus in-range / still-unknown watchlist entries. Skip known out-of-range or dead."""
    flt = (cfg or {}).get("filter") or {}
    min_m = float(flt.get("min_mcap_usd", 40000))
    max_m = float(flt.get("max_mcap_usd", 200000))
    dead = float((cfg or {}).get("dead_liq_usd", 1000))
    out = []
    for m, e in (wl or {}).items():
        if m in discovered:
            out.append(m)
            continue
        liq = e.get("last_liq")
        if liq is not None and liq < dead:
            continue
        mcap = e.get("last_mcap")
        if mcap is None:
            if e.get("miss_prices", 0) < 2 and now - e.get("last_seen", 0) < 1800:
                out.append(m)
            continue
        if min_m <= mcap <= max_m:
            out.append(m)
    return out

def annotate_and_trim(wl, priced_meta, discovered, attempted, now, cfg, watch_max=400, watch_ttl=6 * 3600):
    """Attach last mcap/liq and drop tokens outside the tradeable mcap range, dead, or stale."""
    flt = (cfg or {}).get("filter") or {}
    min_m = float(flt.get("min_mcap_usd", 40000))
    max_m = float(flt.get("max_mcap_usd", 200000))
    dead = float((cfg or {}).get("dead_liq_usd", 1000))
    wl = dict(wl or {})
    for m, e in wl.items():
        meta = (priced_meta or {}).get(m)
        if meta:
            e["last_mcap"] = meta.get("mcap")
            e["last_liq"] = meta.get("liq")
            e["last_priced"] = now
            e["miss_prices"] = 0
        elif m in (attempted or set()):
            e["miss_prices"] = e.get("miss_prices", 0) + 1
    keep = {}
    for m, e in wl.items():
        if now - e.get("last_seen", 0) >= watch_ttl:
            continue
        mcap, liq = e.get("last_mcap"), e.get("last_liq")
        if mcap is not None and not (min_m <= mcap <= max_m):
            continue
        if liq is not None and liq < dead:
            continue
        if mcap is None and e.get("miss_prices", 0) >= 2 and m not in (discovered or set()):
            continue
        keep[m] = e
    if len(keep) > watch_max:
        def rank(kv):
            m, e = kv
            in_range = 1 if e.get("last_mcap") is not None else 0
            return (in_range, e.get("last_seen", 0))
        keep = dict(sorted(keep.items(), key=rank, reverse=True)[:watch_max])
    return keep

def discover_mints(deadline=None, on_progress=None):
    """Collect candidate Solana mints from all feeds. Host groups run in parallel; each host stays rate-limited.
    Returns ({mint: [sources]}, {source: count|error})."""
    out, stats = {}, {}
    out_lock = threading.Lock()
    global _pump_blocked

    def add(m, src):
        if isinstance(m, str) and 32 <= len(m) <= 44 and m not in QUOTE_MINTS:
            with out_lock:
                out.setdefault(m, [])
                if src not in out[m]: out[m].append(src)
            return 1
        return 0

    def run(src, fn):
        if _over_deadline(deadline):
            with out_lock:
                if src not in stats:
                    stats[src] = "ERR scan budget"
            return
        if on_progress:
            try: on_progress(src)
            except Exception: pass
        try:
            n = fn()
            with out_lock:
                _add_stat(stats, src, n)
        except Exception as e:
            msg = str(e)[:80]
            if "403" in msg and src.startswith("pump"):
                msg = "blocked (Cloudflare 403) - skipped, no bypass"
                _pump_blocked = True
            with out_lock:
                if not isinstance(stats.get(src), int):
                    stats[src] = "ERR " + msg

    def group_ds():
        for src, path in DS_LISTS:
            def f(path=path, src=src):
                if not _throttle("ds_slow", 1.1, deadline): raise TimeoutError("scan deadline")
                return sum(add(x.get("tokenAddress"), src) for x in http_json(DS + path, deadline=deadline) or [] if x.get("chainId") == "solana")
            run(src, f)
        for q in DS_SEARCH:
            def f(q=q):
                if not _throttle("ds_slow", 1.1, deadline): raise TimeoutError("scan deadline")
                return sum(add((p.get("baseToken") or {}).get("address"), f"ds_search_{q}")
                           for p in (http_json(f"{DS}/latest/dex/search?q={q}", deadline=deadline) or {}).get("pairs") or [] if p.get("chainId") == "solana")
            run(f"ds_search_{q}", f)

    def group_jup():
        for src, path in JUP_LISTS:
            def f(path=path, src=src):
                if not _throttle("jup", 3.0, deadline): raise TimeoutError("scan deadline")
                return sum(add(t.get("id"), src) for t in _rows(http_json(JUP + path, deadline=deadline)))
            run(src, f)

    def group_pump():
        global _pump_blocked
        if _pump_blocked:
            for src, _url in PUMP_LISTS:
                with out_lock:
                    stats[src] = "ERR blocked (Cloudflare 403) - skipped, no bypass"
            return
        for src, url in PUMP_LISTS:
            if _pump_blocked:
                with out_lock:
                    stats[src] = "ERR blocked (Cloudflare 403) - skipped, no bypass"
                continue
            def f(url=url, src=src):
                if not _throttle("pump", 0.5, deadline): raise TimeoutError("scan deadline")
                return sum(add(r.get("mint") or r.get("coinMint"), src) for r in _rows(http_json(url, headers=PUMP_H, timeout=4, deadline=deadline)))
            run(src, f)

    def group_ray():
        for src, url in RAY_LISTS:
            def f(url=url, src=src):
                if not _throttle("ray", 0.5, deadline): raise TimeoutError("scan deadline")
                n = 0
                for r in _rows(http_json(url, deadline=deadline)):
                    if "mint" in r and isinstance(r["mint"], str): n += add(r["mint"], src)
                    for k in ("mintA", "mintB"):
                        v = r.get(k)
                        if isinstance(v, dict): n += add(v.get("address"), src)
                return n
            run(src, f)

    def group_gt():
        for src, path in GT_LISTS:
            def f(path=path, src=src):
                if not _throttle("gt", 2.2, deadline): raise TimeoutError("scan deadline")
                n = 0
                for p in (http_json(GT + path, deadline=deadline) or {}).get("data", []):
                    bid = ((p.get("relationships") or {}).get("base_token") or {}).get("data", {}).get("id", "")
                    if bid.startswith("solana_"): n += add(bid[7:], src)
                return n
            run(src, f)

    groups = (group_ds, group_jup, group_pump, group_ray, group_gt)
    remain = None if deadline is None else max(0.2, deadline - time.time())
    with ThreadPoolExecutor(max_workers=len(groups)) as ex:
        futs = [ex.submit(g) for g in groups]
        wait(futs, timeout=remain)
        # Let in-flight HTTP (max 8s) finish; don't start new work past deadline.
    return out, stats

def pairs_for_mints(mints, deadline=None, on_progress=None):
    """DexScreener /tokens/v1/solana/<<=30 mints>. Returns {mint: best pair (highest liquidity, mint as base)}."""
    best = {}
    mints = list(mints)
    chunks = [mints[i:i + 30] for i in range(0, len(mints), 30)]
    for n, chunk in enumerate(chunks, 1):
        if _over_deadline(deadline):
            break
        if on_progress:
            try: on_progress(f"price {n}/{len(chunks)}")
            except Exception: pass
        try:
            if not _throttle("ds_fast", 0.3, deadline):
                break
            data = http_json(f"{DS}/tokens/v1/solana/{','.join(chunk)}", deadline=deadline) or []
        except Exception as e:
            log("market", "tokens endpoint error", err=str(e)[:200]); continue
        if not isinstance(data, list):
            data = data.get("pairs") or []
        chunk_set = set(chunk)
        for p in data:
            m = (p.get("baseToken") or {}).get("address")
            if m not in chunk_set: continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if m not in best or liq > float((best[m].get("liquidity") or {}).get("usd") or 0):
                best[m] = p
    return best

def pair_prices(pair_addrs, deadline=None, on_progress=None):
    """Batch quote for specific pairs: {pairAddress: pair}. Used by the trader stop loop — must stay fast."""
    res = {}
    pair_addrs = list(pair_addrs)
    for i in range(0, len(pair_addrs), 30):
        if _over_deadline(deadline):
            break
        chunk = pair_addrs[i:i + 30]
        if on_progress:
            try: on_progress("quote")
            except Exception: pass
        try:
            if not _throttle("ds_fast", 0.3, deadline):
                break
            d = http_json(f"{DS}/latest/dex/pairs/solana/{','.join(chunk)}", deadline=deadline) or {}
            for p in d.get("pairs") or []:
                res[p.get("pairAddress")] = p
        except Exception as e:
            log("market", "pairs endpoint error", err=str(e)[:200])
    return res
