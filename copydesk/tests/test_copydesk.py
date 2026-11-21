import os, sys, json, time, tempfile, unittest, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import core, net, desk as D

CFG = json.load(open(os.path.join(ROOT, "config.json")))
FX = lambda n: json.load(open(os.path.join(HERE, "fixtures", n)))
UNI = "2heJbC32Tpfcb3nbUb5ER61K11FGZVfVGtVnDm6LDogF"
SOL589 = "3SW1va94ZdQGyRVW7hUFj9Mt3BX3KvqCPo42zZDSApFC"
NAWY = "nAWy4pMBkTsVhK6JWGvVm5VKsyxns3sFz4iYGi38qNc"
TOK = "TokMint1111111111111111111111111111111111111"

def tb(mint, amt, owner=UNI, dec=6):
    return {"mint": mint, "owner": owner, "uiTokenAmount": {"uiAmount": amt, "decimals": dec, "amount": str(int(amt * 10 ** dec)), "uiAmountString": str(amt)}}

def mktx(pre_tok, post_tok, sol_pre=10.0, sol_post=10.0, bt=None, owner=UNI, version=0, err=None):
    return {"blockTime": bt, "version": version,
            "meta": {"err": err, "preBalances": [5_000_000, int(sol_pre * 1e9)], "postBalances": [4_990_000, int(sol_post * 1e9)],
                     "preTokenBalances": pre_tok, "postTokenBalances": post_tok, "loadedAddresses": {"writable": [], "readonly": []}},
            "transaction": {"message": {"accountKeys": [{"pubkey": "Relayer111", "signer": True, "source": "transaction"},
                                                        {"pubkey": owner, "signer": True, "source": "transaction"}]}}}

class TParse(unittest.TestCase):
    def test_v1_sell_fixture_parses(self):
        tx = FX("v1_sell_usdc.json"); self.assertEqual(tx["version"], 1)
        ev = core.classify(tx, UNI, CFG)
        self.assertEqual(len(ev), 1); e = ev[0]
        self.assertEqual(e["kind"], "sell"); self.assertEqual(e["sell_frac"], 1.0)
        self.assertLess(e["stable_spent"], -100)          # received ~160.88 USDC
        self.assertFalse(e["transfer_only"])
        self.assertAlmostEqual(core.his_usd(e, 70), 160.878735, places=3)

    def test_legacy_airdrop_and_unpaid_addon_skipped(self):
        self.assertEqual([e["kind"] for e in core.classify(FX("legacy_airdrop.json"), UNI, CFG)], ["airdrop"])
        self.assertEqual([e["kind"] for e in core.classify(FX("unpaid_addon.json"), UNI, CFG)], ["airdrop"])

    def test_quote_only_txs_ignored(self):
        self.assertEqual(core.classify(FX("v0_usdc_deposit.json"), UNI, CFG), [])
        self.assertEqual(core.classify(FX("v1_usdc_to_sol.json"), SOL589, CFG), [])

    def test_synthetic_kinds(self):
        self.assertEqual(core.classify(mktx([], [tb(TOK, 1000)], 10, 9.9), UNI, CFG)[0]["kind"], "new")
        self.assertEqual(core.classify(mktx([tb(core.USDC, 50)], [tb(core.USDC, 40), tb(TOK, 1000)]), UNI, CFG)[0]["kind"], "new")
        self.assertEqual(core.classify(mktx([], [tb(TOK, 1000)], 10, 9.999), UNI, CFG)[0]["kind"], "airdrop")
        self.assertEqual(core.classify(mktx([tb(TOK, 10)], [tb(TOK, 20)], 10, 9.5), UNI, CFG)[0]["kind"], "addon")
        e = core.classify(mktx([tb(TOK, 1000)], [tb(TOK, 600)], 10, 10.3), UNI, CFG)[0]
        self.assertEqual(e["kind"], "sell"); self.assertAlmostEqual(e["sell_frac"], 0.4)
        self.assertEqual(core.classify(mktx([tb(TOK, 1000)], []), UNI, CFG)[0]["sell_frac"], 1.0)
        self.assertTrue(core.classify(mktx([tb(TOK, 1000)], [tb(TOK, 500)]), UNI, CFG)[0]["transfer_only"])
        self.assertEqual(core.classify(mktx([], [tb(TOK, 1)], 10, 9, err={"x": 1}), UNI, CFG), [])

    def test_string_keys_and_loaded_addresses(self):
        tx = mktx([], [tb(TOK, 1000)], 10, 9.9)
        tx["transaction"]["message"]["accountKeys"] = ["Relayer111"]
        tx["meta"]["loadedAddresses"] = {"writable": [UNI], "readonly": []}
        self.assertEqual(core.classify(tx, UNI, CFG)[0]["kind"], "new")

    def test_ui_amount_null(self):
        b = {"mint": TOK, "owner": UNI, "uiTokenAmount": {"uiAmount": None, "decimals": 6, "amount": "0", "uiAmountString": "0"}}
        self.assertEqual(core.ui_amount(b), 0.0)

