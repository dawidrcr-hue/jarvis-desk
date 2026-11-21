#!/usr/bin/env python3
"""PAPER copy desk runner (GitHub Actions or local). Simulation only: never touches a wallet, Phantom, keys or funds.

Copies public Solana wallets onto one $25 paper book (copydesk/state/book.json):
  * new buys after the copy start only (holdings from before the start are never copied)
  * alloc % of current equity per new position (sized down to free cash, skipped under $0.50)
  * sells proportional to the wallet's sold fraction of its holding; paid add-ons proportional, capped at 40% equity per token
  * airdrops / free transfers skipped
  * fills at OUR detection time: Jupiter public quote (pool fee + price impact) + Phantom 0.85% per side + network fee
Writes data.json (site), copydesk/notes.jsonl (Polish trade notes, JSON lines) and commits them back to main at most once
per publish_min_interval_s (>= 60 s). Graceful handoff: commit copydesk/control/stop.json {"run_id": "<run id>"}.
Env: DESK_RUN_SECONDS (default 20400), DESK_NO_GIT=1 (local test, no commits), GITHUB_RUN_ID."""
import os, sys, json, time, uuid, signal, subprocess, traceback, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import core, net, hook, real
from core import fmt_usd

P = lambda *a: os.path.join(HERE, *a)
BOOK = P("state", "book.json")
REAL = P("state", "real.json")
NOTES = P("notes.jsonl")
DATA = os.path.join(REPO, "data.json")
STOP = P("control", "stop.json")
RUN_ID = os.environ.get("GITHUB_RUN_ID") or f"local-{int(time.time())}"
NO_GIT = os.environ.get("DESK_NO_GIT") == "1"
RUN_S = int(os.environ.get("DESK_RUN_SECONDS", "20400"))

def now_iso(ts=None):
    return datetime.datetime.fromtimestamp(ts or time.time()).astimezone().isoformat(timespec="seconds")

def log(msg, **kw):
    line = f"{now_iso()} {msg}" + ("" if not kw else " " + json.dumps(kw, ensure_ascii=False, default=str))
    print(line, flush=True)
    try:
        os.makedirs(P("logs"), exist_ok=True)
        with open(P("logs", "desk.log"), "a") as f: f.write(line + "\n")
    except OSError: pass

def load(path, default=None):
    try:
        with open(path) as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return default

def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f: json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), default=str)
    os.replace(tmp, path)

def load_cfg(): return load(P("config.json"))

# ---------------------------------------------------------------- notes + feed
def add_feed(book, cfg, kind, text, w=None, mint=None, url=None, ts=None):
    book["feed"].append({"ts": ts or time.time(), "kind": kind, "text": text, "wallet": (w or {}).get("key"),
                         "label": (w or {}).get("label"), "mint": mint, "url": url})
    book["feed"] = book["feed"][-int(cfg["feed_keep"]):]

def write_note(kind, title, message, data, cfg):
    if data.get("mint") and data.get("alloc_pct") is not None and "Mint:" not in message:
        message = f"{message} Mint: {data['mint']} · alokacja portfela {int(round(float(data['alloc_pct']) * 100))}%."
    ev = {"id": uuid.uuid4().hex[:12], "ts": now_iso(), "type": kind, "title": title, "message": message, "data": data}
    lines = []
    try:
        with open(NOTES) as f: lines = [l for l in f.read().splitlines() if l.strip()]
    except FileNotFoundError: pass
    lines.append(json.dumps(ev, ensure_ascii=False, default=str))
    lines = lines[-int(cfg["notes_keep"]):]
    tmp = NOTES + ".tmp"
    with open(tmp, "w") as f: f.write("\n".join(lines) + "\n")
    os.replace(tmp, NOTES)
    try: hook.send(ev, log)          # optional signal to the live-copy routine (secrets via env only)
    except Exception as e: log("webhook error", err=type(e).__name__)
    return ev

