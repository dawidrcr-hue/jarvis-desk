"""Read-only view of the REAL wallet that the box routine trades with Phantom (public on-chain data only).
No keys, no signing, nothing is ever sent from here: getBalance, getTokenAccountsByOwner, getSignaturesForAddress and
getTransaction on a public RPC, prices from DexScreener. State: copydesk/state/real.json (committed, public data)."""
import time
import core

TOKEN_PROGRAMS = ["TokenkegQfeZyiNwAJbNbGqPxSWuJTQ7GpvYGyUQRjn", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]

def new_state(address, since_ts):
    return {"address": address, "since_ts": since_ts, "last_sig": None, "queue": [], "handled": [], "trades": [],
            "holdings": {}, "sol": None, "equity_usd": None, "start_equity_usd": None, "start_ts": None,
            "equity_history": [], "updated_ts": None, "last_error": None}

def classify_real(tx, owner, sol_usd):
    """Swaps of the real wallet: token in with SOL/stable out = buy, token out with SOL/stable in = sell, else transfer."""
    if not tx or ((tx.get("meta") or {}).get("err")): return []
    sol, stable = core.quote_spent(tx, owner)
    out = []
    for d in core.token_deltas(tx, owner):
        qty = abs(d["post"] - d["pre"])
        if d["side"] == "buy":
            side = "buy" if (sol > 0.0005 or stable > 0.05) else "transfer_in"
            usd = max(0.0, sol) * sol_usd + max(0.0, stable)
            sol_amt = max(0.0, sol)
        else:
            side = "sell" if (sol < -0.0005 or stable < -0.05) else "transfer_out"
            usd = max(0.0, -sol) * sol_usd + max(0.0, -stable)
            sol_amt = max(0.0, -sol)
        out.append({"mint": d["mint"], "side": side, "qty": qty, "sol": round(sol_amt, 6), "usd": round(usd, 4),
                    "price": usd / qty if qty > 0 and usd > 0 else None, "ts": tx.get("blockTime"), "frac": d.get("frac")})
    return out