class TSizing(unittest.TestCase):
    def setUp(self): self.b = core.new_book(CFG, 1000)
    def test_alloc(self):
        self.assertEqual(core.size_new(self.b, 0.25, TOK, CFG), (6.25, None))
        self.assertEqual(core.size_new(self.b, 0.10, TOK, CFG), (2.5, None))
    def test_size_down_and_skip(self):
        self.b["cash"] = 1.2; self.b["positions"] = [{"mint": "X", "qty": 1, "last_price": 23.8, "cost_left": 23.8, "wallet": "a"}]
        self.assertEqual(core.size_new(self.b, 0.25, TOK, CFG)[0], 1.2)
        self.b["cash"] = 0.4
        s, why = core.size_new(self.b, 0.25, TOK, CFG); self.assertEqual(s, 0.0); self.assertIn("gotówki", why)
    def test_token_cap(self):
        self.b["positions"] = [{"mint": TOK, "qty": 1, "last_price": 9.5, "cost_left": 9.5, "wallet": "a"}]; self.b["cash"] = 15.5
        s, _ = core.size_new(self.b, 0.25, TOK, CFG); self.assertAlmostEqual(s, 0.5)
        pos = {"mint": TOK, "their_initial_usd": 100, "initial_cost": 6.25, "cost_left": 9.5}
        s, why = core.size_addon(self.b, pos, 1000, CFG); self.assertAlmostEqual(s, 0.5)

class TCosts(unittest.TestCase):
    def test_buy_plan(self):
        c = CFG["costs"]; p = core.buy_plan(10, 100, c, True)
        self.assertAlmostEqual(p["network_usd"], 0.000055 * 100)
        self.assertAlmostEqual(p["rent_usd"], 0.00203928 * 100)
        gross = 10 - p["network_usd"] - p["rent_usd"]
        self.assertAlmostEqual(p["phantom_usd"], gross * 0.0085)
        f = core.buy_fill(p, 1.0, quote_tokens=p["swap_in_usd"] * 0.99, costs=c)
        self.assertAlmostEqual(f["impact_pct"], 1 / 0.99 - 1, places=6); self.assertEqual(f["source"], "jupiter")
        f2 = core.buy_fill(p, 1.0, None, 1_000_000, c); self.assertEqual(f2["source"], "szacunek"); self.assertGreater(f2["impact_pct"], 0)
    def test_sell(self):
        c = CFG["costs"]; s = core.sell_fill(10, 1.0, 100, c, quote_usd=9.9)
        self.assertAlmostEqual(s["proceeds"], 9.9 - 9.9 * 0.0085 - 0.0055)

class FakeRpc:
    def __init__(self): self.sigs = {}; self.txs = {}; self.calls = 0; self.errors = 0; self.last_error = None; self.max_version = 1; self.none_for = {}
    def signatures(self, addr, until=None, limit=100):
        out = []
        for s in self.sigs.get(addr, []):
            if s["signature"] == until: break
            out.append(s)
        return out
    def transaction(self, sig):
        if self.none_for.get(sig, 0) > 0: self.none_for[sig] -= 1; return None
        return self.txs.get(sig)
    def add(self, addr, sig, tx):
        self.sigs.setdefault(addr, []).insert(0, {"signature": sig, "blockTime": tx["blockTime"], "err": None}); self.txs[sig] = tx

