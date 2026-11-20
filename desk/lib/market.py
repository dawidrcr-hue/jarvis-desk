"""Public market data (DexScreener primary, GeckoTerminal for extra discovery). Read-only, no wallet."""
import time, threading, urllib.error
from concurrent.futures import ThreadPoolExecutor, wait
from lib.common import http_json, log

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
_last = {}
_throttle_lock = threading.Lock()
_pump_blocked = False
_rot = {}
_cool_until = {}
_unpriceable = {}  # mint -> {miss, until, last_attempt}; survives eviction within a run

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

def _cooling(key):
    return time.time() < _cool_until.get(key, 0)

def _set_cool(key, sec):
    with _throttle_lock:
        _cool_until[key] = max(_cool_until.get(key, 0), time.time() + sec)

def _take_rotated(key, items, n):
    """Return n items from the list, advancing a persistent cursor so every cycle sees a new slice."""
    if not items:
        return []
    n = max(0, min(int(n), len(items)))
    if n == 0:
        return []
    with _throttle_lock:
        i = _rot.get(key, 0)
        _rot[key] = i + n
    return [items[(i + k) % len(items)] for k in range(n)]

def reset_discovery_state():
    """Test helper: clear rotation, cooldowns, unpriceable cache and throttle clocks."""
    with _throttle_lock:
        _rot.clear(); _cool_until.clear(); _unpriceable.clear(); _last.clear()

def miss_backoff_s(miss):
    """Seconds to wait before retrying a mint DexScreener did not price. miss 0-1: immediate."""
    miss = int(miss or 0)
    if miss <= 1:
        return 0
    return min(3600, 180 * (2 ** (miss - 2)))  # 180, 360, 720, 1440, 2880 -> 3600

def remember_unpriceable(wl):
    """Seed the in-memory unpriceable cache from a loaded watchlist (after restore / each cycle)."""
    for m, e in (wl or {}).items():
        if e.get("last_mcap") is None and int(e.get("miss_prices") or 0) >= 3:
            _unpriceable[m] = {"miss": int(e.get("miss_prices") or 0),
                               "until": float(e.get("skip_until") or 0),
                               "last_attempt": float(e.get("last_attempt") or 0)}

def hydrate_watch_entry(m, e):
    """If this mint was previously unpriceable and evicted, restore miss/backoff onto a rediscovered entry."""
    u = _unpriceable.get(m)
    if not u:
        return e
    if int(e.get("miss_prices") or 0) < u.get("miss", 0):
        e["miss_prices"] = u["miss"]
        if u.get("until"):
            e["skip_until"] = u["until"]
        if u.get("last_attempt"):
            e["last_attempt"] = u["last_attempt"]
    return e

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
# Hosts are rate-limited; GT/Jup/DS-search rotate across cycles so we cover more endpoints without 429s.
DS_LISTS = [("ds_profiles_latest", "/token-profiles/latest/v1"), ("ds_profiles_recent", "/token-profiles/recent-updates/v1"),
            ("ds_boosts_latest", "/token-boosts/latest/v1"), ("ds_boosts_top", "/token-boosts/top/v1"),
            ("ds_cto", "/community-takeovers/latest/v1")]
DS_SEARCH = ["pump", "SOL", "moon", "meme", "pumpswap", "pumpfun", "raydium", "letsbonk",
             "meteora", "orca", "moonshot", "launchlab", "bags"]
DS_SEARCH_PER_CYCLE = 5
JUP_LISTS = [("jup_toptrending_1h", "/tokens/v2/toptrending/1h"), ("jup_toporganic_1h", "/tokens/v2/toporganicscore/1h"),
             ("jup_toptrending_6h", "/tokens/v2/toptrending/6h"), ("jup_toporganic_6h", "/tokens/v2/toporganicscore/6h"),
             ("jup_recent", "/tokens/v2/recent")]
JUP_PER_CYCLE = 2
PUMP_LISTS = [("pump_graduated_p0", f"{PUMP_V3}/coins/graduated?offset=0&limit=50&includeNsfw=false"),
              ("pump_graduated_p1", f"{PUMP_V3}/coins/graduated?offset=50&limit=50&includeNsfw=false"),
              ("pump_complete_mcap", f"{PUMP_V3}/coins?offset=0&limit=50&sort=market_cap&order=DESC&includeNsfw=false&complete=true"),
              ("pump_adv_graduated", f"{PUMP_ADV}/coins/graduated")]
