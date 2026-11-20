"""Live config = variant S (20.11): strict filter, stop -6%, +10% -> stop +3%, 50% take-profit at +15%, max 3, buy now, no cutoff."""
import os, sys, json, time, unittest, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.strategy import (ladder_update, ladder_note_pl, take_profit, take_profit_due, apply_take_profit, entries_allowed,
                          entry_mode, filter_reasons, sim_buy)
CFG = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))

def ft(**kw):
    base = {"mint": "M", "symbol": "X", "price": 1.0, "mcap": 100000, "liq": 50000, "vol_h1": 20000, "age_min": 600,
            "chg_m5": 8, "chg_h1": 50, "chg_h6": 30, "bs_m5": 2.0, "bs_h1": 1.5, "tx_m5": 50, "vol_accel": 1.5}
    base.update(kw); return base

class TLiveConfig(unittest.TestCase):
    def test_risk_not_loosened(self):
        self.assertGreaterEqual(CFG["initial_stop"], -0.08)      # stop never wider than -8%
        self.assertLessEqual(CFG["position_pct_of_equity"], 0.2)  # size never above 20%
        self.assertLessEqual(CFG["max_open_positions"], 4)
        self.assertEqual(CFG["filter"]["min_liquidity_usd"], 30000)
        self.assertEqual(CFG["filter"]["min_mcap_usd"], 40000); self.assertEqual(CFG["filter"]["max_mcap_usd"], 200000)
        self.assertEqual(CFG["filter"]["min_score"], 69)
        self.assertTrue(CFG["rugcheck"]["enabled"])
        self.assertEqual(CFG["reentry_ban_after_loss"], "end_of_day")

    def test_variant_s_values(self):
        self.assertEqual(CFG["initial_stop"], -0.06)
        self.assertEqual(CFG["ladder"], [[0.10, 0.03]])
        self.assertEqual(CFG["max_open_positions"], 3)
        self.assertEqual(CFG["take_profit"], {"at": 0.15, "fraction": 0.5, "stop_after": 0.03})
        f = CFG["filter"]
        self.assertEqual(f["min_pair_age_min"], 360); self.assertEqual(f["max_h1_change_pct"], 100); self.assertEqual(f["min_h6_change"], 10.0)
        self.assertEqual(entry_mode(CFG), "now")
        self.assertIsNone(CFG.get("stop_at"))
        self.assertTrue(entries_allowed(CFG, now=time.time() + 86400 * 30))

    def test_note(self):
        n = ladder_note_pl(CFG)
        self.assertIn("Stop -6%", n); self.assertIn("po +10% → stop +3%", n)
        self.assertIn("przy +15% sprzedaż 50% pozycji i stop +3%", n); self.assertIn("od +24% trailing 12 pkt", n)

class TFilter(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(filter_reasons(ft(), CFG["filter"]), [])
    def test_young_pair(self):
        self.assertIn("pair_too_new", filter_reasons(ft(age_min=359), CFG["filter"]))
    def test_h1_cap(self):
        self.assertTrue(any(r.startswith("overextended_h1") for r in filter_reasons(ft(chg_h1=101), CFG["filter"])))
        self.assertEqual(filter_reasons(ft(chg_h1=100), CFG["filter"]), [])
    def test_h6_min(self):
        self.assertTrue(any(r.startswith("downtrend_h6") for r in filter_reasons(ft(chg_h6=9.9), CFG["filter"])))

class TLadder(unittest.TestCase):
    def pos(self): return {"entry_price": 1.0, "stop_pct": CFG["initial_stop"], "peak_gain": 0.0, "qty": 10.0, "entry_liq": 50000}
    def test_initial_stop(self):
        p = self.pos(); self.assertEqual(ladder_update(p, 0.95, CFG), (False, None))
        ex, r = ladder_update(p, 0.94, CFG); self.assertTrue(ex); self.assertEqual(r, "stop -6%")
    def test_lock_plus3(self):
        p = self.pos(); ladder_update(p, 1.10, CFG); self.assertAlmostEqual(p["stop_pct"], 0.03)
        ex, r = ladder_update(p, 1.03, CFG); self.assertTrue(ex)
    def test_take_profit_once(self):
        p = self.pos(); ladder_update(p, 1.149, CFG); self.assertFalse(take_profit_due(p, 1.149, CFG))
        ladder_update(p, 1.15, CFG); self.assertTrue(take_profit_due(p, 1.15, CFG))
        s = apply_take_profit(p, 1.15, 50000, CFG)
        self.assertAlmostEqual(p["qty"], 5.0); self.assertTrue(p["tp_done"]); self.assertAlmostEqual(p["stop_pct"], 0.03)
        self.assertAlmostEqual(s["qty_sold"], 5.0); self.assertGreater(p["partial_proceeds"], 5.0)
        self.assertFalse(take_profit_due(p, 1.3, CFG))
    def test_take_profit_never_lowers_stop(self):
        p = self.pos(); p["stop_pct"] = 0.10; apply_take_profit(p, 1.2, 50000, CFG); self.assertAlmostEqual(p["stop_pct"], 0.10)
    def test_off_when_missing(self):
        self.assertIsNone(take_profit({})); self.assertIsNone(take_profit({"take_profit": {"at": 0.1, "fraction": 1.0}}))

class TTraderPartial(unittest.TestCase):
    def setUp(self):
        import agents.trader as tr
        self.tr = tr; self.d = tempfile.TemporaryDirectory(); self.origP = tr.P
        tr.P = lambda *a: os.path.join(self.d.name, *a)
        os.makedirs(os.path.join(self.d.name, "logs")); os.makedirs(os.path.join(self.d.name, "state"))
        self.notes = []; self.orig_notify = tr.notify; tr.notify = lambda *a, **k: self.notes.append(a)
        self.orig_log = tr.log; tr.log = lambda *a, **k: None
    def tearDown(self):
        self.tr.P = self.origP; self.tr.notify = self.orig_notify; self.tr.log = self.orig_log; self.d.cleanup()
    def test_partial_then_close_pnl_counts_both(self):
        tr = self.tr; c = CFG
        b = sim_buy(5.0, 1.0, 50000, c["costs"])
        pos = {"id": "p", "mint": "M", "symbol": "X", "pair": "P", "entry_ts": time.time(), "entry_price": 1.0, "qty": b["qty"],
               "cost_usd": 5.0, "entry_liq": 50000, "stop_pct": c["initial_stop"], "peak_gain": 0.0}
        pf = {"cash": 20.0, "positions": [pos], "closed": []}
        ladder_update(pos, 1.15, c); tr.take_partial(pf, c, pos, 1.15, 50000)
        self.assertGreater(pf["cash"], 22.7)
        ex, r = ladder_update(pos, 1.03, c); self.assertTrue(ex)
        tr.close(pf, c, pos, 1.03, 50000, r)
        t = pf["closed"][0]
        self.assertGreater(t["pnl_usd"], 0)              # +15% on half and +3% on the rest beat ~3 pp costs
        self.assertAlmostEqual(t["gross_pct"], (0.15 + 0.03) / 2, places=3)
        self.assertAlmostEqual(pf["cash"], 20.0 + 5.0 + t["pnl_usd"], places=6)
        self.assertTrue(all(n[2].startswith("[PAPIER – bez prawdziwych pieniędzy]") for n in self.notes))
        self.assertIn("Częściowa sprzedaż", self.notes[0][2])

if __name__ == "__main__":
    unittest.main()