# ---------------------------------------------------------------- desk
class Desk:
    def __init__(self, cfg, rpc=None, prices=None, quote=None, solpx=None, clock=time.time):
        self.cfg = cfg
        self.rpc = rpc or net.Rpc(cfg["rpc_urls"], cfg.get("max_tx_version", 1))
        self.prices = prices or net.ds_tokens
        self.quote = quote or net.jup_quote
        self.solpx = solpx or net.sol_usd
        self.clock = clock
        self.book = load(BOOK) or core.new_book(cfg, clock())
        self.wallets = cfg["wallets"]
        for w in self.wallets:      # wallets added later start copying from when they were added, not from the book start
            core.wallet_state(self.book, w, self.book["start_ts"] if w["key"] in self.book["wallets"] or not self.book.get("equity_history") else clock())
        self.next_poll = {w["key"]: 0.0 for w in self.wallets}
        self.last_px = 0.0; self.last_eq = 0.0; self.price_ok_ts = None; self.price_err = None
        self.sym_cache = {}
        self.dirty = True
        self.real = None; self.last_real = 0.0
        rw = cfg.get("real_wallet") or {}
        if rw.get("address"):
            since = datetime.datetime.fromisoformat(rw["since"]).timestamp() if rw.get("since") else clock()
            st = load(REAL) or real.new_state(rw["address"], since)
            if st.get("address") != rw["address"]: st = real.new_state(rw["address"], since)
            self.real = real.RealWallet(cfg, st, self.rpc, self.prices, self.solpx, clock, log)

    # ---- helpers
    def eq(self): return core.equity(self.book)

    def market(self, mint):
        try:
            t = self.prices([mint]).get(mint)
        except Exception as e:
            log("price error", mint=mint, err=repr(e)[:150]); t = None
        if t: self.sym_cache[mint] = t.get("symbol")
        return t

    def symbol(self, mint):
        if mint not in self.sym_cache:
            t = self.market(mint)
            self.sym_cache[mint] = (t or {}).get("symbol") or mint[:6]
        return self.sym_cache[mint]

    # ---- wallet polling (never blocks: failed tx fetches are retried later, then dropped)
    def poll_wallet(self, w):
        st = self.book["wallets"][w["key"]]
        now = self.clock()
        try:
            sigs = self.rpc.signatures(w["address"], until=st.get("last_sig"), limit=100)
        except Exception as e:
            st["errors"] += 1; st["last_error"] = repr(e)[:160]; log("signatures error", wallet=w["key"], err=repr(e)[:200])
            return
        st["last_poll_ts"] = now; st["last_error"] = None
        if sigs:
            bt = max((s.get("blockTime") or 0) for s in sigs)
            if bt: st["last_activity_ts"] = max(st.get("last_activity_ts") or 0, bt)
            st["last_sig"] = sigs[0]["signature"]
        elif not st.get("last_activity_ts") and not st.get("history_probed") and hasattr(self.rpc, "first_signature_anywhere"):
            st["history_probed"] = True
            try:
                s0 = self.rpc.first_signature_anywhere(w["address"])
                if s0 and s0.get("blockTime"): st["last_activity_ts"] = s0["blockTime"]
            except Exception: pass
        st["queue"] = core.merge_queue(st["queue"], st["handled"], sigs, st["start_ts"])
        n = 0
        while st["queue"] and n < 10:
            q = st["queue"][0]; n += 1
            try:
                tx = self.rpc.transaction(q["sig"])
            except Exception as e:
                tx = None; log("tx fetch error", wallet=w["key"], sig=q["sig"][:12], err=repr(e)[:160])
            if not tx:
                q["tries"] += 1
                if q["tries"] >= int(self.cfg["tx_retry_max"]) or now - q["first_seen"] > float(self.cfg["tx_retry_max_age_s"]):
                    st["queue"].pop(0); self._handled(st, q["sig"])
                    add_feed(self.book, self.cfg, "error", f"Nie udało się pobrać transakcji {q['sig'][:8]}… portfela {w['label']} – pomijam.", w)
                    log("tx dropped after retries", wallet=w["key"], sig=q["sig"])
                    continue
                break       # keep order: retry the same tx on the next poll
            st["queue"].pop(0)
            try:
                self.handle_tx(w, st, q["sig"], tx)
            except Exception as e:
                log("handle_tx error", wallet=w["key"], sig=q["sig"], err=repr(e)[:200], tb=traceback.format_exc()[-600:])
                add_feed(self.book, self.cfg, "error", f"Błąd przy transakcji {q['sig'][:8]}… ({w['label']}) – pominięta.", w)
            self._handled(st, q["sig"])
            self.dirty = True

    def _handled(self, st, sig):
        st["handled"] = (st["handled"] + [sig])[-600:]

    def handle_tx(self, w, st, sig, tx):
        if (tx.get("blockTime") or 0) and tx["blockTime"] < st["start_ts"]: return
        for ev in core.classify(tx, w["address"], self.cfg):
            k = ev["kind"]
            if k == "airdrop":
                st["airdrops_skipped"] += 1; continue
            st["last_trade_ts"] = max(st.get("last_trade_ts") or 0, ev.get("block_time") or 0)
            pos = core.find_pos(self.book, w["key"], ev["mint"])
            if k == "new" and not pos: self.copy_buy(w, st, ev, sig)
            elif k in ("new", "addon") and pos: self.copy_addon(w, st, pos, ev, sig)
            elif k == "addon":
                st["skipped"] += 1
                add_feed(self.book, self.cfg, "skip", f"{w['label']} dokupił ${self.symbol(ev['mint'])} (pozycja sprzed startu kopii) – pomijam.",
                         w, ev["mint"], core.ds_link(ev["mint"]), ev.get("block_time"))
            elif k == "sell" and pos:
                if ev["sell_frac"] >= float(self.cfg["min_sell_fraction"]): self.copy_sell(w, st, pos, ev, sig)
            elif k == "sell":
                add_feed(self.book, self.cfg, "info", f"{w['label']} sprzedał {ev['sell_frac']*100:.0f}% ${self.symbol(ev['mint'])} – nie mamy tej pozycji w kopii.",
                         w, ev["mint"], core.ds_link(ev["mint"]), ev.get("block_time"))

    def _fill_buy(self, size, mint, t, sol, decimals, new_account):
        c = self.cfg["costs"]
        plan = core.buy_plan(size, sol, c, new_account)
        qt = None
        if sol > 0 and decimals is not None:
            out = self.quote(core.WSOL, mint, int(plan["swap_in_usd"] / sol * 1e9), c["jupiter_slippage_bps"])
            if out: qt = out / 10 ** int(decimals)
        return plan, core.buy_fill(plan, t["price"], qt, t["liq"], c)

    def copy_buy(self, w, st, ev, sig):
        detect = self.clock()
        age = detect - (ev.get("block_time") or detect)
        sym = self.symbol(ev["mint"])
        if age > float(self.cfg["max_buy_signal_age_s"]):
            st["skipped"] += 1
            add_feed(self.book, self.cfg, "skip", f"{w['label']} kupił ${sym} {age/60:.0f} min temu – sygnał za stary, pomijam.", w, ev["mint"], core.ds_link(ev["mint"]))
            return
        sol0 = self.solpx()
        min_usd = float(w.get("min_buy_usd") or 0)
        if min_usd > 0:
            their = core.his_usd(ev, sol0)
            if their < min_usd:
                st["small_skipped"] = int(st.get("small_skipped") or 0) + 1
                st["last_small_ts"] = ev.get("block_time")
                return
            st["big_seen"] = int(st.get("big_seen") or 0) + 1
        size, why = core.size_new(self.book, w["alloc_pct"], ev["mint"], self.cfg)
        if not size:
            st["skipped"] += 1
            add_feed(self.book, self.cfg, "skip", f"{w['label']} kupił ${sym} – kopia pominięta: {why}.", w, ev["mint"], core.ds_link(ev["mint"]))
            return
        t = self.market(ev["mint"])
        sol = self.solpx()
        if not t or t["price"] <= 0 or sol <= 0:
            st["skipped"] += 1
            add_feed(self.book, self.cfg, "skip", f"{w['label']} kupił ${sym} – brak ceny w chwili wykrycia, pomijam.", w, ev["mint"], core.ds_link(ev["mint"]))
            return
        eq0 = self.eq()
        new_acct = not any(p["mint"] == ev["mint"] for p in self.book["positions"])
        plan, fill = self._fill_buy(size, ev["mint"], t, sol, ev.get("decimals"), new_acct)
        fill_ts = self.clock()
        info = {"symbol": t["symbol"], "market_price": t["price"], "liq": t["liq"], "url": core.ds_link(ev["mint"]), "sig": sig,
                "detect_ts": fill_ts, "sol_usd": sol, "their_usd": core.his_usd(ev, sol), "their_price": core.his_price(ev, sol)}
        pos = core.apply_buy(self.book, w, ev, plan, fill, info, fill_ts)
        st["copied_buys"] += 1
        msg = core.note_buy(w, pos, plan, fill, eq0)
        write_note("copy_buy", f"PAPIER kopia {w['label']}: kupno ${pos['symbol']} za {fmt_usd(size)}", msg,
                   {"wallet": w["key"], "mint": ev["mint"], "ticker": pos["symbol"], "alloc_pct": w["alloc_pct"], "pos_id": pos["id"], "size_usd": size, "delay_s": round(pos["delay_s"], 1),
                    "their_sig": sig}, self.cfg)
        add_feed(self.book, self.cfg, "buy", f"Kopia {w['label']}: kupno ${pos['symbol']} za {fmt_usd(size)} ({int(round(w['alloc_pct']*100))}%), "
                 f"opóźnienie {pos['delay_s']:.0f} s, poślizg {fill['impact_pct']*100:+.2f}%.", w, ev["mint"], core.ds_link(ev["mint"]))
        log("COPY BUY", wallet=w["key"], symbol=pos["symbol"], size=size, delay=round(pos["delay_s"], 1), src=fill["source"])

    def copy_addon(self, w, st, pos, ev, sig):
        sol = self.solpx()
        size, why = core.size_addon(self.book, pos, core.his_usd(ev, sol), self.cfg)
        if not size:
            st["skipped"] += 1
            add_feed(self.book, self.cfg, "skip", f"{w['label']} dokupił ${pos['symbol']} – dokupienie pominięte: {why}.", w, pos["mint"], core.ds_link(pos["mint"]))
            return
        t = self.market(pos["mint"])
        if not t or sol <= 0:
            add_feed(self.book, self.cfg, "skip", f"{w['label']} dokupił ${pos['symbol']} – brak ceny, pomijam.", w, pos["mint"], core.ds_link(pos["mint"])); return
        eq0 = self.eq()
        plan, fill = self._fill_buy(size, pos["mint"], t, sol, pos.get("decimals") or ev.get("decimals"), False)
        core.apply_addon(self.book, pos, plan, fill, {"market_price": t["price"]}, self.clock())
        st["copied_buys"] += 1
        msg = core.note_buy(w, pos, plan, fill, eq0, addon=True)
        write_note("copy_buy", f"PAPIER kopia {w['label']}: dokupienie ${pos['symbol']} za {fmt_usd(size)}", msg,
                   {"wallet": w["key"], "mint": pos["mint"], "ticker": pos["symbol"], "alloc_pct": w["alloc_pct"], "pos_id": pos["id"], "size_usd": size, "addon": True, "their_sig": sig}, self.cfg)
        add_feed(self.book, self.cfg, "buy", f"Kopia {w['label']}: dokupienie ${pos['symbol']} za {fmt_usd(size)} (proporcjonalnie).", w, pos["mint"], core.ds_link(pos["mint"]))
        log("COPY ADDON", wallet=w["key"], symbol=pos["symbol"], size=size)

    def _fill_sell(self, pos, qty, t, sol, full):
        c = self.cfg["costs"]
        q_usd = None
        if sol > 0 and pos.get("decimals") is not None:
            out = self.quote(pos["mint"], core.WSOL, int(qty * 10 ** int(pos["decimals"])), c["jupiter_slippage_bps"])
            if out: q_usd = out / 1e9 * sol
        refund = full and not any(p["mint"] == pos["mint"] and p["id"] != pos["id"] for p in self.book["positions"])
        if refund: refund = float(pos.get("rent_usd") or 0) > 0
        return core.sell_fill(qty, t["price"], sol, c, q_usd, t["liq"], refund)

    def copy_sell(self, w, st, pos, ev, sig):
        detect = self.clock()
        frac = float(ev["sell_frac"])
        full = frac >= float(self.cfg["full_exit_fraction"]) - 1e-12
        t = self.market(pos["mint"]) or {"price": pos.get("last_price") or pos["entry_price"], "liq": pos.get("last_liq") or 0, "symbol": pos["symbol"]}
        sol = self.solpx()
        qty = pos["qty"] if full else pos["qty"] * frac
        sf = self._fill_sell(pos, qty, t, sol, full)
        delay = detect - (ev.get("block_time") or detect)
        hhmm = time.strftime("%H:%M:%S", time.localtime(ev.get("block_time") or detect))
        reason = f"{w['label']} {'przelał' if ev.get('transfer_only') else 'sprzedał'} {frac*100:.0f}% o {hhmm}"
        rec = core.apply_sell(self.book, pos, frac, sf, {"market_price": t["price"], "delay_s": delay, "sig": sig, "reason": reason}, self.cfg, self.clock())
        if rec: rec["exit_fill_source"] = sf["source"]
        st["copied_sells"] += 1
        eq1 = self.eq()
        msg = core.note_sell(w, pos, frac, sf, rec, eq1, delay, ev.get("transfer_only"))
        title = (f"PAPIER kopia {w['label']}: sprzedaż ${pos['symbol']} {rec['pnl_pct']*100:+.1f}%" if rec
                 else f"PAPIER kopia {w['label']}: sprzedaż {frac*100:.0f}% ${pos['symbol']}")
        write_note("copy_sell", title, msg, {"wallet": w["key"], "mint": pos["mint"], "ticker": pos["symbol"], "alloc_pct": w["alloc_pct"], "pos_id": pos["id"], "frac": round(frac, 4),
                   "pnl_usd": rec["pnl_usd"] if rec else None, "equity": round(eq1, 4), "delay_s": round(delay, 1), "their_sig": sig}, self.cfg)
        txt = (f"Kopia {w['label']}: sprzedaż całości ${pos['symbol']} – wynik {fmt_usd(rec['pnl_usd'], True)} ({rec['pnl_pct']*100:+.1f}%)." if rec
               else f"Kopia {w['label']}: sprzedaż {frac*100:.0f}% ${pos['symbol']} za {fmt_usd(sf['proceeds'])}.")
        add_feed(self.book, self.cfg, "sell", txt + f" Opóźnienie {delay:.0f} s.", w, pos["mint"], core.ds_link(pos["mint"]))
        log("COPY SELL", wallet=w["key"], symbol=pos["symbol"], frac=round(frac, 3), full=bool(rec), proceeds=round(sf["proceeds"], 4))

    # ---- marking
    def mark(self):
        ps = self.book["positions"]
        if not ps: self.price_ok_ts = self.clock(); return
        try:
            q = self.prices([p["mint"] for p in ps])
            self.price_ok_ts = self.clock(); self.price_err = None
        except Exception as e:
            self.price_err = repr(e)[:150]; log("mark error", err=self.price_err); q = {}
        now = self.clock()
        for p in list(ps):
            t = q.get(p["mint"])
            if t:
                p["last_price"] = t["price"]; p["last_liq"] = t["liq"]; p["last_quote_ts"] = now
                if t.get("symbol") and p["symbol"] == p["mint"][:6]: p["symbol"] = t["symbol"]
            low = (p.get("last_liq") or 0) < float(self.cfg["dead_liq_usd"])
            if low: p.setdefault("low_liq_since", now)
            else: p.pop("low_liq_since", None)
            stale = now - (p.get("last_quote_ts") or now) > float(self.cfg["dead_after_s"])
            if stale or (low and now - p["low_liq_since"] > float(self.cfg["dead_after_s"])):
                rec = core.apply_sell(self.book, p, 1.0, None, {"reason": "rug / brak płynności"}, self.cfg, now, dead=True)
                w = next((x for x in self.wallets if x["key"] == p["wallet"]), {"key": p["wallet"], "label": p["label"]})
                msg = core.note_dead(p, rec, self.eq())
                write_note("copy_sell", f"PAPIER kopia {p['label']}: ${p['symbol']} rug/brak płynności", msg,
                           {"wallet": p["wallet"], "mint": p["mint"], "ticker": p["symbol"], "alloc_pct": w.get("alloc_pct"), "pos_id": p["id"], "pnl_usd": rec["pnl_usd"], "dead": True}, self.cfg)
                add_feed(self.book, self.cfg, "sell", f"${p['symbol']} ({p['label']}) bez płynności – zamknięte jako strata {fmt_usd(rec['pnl_usd'], True)}.", w, p["mint"], core.ds_link(p["mint"]))
        self.dirty = True

    def snapshot(self):
        self.book["equity_history"].append([int(self.clock()), round(self.eq(), 4)])
        self.book["equity_history"] = core.thin_history(self.book["equity_history"])

    def save(self):
        save(BOOK, self.book)
        if self.real: save(REAL, self.real.st)

    # ---- public data.json (no wallet keys/secrets: only public addresses of the copied wallets)
    def data(self, run):
        b = self.book; now = self.clock(); eq = self.eq(); start = b["start_equity"]
        wallets = []
        for w in self.wallets:
            st = b["wallets"][w["key"]]
            ops = [p for p in b["positions"] if p["wallet"] == w["key"]]
            cl = [c for c in b["closed"] if c["wallet"] == w["key"]]
            unreal = sum(core.mark_value(p) - p["cost_left"] for p in ops)
            real = sum(c["pnl_usd"] for c in cl) + sum(p["realized"] for p in ops)
            wallets.append({"key": w["key"], "label": w["label"], "address": w["address"], "alloc_pct": w["alloc_pct"],
                            "solscan": f"https://solscan.io/account/{w['address']}", "open": len(ops),
                            "positions": [p["id"] for p in ops], "pnl_usd": round(real + unreal, 4), "realized": round(real, 4),
                            "unrealized": round(unreal, 4), "trades": len(cl), "wins": sum(1 for c in cl if c["pnl_usd"] > 0),
                            "last_activity_ts": st.get("last_activity_ts"), "last_trade_ts": st.get("last_trade_ts"),
                            "last_poll_ts": st.get("last_poll_ts"), "errors": st.get("errors"), "last_error": st.get("last_error"),
                            "queue": len(st.get("queue") or []), "copied_buys": st.get("copied_buys"), "copied_sells": st.get("copied_sells"),
                            "skipped": st.get("skipped"), "airdrops_skipped": st.get("airdrops_skipped"),
                            "min_buy_usd": w.get("min_buy_usd"), "filter_label": w.get("filter_label") if w.get("min_buy_usd") else None,
                            "small_skipped": st.get("small_skipped") or 0, "big_seen": st.get("big_seen") or 0, "start_ts": st.get("start_ts")})
        pos = []
        for p in b["positions"]:
            v = core.mark_value(p)
            pos.append({"id": p["id"], "wallet": p["wallet"], "label": p["label"], "symbol": p["symbol"], "mint": p["mint"],
                        "url": core.ds_link(p["mint"]), "entry_ts": p["entry_ts"], "entry_price": p["entry_price"], "last_price": p.get("last_price"),
                        "cost": round(p["cost_left"], 4), "cost_total": round(p["cost_usd"], 4), "value": round(v, 4),
                        "pnl_usd": round(v - p["cost_left"] + p["realized"], 4),
                        "pnl_pct": (v - p["cost_left"] + p["realized"]) / p["cost_usd"] if p["cost_usd"] else 0,
                        "delay_s": round(p.get("delay_s") or 0, 1), "impact_pct": p.get("impact_pct"), "fill_source": p.get("fill_source"),
                        "sold_pct": 1 - p["qty"] / p["qty0"] if p.get("qty0") else 0, "liq": p.get("last_liq"), "adds": p.get("adds", 0),
                        "their_sig": p.get("their_sig")})
        closed = [{"id": c["id"], "wallet": c["wallet"], "label": c["label"], "symbol": c["symbol"], "mint": c["mint"], "url": core.ds_link(c["mint"]),
                   "entry_ts": c["entry_ts"], "exit_ts": c["exit_ts"], "cost": round(c["cost_usd"], 4), "pnl_usd": round(c["pnl_usd"], 4),
                   "pnl_pct": c["pnl_pct"], "hold_min": round(c["hold_min"], 1), "reason": c.get("reason"), "delay_s": round(c.get("delay_s") or 0, 1),
                   "exit_delay_s": round(c.get("exit_delay_s") or 0, 1) if c.get("exit_delay_s") is not None else None}
                  for c in b["closed"][-60:]][::-1]
        polls = [b["wallets"][w["key"]].get("last_poll_ts") or 0 for w in self.wallets]
        lag = now - min(polls) if polls and min(polls) else None
        ok = lag is not None and lag < 120 and (self.price_ok_ts and now - self.price_ok_ts < 180)
        delays = [p.get("delay_s") for p in b["positions"]] + [c.get("delay_s") for c in b["closed"]]
        delays = [d for d in delays if d is not None]
        return {"generated": now_iso(now), "generated_ts": int(now), "mode": "paper", "badge": "PAPIER – bez prawdziwych pieniędzy",
                "start_ts": b["start_ts"], "start_iso": now_iso(b["start_ts"]), "start_equity": start, "equity": round(eq, 4),
                "cash": round(b["cash"], 4), "change_usd": round(eq - start, 4), "change_pct": (eq - start) / start if start else 0,
                "fees_paid_usd": round(b.get("fees_paid_usd") or 0, 4), "invested": round(sum(p["cost_left"] for p in b["positions"]), 4),
                "wallets": wallets, "positions": pos, "closed": closed, "feed": b["feed"][-80:][::-1],
                "equity_history": b["equity_history"][-1500:],
                "stats": {"trades": len(b["closed"]), "wins": sum(1 for c in b["closed"] if c["pnl_usd"] > 0),
                          "realized": round(sum(c["pnl_usd"] for c in b["closed"]), 4),
                          "avg_delay_s": round(sum(delays) / len(delays), 1) if delays else None},
                "real": self.real_view(),
                "costs": {"phantom_fee_pct": self.cfg["costs"]["phantom_fee_pct"], "network_sol": self.cfg["costs"]["base_fee_sol"] + self.cfg["costs"]["priority_fee_sol"],
                          "sol_usd": round(net._sol["px"] or 0, 4), "quotes": "Jupiter (publiczne API) + DexScreener"},
                "health": {"status": "ok" if ok else "warn", "run_id": run.get("run_id"), "run_started": run.get("started"),
                           "run_ends": run.get("ends"), "poll_lag_s": round(lag, 1) if lag is not None else None,
                           "price_ok_ts": self.price_ok_ts, "price_err": self.price_err, "rpc_calls": self.rpc.calls, "rpc_errors": self.rpc.errors,
                           "rpc_last_error": self.rpc.last_error, "tx_version": self.rpc.max_version,
                           "where": "GitHub Actions (copy-desk)" if os.environ.get("GITHUB_ACTIONS") else "lokalnie"}}

    def real_view(self):
        """Real wallet (read-only) + which copied wallet each real token came from (same mint bought on paper)."""
        if not self.real: return None
        v = self.real.view(); b = self.book
        src = {}
        for p in list(b["closed"]) + list(b["positions"]):
            src[p["mint"]] = (p["wallet"], p["label"])
        for x in v["positions"] + v["trades"]:
            k = src.get(x["mint"]); x["wallet"], x["label"] = (k if k else (None, None))
        per = {}
        for x in v["positions"]:
            if x["wallet"]:
                d = per.setdefault(x["wallet"], {"open": 0, "pnl_usd": 0.0, "trades": 0})
                d["open"] += 1; d["pnl_usd"] += x["pnl_usd"] or 0
        for x in v["trades"]:
            if x["wallet"] and x["side"] in ("buy", "sell"):
                per.setdefault(x["wallet"], {"open": 0, "pnl_usd": 0.0, "trades": 0})["trades"] += 1
        v["per_wallet"] = per
        return v

    def step(self):
        now = self.clock()
        for w in self.wallets:
            if now >= self.next_poll[w["key"]]:
                self.poll_wallet(w)
                self.next_poll[w["key"]] = self.clock() + float(self.cfg["wallet_poll_s"])
        if now - self.last_px >= float(self.cfg["price_poll_s"]):
            self.last_px = now; self.mark(); self.solpx()
        if self.real and now - self.last_real >= float((self.cfg.get("real_wallet") or {}).get("poll_s", 30)):
            self.last_real = now; self.real.update(); self.dirty = True
        if now - self.last_eq >= float(self.cfg["equity_snapshot_s"]):
            self.last_eq = now; self.snapshot(); self.dirty = True