class RealWallet:
    def __init__(self, cfg, state, rpc, prices, solpx, clock=time.time, log=lambda *a, **k: None):
        self.cfg, self.st, self.rpc, self.prices, self.solpx, self.clock, self.log = cfg, state, rpc, prices, solpx, clock, log
        self.sym = {}

    def balances(self):
        addr = self.st["address"]
        lam = self.rpc.call("getBalance", [addr, {"commitment": "confirmed"}])
        sol = (lam.get("value") if isinstance(lam, dict) else lam) / 1e9
        hold = {}
        try:
            hold = self._tokens(addr); self.st["holdings_stale"] = False
        except Exception as e:                       # keep the last known token amounts; SOL balance still updates
            hold = {m: h["qty"] for m, h in (self.st.get("holdings") or {}).items()}; self.st["holdings_stale"] = True
            self.log("real token accounts unavailable", err=type(e).__name__)
        return sol, hold

    def _tokens(self, addr):
        hold = {}
        for prog in TOKEN_PROGRAMS:
            r = self.rpc.call("getTokenAccountsByOwner", [addr, {"programId": prog}, {"encoding": "jsonParsed", "commitment": "confirmed"}]) or {}
            for acc in r.get("value") or []:
                info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                mint = info.get("mint"); amt = core.ui_amount({"uiTokenAmount": info.get("tokenAmount") or {}})
                if mint and amt > 0 and mint not in core.QUOTES:
                    hold[mint] = hold.get(mint, 0.0) + amt
        return hold

    def poll_trades(self, sol_usd):
        st = self.st
        sigs = self.rpc.signatures(st["address"], until=st.get("last_sig"), limit=50)
        if sigs: st["last_sig"] = sigs[0]["signature"]
        st["queue"] = core.merge_queue(st["queue"], st["handled"], sigs, st["since_ts"])
        n = 0
        while st["queue"] and n < 8:
            q = st["queue"][0]; n += 1
            try: tx = self.rpc.transaction(q["sig"])
            except Exception: tx = None
            if not tx:
                q["tries"] += 1
                if q["tries"] < 8: break
                st["queue"].pop(0); st["handled"] = (st["handled"] + [q["sig"]])[-400:]; continue
            st["queue"].pop(0); st["handled"] = (st["handled"] + [q["sig"]])[-400:]
            for t in classify_real(tx, st["address"], sol_usd):
                t["sig"] = q["sig"]; st["trades"].append(t)
        st["trades"] = st["trades"][-200:]

    def update(self):
        st = self.st
        try:
            sol_usd = self.solpx() or 0.0
            sol, hold = self.balances()
            self.poll_trades(sol_usd)
            mints = list(hold) + [t["mint"] for t in st["trades"][-60:]]
            q = {}
            try: q = self.prices(list(dict.fromkeys(mints))) if mints else {}
            except Exception as e: self.log("real price error", err=type(e).__name__)
            for m, t in q.items(): self.sym[m] = t.get("symbol")
            holdings = {}
            for m, amt in hold.items():
                t = q.get(m)
                holdings[m] = {"qty": amt, "price": t["price"] if t else None, "value": amt * t["price"] if t else None,
                               "symbol": (t or {}).get("symbol") or st["holdings"].get(m, {}).get("symbol") or m[:6], "liq": (t or {}).get("liq")}
            for tr in st["trades"]:
                tr.setdefault("symbol", None)
                if not tr["symbol"] and self.sym.get(tr["mint"]): tr["symbol"] = self.sym[tr["mint"]]
            st["holdings"] = holdings; st["sol"] = sol; st["sol_usd"] = sol_usd
            eq = sol * sol_usd + sum(h["value"] or 0 for h in holdings.values())
            st["equity_usd"] = eq
            if st.get("start_equity_usd") is None and sol_usd > 0:
                st["start_equity_usd"] = eq; st["start_ts"] = self.clock()
            now = self.clock()
            if not st["equity_history"] or now - st["equity_history"][-1][0] >= 60:
                st["equity_history"].append([int(now), round(eq, 4)]); st["equity_history"] = core.thin_history(st["equity_history"], 2000)
            st["updated_ts"] = now; st["last_error"] = None
        except Exception as e:
            st["last_error"] = type(e).__name__ + ": " + str(e)[:120]
            self.log("real wallet error", err=st["last_error"])

    def view(self):
        st = self.st
        per = {}
        for t in st["trades"]:
            if t["side"] not in ("buy", "sell"): continue
            p = per.setdefault(t["mint"], {"bought_usd": 0.0, "sold_usd": 0.0, "buys": 0, "sells": 0, "first_ts": t["ts"]})
            if t["side"] == "buy": p["bought_usd"] += t["usd"]; p["buys"] += 1
            else: p["sold_usd"] += t["usd"]; p["sells"] += 1
        pos = []
        for m, h in st["holdings"].items():
            c = per.get(m)
            pnl = (h["value"] or 0) + c["sold_usd"] - c["bought_usd"] if c and c["buys"] and h["value"] is not None else None
            pos.append({"mint": m, "symbol": h["symbol"], "qty": h["qty"], "price": h["price"], "value": h["value"],
                        "cost": c["bought_usd"] - c["sold_usd"] if c else None, "pnl_usd": pnl,
                        "pnl_pct": pnl / c["bought_usd"] if pnl is not None and c["bought_usd"] > 0 else None,
                        "tracked": bool(c and c["buys"]), "since_ts": c["first_ts"] if c else None,
                        "url": core.ds_link(m)})
        pos.sort(key=lambda p: -(p["value"] or 0))
        trades = [{"sig": t["sig"], "solscan": f"https://solscan.io/tx/{t['sig']}", "ts": t["ts"], "side": t["side"], "mint": t["mint"],
                   "symbol": t.get("symbol") or t["mint"][:6], "qty": t["qty"], "sol": t["sol"], "usd": t["usd"], "url": core.ds_link(t["mint"])}
                  for t in st["trades"][-60:]][::-1]
        eq, s0 = st.get("equity_usd"), st.get("start_equity_usd")
        return {"address": st["address"], "solscan": f"https://solscan.io/account/{st['address']}", "sol": st.get("sol"),
                "sol_usd": st.get("sol_usd"), "equity_usd": eq, "start_equity_usd": s0, "start_ts": st.get("start_ts"),
                "change_usd": (eq - s0) if eq is not None and s0 is not None else None,
                "change_pct": (eq - s0) / s0 if eq is not None and s0 else None, "since_ts": st["since_ts"],
                "positions": pos, "trades": trades, "equity_history": st["equity_history"][-1000:],
                "updated_ts": st.get("updated_ts"), "last_error": st.get("last_error"), "holdings_stale": st.get("holdings_stale"),
                "n_swaps": sum(1 for t in st["trades"] if t["side"] in ("buy", "sell"))}