RAY_LISTS = [("ray_volume", f"{RAY_POOLS}?poolType=all&poolSortField=volume24h&sortType=desc&pageSize=50&page=1"),
             ("ray_standard", f"{RAY_POOLS}?poolType=standard&poolSortField=volume24h&sortType=desc&pageSize=50&page=1"),
             ("launchlab_lasttrade", f"{RAY_LAUNCH}?page=0&pageSize=50&sort=lastTrade"),
             ("launchlab_new", f"{RAY_LAUNCH}?page=0&pageSize=50&sort=new")]
# Core = better fit for variant S (6h+ / already-trending). More = pagination + extra dexes, rotated.
GT_CORE = [("gt_trend6h", "/networks/solana/trending_pools?duration=6h"),
           ("gt_trend1h", "/networks/solana/trending_pools?duration=1h"),
           ("gt_trending", "/networks/solana/trending_pools?page=1"),
           ("gt_top_vol", "/networks/solana/pools?sort=h24_volume_usd_desc"),
           ("gt_top_tx", "/networks/solana/pools?sort=h24_tx_count_desc")]
GT_MORE = [("gt_trending", "/networks/solana/trending_pools?page=2"),
           ("gt_trending", "/networks/solana/trending_pools?page=3"),
           ("gt_trend5m", "/networks/solana/trending_pools?duration=5m"),
           ("gt_trend6h_p2", "/networks/solana/trending_pools?duration=6h&page=2"),
           ("gt_trend1h_p2", "/networks/solana/trending_pools?duration=1h&page=2"),
           ("gt_new", "/networks/solana/new_pools?page=1"),
           ("gt_new_p2", "/networks/solana/new_pools?page=2"),
           ("gt_top_tx_p2", "/networks/solana/pools?sort=h24_tx_count_desc&page=2"),
           ("gt_pumpswap_trend", "/networks/solana/trending_pools?dex=pumpswap"),
           ("gt_pumpfun", "/networks/solana/dexes/pump-fun/pools?page=1"),
           ("gt_pumpfun_p2", "/networks/solana/dexes/pump-fun/pools?page=2"),
           ("gt_pumpswap", "/networks/solana/dexes/pumpswap/pools?page=1"),
           ("gt_pumpswap_p2", "/networks/solana/dexes/pumpswap/pools?page=2"),
           ("gt_raydium", "/networks/solana/dexes/raydium/pools?page=1"),
           ("gt_raydium_clmm", "/networks/solana/dexes/raydium-clmm/pools?page=1"),
           ("gt_launchlab", "/networks/solana/dexes/raydium-launchlab/pools?page=1"),
           ("gt_letsbonk", "/networks/solana/dexes/letsbonk-fun/pools?page=1"),
           ("gt_meteora", "/networks/solana/dexes/meteora/pools?page=1"),
           ("gt_meteora_dbc", "/networks/solana/dexes/meteora-dbc/pools?page=1"),
           ("gt_meteora_damm", "/networks/solana/dexes/meteora-damm-v2/pools?page=1"),
           ("gt_orca", "/networks/solana/dexes/orca/pools?page=1"),
           ("gt_moonshot", "/networks/solana/dexes/moonshot/pools?page=1"),
           ("gt_bags", "/networks/solana/dexes/bags-fm/pools?page=1")]
GT_CORE_PER_CYCLE = 2
GT_MORE_PER_CYCLE = 2
# Kept as the union so older imports/tests that iterate GT_LISTS still see every path.
GT_LISTS = GT_CORE + GT_MORE

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

def _still_in_backoff(e, now):
    miss = int(e.get("miss_prices") or 0)
    if e.get("last_mcap") is not None or miss < 2:
        return False
    until = float(e.get("skip_until") or 0)
    if until:
        return now < until
    last = float(e.get("last_attempt") or 0)
    return last and (now - last) < miss_backoff_s(miss)

