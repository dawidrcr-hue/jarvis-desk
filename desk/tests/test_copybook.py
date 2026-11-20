import os, sys, json, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.copylogic import classify, conf, copy_size, book_equity, token_deltas, quote_spent, WSOL, USDC
CFG = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))
W = "2heJbC32Tpfcb3nbUb5ER61K11FGZVfVGtVnDm6LDogF"
CC = conf(CFG)

def tb(mint, amt, owner=W):
    return {"mint": mint, "owner": owner, "uiTokenAmount": {"uiAmount": amt}}

def tx(pre_tok, post_tok, sol_pre=10.0, sol_post=10.0, err=None):
    return {"blockTime": 1000, "meta": {"err": err, "preBalances": [int(sol_pre * 1e9)], "postBalances": [int(sol_post * 1e9)],
                                        "preTokenBalances": pre_tok, "postTokenBalances": post_tok},
            "transaction": {"message": {"accountKeys": [{"pubkey": W}]}}}

class TCopy(unittest.TestCase):
    def test_config_targets_unipcs_paper(self):
        self.assertTrue(CC["enabled"])
        self.assertEqual(CC["target"], W)
        self.assertEqual(CC["label"], "@unipcs")
        self.assertAlmostEqual(CC["start_balance_usd"], 25.0)
        self.assertAlmostEqual(CC["position_pct_of_equity"], 0.2)
        self.assertEqual(CC["max_open_positions"], 4)
        self.assertAlmostEqual(CC["min_spend_sol"], 0.005)
        self.assertAlmostEqual(CC["min_spend_stable"], 1.0)
        self.assertAlmostEqual(CC["full_exit_fraction"], 0.9)
        for k in CFG.get("copybook", {}):
            self.assertNotIn("key", k.lower()); self.assertNotIn("secret", k.lower())

    def test_new_position_paid_in_sol(self):
        e = classify(tx([], [tb("TOK", 1000)], 10.0, 9.9), W, CC)
        self.assertEqual(len(e), 1); self.assertEqual(e[0]["kind"], "new"); self.assertAlmostEqual(e[0]["sol_spent"], 0.1)

    def test_new_position_paid_in_usdc(self):
        e = classify(tx([tb(USDC, 50)], [tb(USDC, 45), tb("TOK", 1000)]), W, CC)
        self.assertEqual([x["kind"] for x in e], ["new"])

    def test_tiny_payment_is_airdrop(self):
        e = classify(tx([], [tb("TOK", 1000)], 10.0, 9.999), W, CC)
        self.assertEqual(e[0]["kind"], "airdrop")

    def test_addon_skipped(self):
        e = classify(tx([tb("TOK", 500)], [tb("TOK", 1000)], 10.0, 9.5), W, CC)
        self.assertEqual(e[0]["kind"], "addon")

    def test_sell_fractions(self):
        e = classify(tx([tb("TOK", 1000)], [tb("TOK", 600)], 10.0, 10.2), W, CC)
        self.assertEqual(e[0]["kind"], "sell"); self.assertAlmostEqual(e[0]["sell_frac"], 0.4)
        e = classify(tx([tb("TOK", 1000)], [tb("TOK", 50)]), W, CC)
        self.assertAlmostEqual(e[0]["sell_frac"], 1.0)   # 95% sold -> full exit
        e = classify(tx([tb("TOK", 1000)], []), W, CC)
        self.assertAlmostEqual(e[0]["sell_frac"], 1.0)

    def test_failed_tx_and_other_owner_ignored(self):
        self.assertEqual(classify(tx([], [tb("TOK", 1000)], 10, 9, err={"x": 1}), W, CC), [])
        self.assertEqual(classify(tx([], [tb("TOK", 1000, owner="other")], 10, 9), W, CC), [])

    def test_wsol_counts_as_sol(self):
        t = tx([tb(WSOL, 1.0)], [tb(WSOL, 0.5), tb("TOK", 10)])
        sol, st = quote_spent(t, W); self.assertAlmostEqual(sol, 0.5)
        self.assertEqual([d["mint"] for d in token_deltas(t, W)], ["TOK"])

    def test_size_and_limits(self):
        self.assertAlmostEqual(copy_size(25.0, 25.0, 0, CC), 5.0)
        self.assertEqual(copy_size(25.0, 25.0, 4, CC), 0.0)
        self.assertAlmostEqual(copy_size(25.0, 3.0, 1, CC), 3.0)
        self.assertEqual(copy_size(25.0, 0.5, 1, CC), 0.0)

    def test_book_equity_marks_dead_at_zero(self):
        b = {"cash": 10.0, "positions": [{"qty": 100, "entry_price": 0.1, "last_price": 0.2}, {"qty": 100, "entry_price": 0.1, "dead": True}]}
        self.assertAlmostEqual(book_equity(b), 30.0)

class TCopyAgent(unittest.TestCase):
    def test_sell_copy_partial_then_full(self):
        import tempfile, importlib
        from agents import copybook as cb
        notes = []
        cb.notify = lambda *a, **k: notes.append(a)
        cb.log = lambda *a, **k: None
        b = {"cash": 20.0, "positions": [], "closed": []}
        pos = {"id": "p1", "mint": "TOK", "symbol": "T", "entry_price": 1.0, "entry_ts": 0, "qty": 4.9, "cost_usd": 5.0, "cost_left": 5.0,
               "realized": 0.0, "entry_liq": 100000, "last_price": 1.0, "last_liq": 100000}
        b["positions"].append(pos)
        cb.sell_copy(b, CC, CFG, pos, 0.5, "test", price=1.2, liq=100000)
        self.assertEqual(len(b["positions"]), 1); self.assertAlmostEqual(pos["qty"], 2.45)
        cb.sell_copy(b, CC, CFG, pos, 1.0, "test", price=1.2, liq=100000)
        self.assertEqual(b["positions"], []); self.assertEqual(len(b["closed"]), 1)
        self.assertTrue(all(n[2].startswith("[PAPIER – kopia @unipcs]") for n in notes))
        self.assertGreater(b["closed"][0]["pnl_usd"], 0)

    def test_dead_exit_is_total_loss(self):
        from agents import copybook as cb
        cb.notify = lambda *a, **k: None; cb.log = lambda *a, **k: None
        pos = {"id": "p2", "mint": "R", "symbol": "R", "entry_price": 1.0, "entry_ts": 0, "qty": 5, "cost_usd": 5.0, "cost_left": 5.0,
               "realized": 0.0, "entry_liq": 50000, "last_price": 0.5, "last_liq": 10}
        b = {"cash": 0.0, "positions": [pos], "closed": []}
        cb.sell_copy(b, CC, CFG, pos, 1.0, "rug/brak płynności", dead=True)
        self.assertAlmostEqual(b["closed"][0]["pnl_pct"], -1.0); self.assertAlmostEqual(b["cash"], 0.0)

if __name__ == "__main__":
    unittest.main()
