"""Public, read-only market/chain data for the PAPER copy desk. No keys, no wallet, no signing.
Solana public RPC (getSignaturesForAddress / getTransaction), DexScreener prices, Jupiter public quote API."""
import json, re, time, urllib.request, urllib.error
from core import WSOL, USDC

UA = {"User-Agent": "Mozilla/5.0 jarvis-copy-desk/1.0 (paper)", "Accept": "application/json"}

def http_json(url, data=None, timeout=10):
    h = dict(UA); body = None
    if data is not None:
        body = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

class RpcError(RuntimeError): pass

class Rpc:
    """Round-robin over public RPC URLs with short per-URL cool-down after errors / 429."""
    def __init__(self, urls, max_version=1, timeout=12):
        self.urls = list(urls); self.i = 0; self.cool = {}; self.timeout = timeout
        self.max_version = int(max_version); self.calls = 0; self.errors = 0; self.last_ok = None; self.last_error = None

    def call(self, method, params, attempts=None):
        err = None
        n = attempts or len(self.urls) * 2
        for k in range(n):
            url = self.urls[(self.i + k) % len(self.urls)]
            if time.time() < self.cool.get(url, 0) and k < n - 1: continue
            try:
                self.calls += 1
                r = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=self.timeout)
                if isinstance(r, dict) and "error" in r:
                    e = r["error"]; raise RpcError(json.dumps(e)[:300])
                self.i = (self.i + k) % len(self.urls); self.last_ok = time.time()
                return r.get("result")
            except RpcError as e:
                msg = str(e)
                if "not supported by the requesting client" in msg or "maxSupportedTransactionVersion" in msg:
                    raise                                   # version problem: caller adapts, other URLs would say the same
                err = e; self.errors += 1; self.cool[url] = time.time() + 5
            except Exception as e:
                err = e; self.errors += 1
                code = getattr(e, "code", None)
                self.cool[url] = time.time() + (30 if code in (403, 429) else 5)
            time.sleep(0.3 + 0.4 * k)
        self.last_error = repr(err)[:200]
        raise RpcError(f"{method} failed: {err!r}"[:300])

    def signatures(self, address, until=None, limit=100):
        opt = {"limit": limit, "commitment": "confirmed"}
        if until: opt["until"] = until
        return self.call("getSignaturesForAddress", [address, opt]) or []

    def first_signature_anywhere(self, address):
        """Newest signature from any configured RPC (some public RPCs keep only ~2 days of address history)."""
        for url in self.urls:
            try:
                r = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": [address, {"limit": 1}]}, timeout=self.timeout)
                res = (r or {}).get("result") or []
                if res: return res[0]
            except Exception:
                continue
        return None

    def transaction(self, sig):
        """getTransaction(jsonParsed) for any tx version. Starts with max_version (default 1) and adapts when the RPC
        reports a higher version ("Transaction version (N) is not supported") or rejects the parameter value.
        Returns None when the RPC does not have the tx yet (caller retries later; never blocks the loop)."""
        v = self.max_version
        for _ in range(4):
            try:
                return self.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": v,
                                                          "commitment": "confirmed"}])
            except RpcError as e:
                m = re.search(r"Transaction version \((\d+)\) is not supported", str(e))
                if m and int(m.group(1)) > v:
                    v = int(m.group(1)); self.max_version = max(self.max_version, v); continue
                if ("invalid" in str(e).lower() or "-32602" in str(e)) and v > 0:
                    v -= 1; continue
                raise
        return None

# ---------------------------------------------------------------- DexScreener
def ds_tokens(mints, timeout=10):
    """{mint: {price, liq, symbol, url, pair}} using the most liquid Solana pair per token (batch of <= 30)."""
    out = {}
    mints = [m for m in dict.fromkeys(mints) if m]
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        d = http_json("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk), timeout=timeout) or []
        if isinstance(d, dict): d = d.get("pairs") or []
        for p in d:
            base = (p.get("baseToken") or {}).get("address")
            if base not in chunk: continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            px = float(p.get("priceUsd") or 0)
            if px <= 0: continue
            if base not in out or liq > out[base]["liq"]:
                out[base] = {"price": px, "liq": liq, "symbol": (p.get("baseToken") or {}).get("symbol") or base[:6],
                             "url": p.get("url") or f"https://dexscreener.com/solana/{base}", "pair": p.get("pairAddress"),
                             "mcap": float(p.get("marketCap") or p.get("fdv") or 0)}
    return out

# ---------------------------------------------------------------- Jupiter (public quote API, no key)
JUP = "https://lite-api.jup.ag/swap/v1/quote"
_jup_cool = {"until": 0.0}

def jup_quote(input_mint, output_mint, amount_raw, slippage_bps=500, timeout=10):
    """outAmount (raw int) or None. Quote only – nothing is signed or sent."""
    if amount_raw <= 0 or time.time() < _jup_cool["until"]: return None
    url = (f"{JUP}?inputMint={input_mint}&outputMint={output_mint}&amount={int(amount_raw)}"
           f"&slippageBps={int(slippage_bps)}&restrictIntermediateTokens=true")
    try:
        q = http_json(url, timeout=timeout)
        return int(q["outAmount"]) if q and q.get("outAmount") else None
    except urllib.error.HTTPError as e:
        if e.code == 429: _jup_cool["until"] = time.time() + 20
        return None
    except Exception:
        return None

_sol = {"ts": 0.0, "px": 0.0, "src": None}
def sol_usd(max_age=90):
    if time.time() - _sol["ts"] < max_age and _sol["px"] > 0: return _sol["px"]
    out = jup_quote(WSOL, USDC, 1_000_000_000, 50)
    if out:
        _sol.update(ts=time.time(), px=out / 1e6, src="jupiter"); return _sol["px"]
    try:
        t = ds_tokens([WSOL]).get(WSOL)
        if t and t["price"] > 0:
            _sol.update(ts=time.time(), px=t["price"], src="dexscreener"); return _sol["px"]
    except Exception:
        pass
    return _sol["px"]