def select_mints_to_price(wl, discovered, now, cfg):
    """Price newly discovered mints plus in-range / still-unknown watchlist entries. Skip known out-of-range or dead.
    Mints DexScreener repeatedly fails to price (LaunchLab / pre-list) are retried on a backoff even if rediscovered,
    so they do not consume the pricing budget or crowd out live tokens."""
    flt = (cfg or {}).get("filter") or {}
    min_m = float(flt.get("min_mcap_usd", 40000))
    max_m = float(flt.get("max_mcap_usd", 200000))
    dead = float((cfg or {}).get("dead_liq_usd", 1000))
    in_range, fresh, retry = [], [], []
    seen = set()

    def consider(m, e):
        if m in seen:
            return
        e = hydrate_watch_entry(m, dict(e or {}))
        if _still_in_backoff(e, now):
            return
        liq = e.get("last_liq")
        if liq is not None and liq < dead and m not in discovered:
            return
        mcap = e.get("last_mcap")
        if mcap is not None and not (min_m <= mcap <= max_m) and m not in discovered:
            return
        if mcap is not None and min_m <= mcap <= max_m:
            in_range.append(m); seen.add(m); return
        # Unpriced and not rediscovered: at most two immediate tries, and only if seen recently.
        if mcap is None and m not in discovered:
            if int(e.get("miss_prices") or 0) >= 2:
                return
            if now - e.get("last_seen", 0) >= 1800:
                return
        elif mcap is not None and m not in discovered:
            return
        if int(e.get("miss_prices") or 0) == 0:
            fresh.append(m)
        else:
            retry.append(m)
        seen.add(m)

    for m, e in (wl or {}).items():
        consider(m, e)
    for m in discovered or []:
        if m not in seen:
            consider(m, (wl or {}).get(m) or _unpriceable.get(m) or {})
    return in_range + fresh + retry

def annotate_and_trim(wl, priced_meta, discovered, attempted, now, cfg, watch_max=400, watch_ttl=6 * 3600, tomb_max=200):
    """Attach last mcap/liq and drop tokens outside the tradeable mcap range, dead, or stale.
    Persistent DexScreener misses become tombstones (capped) so rediscovery does not reset the backoff."""
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
            e.pop("skip_until", None)
            _unpriceable.pop(m, None)
        elif m in (attempted or set()):
            e["miss_prices"] = int(e.get("miss_prices") or 0) + 1
            e["last_attempt"] = now
            if e.get("last_mcap") is None and e["miss_prices"] >= 2:
                e["skip_until"] = now + miss_backoff_s(e["miss_prices"])
                _unpriceable[m] = {"miss": e["miss_prices"], "until": e["skip_until"], "last_attempt": now}
    live, tombs = {}, {}
    for m, e in wl.items():
        if now - e.get("last_seen", 0) >= watch_ttl:
            continue
        mcap, liq = e.get("last_mcap"), e.get("last_liq")
        if mcap is not None and not (min_m <= mcap <= max_m):
            continue
        if liq is not None and liq < dead:
            continue
        miss = int(e.get("miss_prices") or 0)
        if mcap is None and miss >= 8 and now - e.get("last_seen", 0) >= 7200:
            continue
        # Same as before: unpriced misses that were not rediscovered this round are dropped.
        if mcap is None and miss >= 2 and m not in (discovered or set()):
            continue
        if mcap is None and miss >= 3:
            tombs[m] = e
        else:
            live[m] = e
    if len(live) > watch_max:
        def rank(kv):
            m, e = kv
            in_range = 1 if e.get("last_mcap") is not None else 0
            return (in_range, e.get("last_seen", 0))
        live = dict(sorted(live.items(), key=rank, reverse=True)[:watch_max])
    if len(tombs) > tomb_max:
        tombs = dict(sorted(tombs.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)[:tomb_max])
    keep = live
    keep.update(tombs)
    return keep

def _http(url, host, gap, deadline=None, headers=None, timeout=None):
    """http_json with per-host throttle and a short cooldown after 429 (no long backoff)."""
    if _cooling(host):
        raise TimeoutError(f"{host} cooling after 429")
    if not _throttle(host, gap, deadline):
        raise TimeoutError("scan deadline")
    try:
        return http_json(url, headers=headers, timeout=timeout, deadline=deadline)
    except urllib.error.HTTPError as e:
        if getattr(e, "code", None) == 429:
            wait = 30
            try:
                ra = (e.headers or {}).get("Retry-After")
                if ra:
                    wait = min(60, max(5, int(float(ra))))
            except Exception:
                pass
            _set_cool(host, wait)
        raise

def _seed_pair(seed, mint, pair):
    if not mint or not isinstance(pair, dict) or mint in QUOTE_MINTS:
        return
    if (pair.get("baseToken") or {}).get("address") != mint:
        return
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    cur = seed.get(mint)
    if cur is None or liq > float((cur.get("liquidity") or {}).get("usd") or 0):
        seed[mint] = pair

