import os, sys, json, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import safety

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
def fx(n):
    with open(os.path.join(FX, f"rc_{n}.json")) as f: return json.load(f)
CFG = {"rugcheck": {"max_top10_pct": 35.0, "max_single_holder_pct": 15.0, "max_creator_pct": 10.0,
                    "min_lp_locked_pct": 80.0, "max_score_normalised": 30}}

def keys(reasons): return {r.split(" ")[0] for r in reasons}

class TRealReports(unittest.TestCase):
    def setUp(self): safety._cache.clear()
    def ev(self, n): return safety.evaluate(fx(n), safety.conf(CFG))

    def test_rugs_rejected(self):
        r, s = self.ev("GTASZN")
        self.assertTrue({"rug_single_holder", "rug_top10", "rug_danger"} <= keys(r))
        r, s = self.ev("INFI")
        self.assertTrue({"rug_creator", "rug_single_holder", "rug_top10", "rug_score", "rug_danger"} <= keys(r))
        self.assertGreater(s["creator_pct"], 80)

    def test_unlocked_manual_pumpswap_pool_rejected(self):
        r, s = self.ev("CATMARIO")
        self.assertIn("rug_lp_unlocked", keys(r))
        self.assertEqual(s["market"], "pump_fun_amm")

    def test_normal_coins_pass(self):
        for n in ("DCTY", "Buncoin", "Huss", "TRK"):
            r, s = self.ev(n)
            self.assertEqual(r, [], n)
            self.assertGreaterEqual(s["lp_locked_pct"], 99, n)   # graduated pump.fun / burned LP

    def test_pool_and_odd_accounts_excluded_from_holders(self):
        # DCTY has a pool-owned account at 105% and the PumpSwap vault at 13% -> neither counts
        top10, top1 = safety.holder_stats(fx("DCTY"))
        self.assertLess(top1, 5); self.assertLess(top10, 30)

class TRules(unittest.TestCase):
    def base(self):
        return {"mint": "M", "token": {"supply": 1000, "mintAuthority": None, "freezeAuthority": None}, "creatorBalance": 0,
                "risks": [], "score_normalised": 1, "rugged": False, "transferFee": {"pct": 0}, "token_extensions": {},
                "markets": [{"pubkey": "POOL", "marketType": "raydium", "lp": {"lpLockedPct": 100, "quoteUSD": 1, "baseUSD": 1}}],
                "topHolders": [{"address": "A%d" % i, "owner": "O%d" % i, "pct": 2} for i in range(12)], "knownAccounts": {}}
    def ev(self, rep, pair=None): return keys(safety.evaluate(rep, safety.conf(CFG), pair)[0])

    def test_clean_passes(self): self.assertEqual(self.ev(self.base()), set())
    def test_authorities(self):
        b = self.base(); b["mintAuthority"] = "X"; self.assertIn("rug_mint_authority", self.ev(b))
        b = self.base(); b["token"]["freezeAuthority"] = "X"; self.assertIn("rug_freeze_authority", self.ev(b))
    def test_rugged_flag_and_score(self):
        b = self.base(); b["rugged"] = True; b["score_normalised"] = 31
        self.assertTrue({"rug_flag", "rug_score"} <= self.ev(b))
    def test_creator(self):
        b = self.base(); b["creatorBalance"] = 150; self.assertIn("rug_creator", self.ev(b))
    def test_holders_and_pool_exclusion(self):
        b = self.base(); b["topHolders"][0]["pct"] = 40
        self.assertTrue({"rug_single_holder", "rug_top10"} <= self.ev(b))
        b["topHolders"][0]["owner"] = "POOL"          # owned by the pool -> ignored
        self.assertEqual(self.ev(b), set())
        b["topHolders"][0]["owner"] = "L"; b["knownAccounts"] = {"L": {"type": "LOCKER"}}
        self.assertEqual(self.ev(b), set())
    def test_lp_uses_traded_pair(self):
        b = self.base()
        b["markets"].append({"pubkey": "DLMM", "marketType": "meteoraDlmm", "lp": {"lpLockedPct": 0, "quoteUSD": 0.1, "baseUSD": 0.1}})
        self.assertEqual(self.ev(b), set())                       # largest market locked
        self.assertIn("rug_lp_unlocked", self.ev(b, pair="DLMM"))  # but the desk trades the unlocked one
        b["markets"] = []; self.assertIn("rug_no_market", self.ev(b))
    def test_danger_risk_and_ignore(self):
        b = self.base(); b["risks"] = [{"name": "Weird", "level": "danger"}, {"name": "Mutable metadata", "level": "warn"}]
        self.assertEqual(self.ev(b), {"rug_danger"})
        rc = safety.conf({"rugcheck": dict(CFG["rugcheck"], ignore_danger_risks=["Weird"])})
        self.assertEqual(safety.evaluate(b, rc)[0], [])
    def test_extensions_and_fee(self):
        b = self.base(); b["token_extensions"] = {"permanentDelegate": {"delegate": "X"}}; b["transferFee"] = {"pct": 5}
        self.assertTrue({"rug_token_ext", "rug_transfer_fee"} <= self.ev(b))

class TCheck(unittest.TestCase):
    def setUp(self): safety._cache.clear()
    def test_fail_closed_and_short_fail_cache(self):
        calls = []
        def boom(m, t): calls.append(1); raise TimeoutError("timed out")
        r = safety.check("M1", None, CFG, fetch=boom, now=1000)
        self.assertFalse(r["ok"]); self.assertTrue(r["reasons"][0].startswith("rugcheck_unavailable"))
        self.assertIn("pominięte", r["note_pl"])
        safety.check("M1", None, CFG, fetch=boom, now=1030); self.assertEqual(len(calls), 1)   # cached 60 s
        safety.check("M1", None, CFG, fetch=boom, now=1070); self.assertEqual(len(calls), 2)
    def test_garbage_response_fails_closed(self):
        r = safety.check("M2", None, CFG, fetch=lambda m, t: {"error": "not found"}, now=1)
        self.assertFalse(r["ok"])
    def test_cache_10_min_and_note(self):
        calls = []
        def ok(m, t): calls.append(t); return fx("Huss")
        r = safety.check("M3", None, CFG, fetch=ok, now=0)
        self.assertTrue(r["ok"]); self.assertEqual(calls, [8])
        self.assertTrue(r["note_pl"].startswith("RugCheck: OK, top10 "))
        self.assertIn("LP zablokowane", r["note_pl"])
        self.assertTrue(safety.check("M3", None, CFG, fetch=ok, now=599)["cached"]); self.assertEqual(len(calls), 1)
        safety.check("M3", None, CFG, fetch=ok, now=601); self.assertEqual(len(calls), 2)
    def test_disabled(self):
        r = safety.check("M4", None, {"rugcheck": {"enabled": False}}, fetch=lambda m, t: 1 / 0)
        self.assertTrue(r["ok"])

if __name__ == "__main__":
    unittest.main()