# ---------------------------------------------------------------- git publish (>= 60 s between commits)
def git(*a, t=90):
    return subprocess.run(["git", "-C", REPO, *a], timeout=t, capture_output=True, text=True)

PATHS = ["data.json", "copydesk/state/book.json", "copydesk/state/real.json", "copydesk/notes.jsonl"]
_last_commit = {"ts": 0.0}

def publish(desk, run, min_gap):
    save(DATA, desk.data(run)); desk.save()
    if NO_GIT: return "no-git"
    if time.time() - _last_commit["ts"] < min_gap: return "wait"
    git("add", "--", *[p for p in PATHS if os.path.exists(os.path.join(REPO, p))])
    if git("diff", "--cached", "--quiet").returncode == 0: return "no diff"
    r = git("commit", "-qm", f"copy desk {time.strftime('%H:%M:%S')} [paper]")
    if r.returncode != 0: return "commit failed: " + r.stderr[-200:]
    _last_commit["ts"] = time.time()
    r = git("push", "-q", "origin", "HEAD:main")
    if r.returncode != 0:
        git("fetch", "-q", "origin", "main")
        m = git("merge", "-X", "ours", "--no-edit", "-m", "sync remote [paper]", "FETCH_HEAD")
        if m.returncode != 0:
            git("merge", "--abort"); return "merge failed: " + (m.stderr or m.stdout)[-200:]
        r = git("push", "-q", "origin", "HEAD:main")
    return "pushed" if r.returncode == 0 else "push failed: " + r.stderr[-200:]