def discover_mints(deadline=None, on_progress=None):
    """Collect candidate Solana mints from all feeds. Host groups run in parallel; each host stays rate-limited.
    Returns ({mint: [sources]}, {source: count|error}, {mint: dex pair already fetched this cycle})."""
    out, stats, seed = {}, {}, {}
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
                return sum(add(x.get("tokenAddress"), src) for x in _http(DS + path, "ds_slow", 1.1, deadline) or [] if x.get("chainId") == "solana")
            run(src, f)
        for q in _take_rotated("ds_search", DS_SEARCH, DS_SEARCH_PER_CYCLE):
            def f(q=q):
                data = _http(f"{DS}/latest/dex/search?q={q}", "ds_slow", 1.1, deadline) or {}
                n = 0
                for p in data.get("pairs") or []:
                    if p.get("chainId") != "solana":
                        continue
                    m = (p.get("baseToken") or {}).get("address")
                    n += add(m, f"ds_search_{q}")
                    with out_lock:
                        _seed_pair(seed, m, p)
                return n
            run(f"ds_search_{q}", f)

    def group_jup():
        for src, path in _take_rotated("jup_lists", JUP_LISTS, JUP_PER_CYCLE):
            def f(path=path, src=src):
                return sum(add(t.get("id"), src) for t in _rows(_http(JUP + path, "jup", 3.0, deadline)))
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
                return sum(add(r.get("mint") or r.get("coinMint"), src) for r in _rows(_http(url, "pump", 0.5, deadline, headers=PUMP_H, timeout=4)))
            run(src, f)

    def group_ray():
        for src, url in RAY_LISTS:
            def f(url=url, src=src):
                n = 0
                for r in _rows(_http(url, "ray", 0.5, deadline)):
                    if "mint" in r and isinstance(r["mint"], str): n += add(r["mint"], src)
                    for k in ("mintA", "mintB"):
                        v = r.get(k)
                        if isinstance(v, dict): n += add(v.get("address"), src)
                return n
            run(src, f)

    def group_gt():
        planned = _take_rotated("gt_core", GT_CORE, GT_CORE_PER_CYCLE) + _take_rotated("gt_more", GT_MORE, GT_MORE_PER_CYCLE)
        for src, path in planned:
            if _cooling("gt"):
                with out_lock:
                    if src not in stats:
                        stats[src] = "ERR cooling after 429"
                continue
            def f(path=path, src=src):
                n = 0
                for p in (_http(GT + path, "gt", 2.5, deadline) or {}).get("data", []):
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
    return out, stats, seed

def pairs_for_mints(mints, deadline=None, on_progress=None, seed=None):
    """DexScreener /tokens/v1/solana/<<=30 mints>. Returns {mint: best pair (highest liquidity, mint as base)}.
    `seed` is optional {mint: pair} already fetched (e.g. from DexScreener search this cycle) — those mints are not re-requested."""
    want = set(mints)
    best = {}
    if seed:
        for m, p in seed.items():
            if m in want and (p.get("baseToken") or {}).get("address") == m:
                best[m] = p
    rest = [m for m in list(mints) if m not in best]
    chunks = [rest[i:i + 30] for i in range(0, len(rest), 30)]
    for n, chunk in enumerate(chunks, 1):
        if _over_deadline(deadline):
            break
        if on_progress:
            try: on_progress(f"price {n}/{len(chunks)}")
            except Exception: pass
        data = None
        for attempt in range(2):
            try:
                if not _throttle("ds_fast", 0.3, deadline):
                    data = None
                    break
                data = http_json(f"{DS}/tokens/v1/solana/{','.join(chunk)}", deadline=deadline) or []
                break
            except urllib.error.HTTPError as e:
                if getattr(e, "code", None) == 429:
                    _set_cool("ds_fast", 10)
                    log("market", "tokens endpoint 429", attempt=attempt)
                    if attempt == 0 and not _over_deadline(deadline):
                        time.sleep(min(1.5, max(0.2, (deadline or time.time() + 1.5) - time.time())))
                        continue
                log("market", "tokens endpoint error", err=str(e)[:200]); data = None; break
            except Exception as e:
                log("market", "tokens endpoint error", err=str(e)[:200]); data = None; break
        if data is None:
            continue
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