class TDesk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        D.BOOK = os.path.join(self.tmp, "book.json"); D.NOTES = os.path.join(self.tmp, "notes.jsonl"); D.DATA = os.path.join(self.tmp, "data.json")
        self.t = [100000.0]
        self.px = {TOK: {"price": 0.002, "liq": 80000, "symbol": "TEST", "url": "u", "pair": "p", "mcap": 1}}
        self.rpc = FakeRpc()
        self.quotes = []
        def quote(i, o, amt, bps):
            self.quotes.append((i, o, amt))
            if i == core.WSOL: return int(amt / 1e9 * 100 / 0.002 * 0.99 * 1e6)       # tokens (6 dec) at price 0.002, 1% impact
            return int(amt / 1e6 * self.px[TOK]["price"] * 0.99 / 100 * 1e9)          # lamports
        self.d = D.Desk(CFG, rpc=self.rpc, prices=lambda ms: {m: self.px[m] for m in ms if m in self.px}, quote=quote,
                        solpx=lambda: 100.0, clock=lambda: self.t[0])
        self.w = CFG["wallets"][0]
        self.d.poll_wallet(self.w)      # baseline at start
    def tearDown(self): shutil.rmtree(self.tmp)

    def notes(self): return [json.loads(l) for l in open(D.NOTES) if l.strip()]

    def test_buy_partial_full_sell_flow(self):
        self.rpc.add(UNI, "BUY1", mktx([], [tb(TOK, 50000)], 10, 9.0, bt=self.t[0] + 5))   # wallet pays 1 SOL ($100) at 0.002
        self.t[0] += 17; self.px[TOK]["price"] = 0.0021                                      # price moved before we saw it
        self.d.poll_wallet(self.w)
        self.assertEqual(len(self.d.book["positions"]), 1); p = self.d.book["positions"][0]
        self.assertAlmostEqual(p["cost_usd"], 6.25); self.assertAlmostEqual(p["delay_s"], 12)
        self.assertAlmostEqual(p["entry_market_price"], 0.0021)             # detection-time price, not the wallet's 0.002
        self.assertAlmostEqual(p["their_price"], 0.002)
        self.assertAlmostEqual(self.d.book["cash"], 25 - 6.25)
        n = self.notes()[-1]
        self.assertTrue(n["message"].startswith("[PAPIER – bez prawdziwych pieniędzy] Kopia @unipcs: kupno $TEST za $6.25 (25%)"))
        self.assertIn("https://dexscreener.com/solana/" + TOK, n["message"])
        for k in ("id", "ts", "type", "title", "message"): self.assertIn(k, n)
        # wallet sells 40% -> we sell 40%
        self.t[0] += 60
        self.rpc.add(UNI, "SELL1", mktx([tb(TOK, 50000)], [tb(TOK, 30000)], 9.0, 9.5, bt=self.t[0] - 3))
        q0 = p["qty"]; self.d.poll_wallet(self.w)
        self.assertAlmostEqual(self.d.book["positions"][0]["qty"], q0 * 0.6)
        self.assertIn("sprzedaż 40%", self.notes()[-1]["message"])
        # wallet sells the rest -> closed with PnL + equity in the note
        self.t[0] += 60; self.px[TOK]["price"] = 0.003
        self.rpc.add(UNI, "SELL2", mktx([tb(TOK, 30000)], [], 9.5, 10.5, bt=self.t[0] - 2))
        self.d.poll_wallet(self.w)
        self.assertEqual(self.d.book["positions"], []); c = self.d.book["closed"][-1]
        self.assertGreater(c["pnl_usd"], 0)
        msg = self.notes()[-1]["message"]
        self.assertIn("sprzedaż całości $TEST", msg); self.assertIn("Wynik netto +$", msg); self.assertIn("Equity: $", msg)
        self.assertAlmostEqual(core.equity(self.d.book), self.d.book["cash"])
        self.assertGreater(self.d.book["fees_paid_usd"], 0.05)

    def test_prestart_holdings_not_copied(self):
        self.rpc.add(UNI, "OLD", mktx([], [tb(TOK, 5)], 10, 9, bt=self.t[0] - 100))           # before start
        self.rpc.add(UNI, "ADD", mktx([tb(TOK, 5)], [tb(TOK, 10)], 10, 9, bt=self.t[0] + 1))   # add-on to pre-start bag
        self.rpc.add(UNI, "SELLOLD", mktx([tb(TOK, 10)], [], 9, 10, bt=self.t[0] + 2))
        self.t[0] += 5; self.d.poll_wallet(self.w)
        self.assertEqual(self.d.book["positions"], []); self.assertEqual(self.d.book["cash"], 25)
        self.assertFalse(os.path.exists(D.NOTES))

    def test_stale_buy_and_airdrop_skipped(self):
        self.rpc.add(UNI, "AIR", mktx([], [tb(TOK, 5)], 10, 10, bt=self.t[0] + 1))
        self.rpc.add(UNI, "LATE", mktx([], [tb(TOK, 5)], 10, 9, bt=self.t[0] + 2))
        self.t[0] += 2000; self.d.poll_wallet(self.w)
        self.assertEqual(self.d.book["positions"], [])
        st = self.d.book["wallets"]["unipcs"]; self.assertEqual(st["airdrops_skipped"], 1); self.assertEqual(st["skipped"], 1)

    def test_tx_unavailable_never_stalls(self):
        self.rpc.add(UNI, "GHOST", mktx([], [tb(TOK, 5)], 10, 9, bt=self.t[0] + 1)); self.rpc.none_for["GHOST"] = 99
        self.rpc.add(UNI, "BUY2", mktx([], [tb(TOK, 50000)], 10, 9, bt=self.t[0] + 2))
        for _ in range(CFG["tx_retry_max"]):
            self.t[0] += 3; self.d.poll_wallet(self.w)
        self.assertEqual(len(self.d.book["positions"]), 1)          # later buy processed after GHOST was dropped
        self.assertEqual(self.d.book["wallets"]["unipcs"]["queue"], [])
        self.assertIn("GHOST", self.d.book["wallets"]["unipcs"]["handled"])

    def test_transient_none_then_ok(self):
        self.rpc.add(UNI, "B3", mktx([], [tb(TOK, 50000)], 10, 9, bt=self.t[0] + 1)); self.rpc.none_for["B3"] = 2
        for _ in range(3): self.t[0] += 2; self.d.poll_wallet(self.w)
        self.assertEqual(len(self.d.book["positions"]), 1)

    def test_paid_addon_proportional_capped(self):
        self.rpc.add(UNI, "B", mktx([], [tb(TOK, 50000)], 10, 9.0, bt=self.t[0] + 1)); self.t[0] += 3; self.d.poll_wallet(self.w)
        self.rpc.add(UNI, "A", mktx([tb(TOK, 50000)], [tb(TOK, 100000)], 9.0, 8.5, bt=self.t[0] + 1)); self.t[0] += 3; self.d.poll_wallet(self.w)
        p = self.d.book["positions"][0]; self.assertAlmostEqual(p["cost_usd"], 6.25 + 3.12, places=2)
        self.rpc.add(UNI, "A2", mktx([tb(TOK, 100000)], [tb(TOK, 900000)], 8.5, 0.5, bt=self.t[0] + 1)); self.t[0] += 3; self.d.poll_wallet(self.w)
        p = self.d.book["positions"][0]
        self.assertLessEqual(p["cost_left"], 0.40 * core.equity(self.d.book) + 0.01)

    def test_wallet_sizes_and_data(self):
        w2 = CFG["wallets"][2]; self.d.poll_wallet(w2)
        self.rpc.add(SOL589, "S1", mktx([], [tb(TOK, 50000, SOL589)], 10, 9, bt=self.t[0] + 1, owner=SOL589))
        self.t[0] += 4; self.d.poll_wallet(w2)
        self.assertAlmostEqual(self.d.book["positions"][0]["cost_usd"], 2.5)
        self.d.mark(); self.d.snapshot()
        data = self.d.data({"run_id": "x"})
        s = json.dumps(data)
        for bad in ("secret", "private", "token\":", "seed"): self.assertNotIn(bad, s.lower())
        self.assertEqual(data["badge"], "PAPIER – bez prawdziwych pieniędzy")
        self.assertEqual(len(data["wallets"]), 3); self.assertEqual(data["positions"][0]["label"], "@SOL589")
        self.assertIn("solscan.io/account/", data["wallets"][0]["solscan"])