def stop_requested():
    req = load(STOP, {}) or {}
    return str(req.get("run_id")) == str(RUN_ID)

def main():
    cfg = load_cfg()
    desk = Desk(cfg)
    t0 = time.time()
    run = {"run_id": RUN_ID, "started": now_iso(t0), "ends": now_iso(t0 + RUN_S)}
    save(P("state", "run.json"), run)
    log("copy desk start (PAPER)", run_id=RUN_ID, planned_s=RUN_S, no_git=NO_GIT, wallets=[w["label"] for w in cfg["wallets"]])
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    gap = max(60.0, float(cfg["publish_min_interval_s"]))
    last_pub = 0.0
    while not stop["flag"] and time.time() - t0 < RUN_S:
        try:
            desk.step()
        except Exception as e:
            log("step error", err=repr(e)[:300], tb=traceback.format_exc()[-800:])
        if time.time() - last_pub >= gap:
            last_pub = time.time()
            try:
                r = publish(desk, run, gap); log("publish", result=r, equity=round(desk.eq(), 4))
            except Exception as e:
                log("publish error", err=repr(e)[:300])
            if stop_requested():
                log("stop requested via copydesk/control/stop.json"); break
        time.sleep(1.0)
    log("copy desk stopping", ran_s=round(time.time() - t0))
    lock = os.path.join(REPO, ".git", "index.lock")
    if os.path.exists(lock): os.remove(lock)
    wait = gap - (time.time() - _last_commit["ts"])
    if wait > 0 and not NO_GIT: time.sleep(min(wait, gap))
    print("final publish:", publish(desk, run, gap))

if __name__ == "__main__":
    main()