class TQueueRpc(unittest.TestCase):
    def test_merge_queue(self):
        sigs = [{"signature": "c", "blockTime": 30}, {"signature": "b", "blockTime": 20, "err": {"x": 1}}, {"signature": "a", "blockTime": 10}, {"signature": "z", "blockTime": 1}]
        q = core.merge_queue([], ["h"], sigs, 5)
        self.assertEqual([x["sig"] for x in q], ["a", "c"])
        self.assertEqual(len(core.merge_queue(q, [], sigs, 5)), 2)

    def test_rpc_adapts_tx_version(self):
        calls = []
        def fake(url, data=None, timeout=10):
            v = data["params"][1]["maxSupportedTransactionVersion"]; calls.append(v)
            if v < 1: return {"error": {"code": -32015, "message": "Transaction version (1) is not supported by the requesting client."}}
            return {"result": {"version": 1, "blockTime": 1}}
        old = net.http_json; net.http_json = fake
        try:
            r = net.Rpc(["http://x"], max_version=0)
            self.assertEqual(r.transaction("sig")["version"], 1); self.assertEqual(r.max_version, 1); self.assertEqual(calls, [0, 1])
        finally: net.http_json = old

class TConfig(unittest.TestCase):
    def test_config(self):
        a = {w["address"]: w["alloc_pct"] for w in CFG["wallets"]}
        self.assertEqual(a, {UNI: 0.25, NAWY: 0.10, SOL589: 0.10})
        self.assertEqual(CFG["start_equity_usd"], 25.0); self.assertEqual(CFG["max_token_pct_of_equity"], 0.40)
        self.assertEqual(CFG["costs"]["phantom_fee_pct"], 0.0085); self.assertEqual(CFG["min_position_usd"], 0.5)
        self.assertGreaterEqual(CFG["publish_min_interval_s"], 60)
        for u in CFG["rpc_urls"]: self.assertNotIn("api-key", u); self.assertNotIn("?", u)
    def test_workflow(self):
        wf = open(os.path.join(os.path.dirname(ROOT), ".github", "workflows", "copy-desk.yml")).read()
        self.assertIn("name: copy-desk", wf); self.assertIn("group: copy-desk", wf); self.assertIn("cancel-in-progress: false", wf)
        self.assertNotIn("run_actions.py", wf); self.assertNotIn("secrets.", wf)

if __name__ == "__main__":
    unittest.main()
