import os, sys, json, time, glob, unittest, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.strategy import entry_mode, pullback_params, pullback_new, pullback_step, pullback_entry_reasons, filter_reasons, PULLBACK_DEFAULTS, entry_note_pl
CFG = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "config_ref_20261120.json")))  # frozen 20.11 06:55 rules (mechanics tests); live config: test_variant_s.py
PB = dict(CFG, entry={"mode": "pullback", "pullback": {"dip_pct": 0.05, "bounce_pct": 0.02, "window_min": 15, "cancel_pct": 0.15, "max_setups": 8}})

def feed(path, cfg=PB, t0=1000.0, dt=3.0):
    st = pullback_new({"mint": "M", "symbol": "X", "pair": "P"}, path[0], t0, cfg)
    for i, px in enumerate(path[1:], 1):
        r = pullback_step(st, px, t0 + i * dt, cfg)
        if r != "wait":
            return st, r, px
    return st, "wait", None

def ft(**kw):
    base = {"mint": "M", "symbol": "X", "price": 1.0, "mcap": 100000, "liq": 50000, "vol_h1": 20000, "age_min": 120,
            "chg_m5": 5, "chg_h1": 40, "chg_h6": 30, "bs_m5": 1.5, "bs_h1": 1.3, "tx_m5": 50, "vol_accel": 1.5}
    base.update(kw); return base

class TMode(unittest.TestCase):
    def test_live_config_is_pullback(self):
        self.assertEqual(entry_mode(CFG), "pullback")
        self.assertEqual(CFG["entry"]["mode"], "pullback")
        p = pullback_params(CFG)
        self.assertAlmostEqual(p["dip_pct"], 0.05)
        self.assertAlmostEqual(p["bounce_pct"], 0.03)
        self.assertAlmostEqual(p["cancel_pct"], 0.15)
        self.assertEqual(p["window_min"], 15)
        self.assertEqual(p["max_setups"], 8)
        self.assertEqual(entry_note_pl(CFG),
                         "Wejście po cofnięciu: -5% od szczytu, odbicie +3%, rezygnacja po -15% lub 15 min")
        self.assertEqual(entry_note_pl({"entry": {"mode": "now"}}), "")
    def test_default_and_unknown(self):
        self.assertEqual(entry_mode({}), "now")
        self.assertEqual(entry_mode({"entry": {"mode": "weird"}}), "now")
        self.assertEqual(entry_mode(PB), "pullback")
    def test_config_params_present_and_sane(self):
        p = pullback_params(CFG)
        for k in PULLBACK_DEFAULTS: self.assertIn(k, p)
        self.assertTrue(0 < p["dip_pct"] < p["cancel_pct"] < 1)
        self.assertTrue(0 < p["bounce_pct"] < 1 and p["window_min"] > 0)

class TStep(unittest.TestCase):
    def test_no_buy_without_dip(self):
        st, r, _ = feed([1.0, 1.05, 1.10, 1.12, 1.15])
        self.assertEqual(r, "wait"); self.assertAlmostEqual(st["high"], 1.15)
    def test_dip_then_bounce_buys(self):
        # high 1.10, dip to 1.04 (-5.5%), bounce to 1.0608 (+2% from low)
        st, r, px = feed([1.0, 1.10, 1.06, 1.04, 1.05, 1.061])
        self.assertEqual(r, "buy"); self.assertEqual(px, 1.061)
        self.assertAlmostEqual(st["high"], 1.10); self.assertAlmostEqual(st["low"], 1.04)
    def test_low_keeps_updating(self):
        st, r, px = feed([1.0, 0.95, 0.93, 0.92, 0.935, 0.9385])
        self.assertEqual(r, "buy"); self.assertAlmostEqual(st["low"], 0.92)
    def test_small_bounce_not_enough(self):
        st, r, _ = feed([1.0, 0.94, 0.95, 0.955])
        self.assertEqual(r, "wait"); self.assertEqual(st["state"], "dip")
    def test_cancel_too_deep(self):
        st, r, px = feed([1.0, 1.2, 1.1, 1.02])
        self.assertEqual(r, "cancel:too_deep")   # 1.02 <= 1.2 * 0.85
    def test_high_not_raised_after_dip(self):
        st, r, _ = feed([1.0, 0.94, 1.2])         # bounce on the way up = buy, high stays at 1.0
        self.assertEqual(r, "buy"); self.assertAlmostEqual(st["high"], 1.0)
    def test_timeout(self):
        st = pullback_new({"mint": "M"}, 1.0, 0.0, PB)
        self.assertEqual(pullback_step(st, 1.01, 60.0, PB), "wait")
        self.assertEqual(pullback_step(st, 1.01, 15 * 60 + 1, PB), "cancel:timeout")
    def test_missing_quote_waits_then_times_out(self):
        st = pullback_new({"mint": "M"}, 1.0, 0.0, PB)
        self.assertEqual(pullback_step(st, None, 10.0, PB), "wait")
        self.assertEqual(pullback_step(st, None, 16 * 60, PB), "cancel:timeout")

class TRequote(unittest.TestCase):
    def test_pullback_requote_ignores_short_term_dip(self):
        f = ft(chg_m5=-3.0, bs_m5=0.8, vol_accel=0.5)
        self.assertTrue(filter_reasons(f, CFG["filter"]))           # would fail the buy-now check
        self.assertEqual(pullback_entry_reasons(f, CFG["filter"]), [])
    def test_pullback_requote_keeps_hygiene(self):
        rs = pullback_entry_reasons(ft(liq=10000, chg_h1=-5, mcap=500000), CFG["filter"])
        self.assertTrue(any(r.startswith("liq<") for r in rs))
        self.assertTrue(any(r.startswith("mcap>") for r in rs))
        self.assertTrue(any(r.startswith("downtrend_h1") for r in rs))
        self.assertIn("already_held", pullback_entry_reasons(ft(), CFG["filter"], {"M"}))

class TTrader(unittest.TestCase):
    def setUp(self):
        import agents.trader as tr
        self.tr = tr; self.d = tempfile.TemporaryDirectory(); self.origP = tr.P
        tr.P = lambda *a: os.path.join(self.d.name, *a); tr.SETUPS.clear()
    def tearDown(self):
        self.tr.P = self.origP; self.tr.SETUPS.clear(); self.d.cleanup()
    def test_setup_then_trigger_queues_buy_signal(self):
        tr = self.tr; c = dict(PB, stop_at=None)
        pf = {"positions": [], "closed": [], "cash": 25.0}
        sig = {"ts": time.time(), "cand": {"mint": "M", "symbol": "X", "pair": "P", "price": 1.0}}
        self.assertIsNone(tr._add_setup_locked(pf, c, sig))
        self.assertEqual(tr._add_setup_locked(pf, c, sig), "setup_exists")
        now = time.time()
        for i, px in enumerate([1.1, 1.04, 1.07]):
            tr.step_setups_locked(c, {"P": {"priceUsd": str(px)}}, now + i)
        self.assertEqual(tr.SETUPS, {})
        files = glob.glob(os.path.join(self.d.name, "signals", "approved", "*_pb_M.json"))
        self.assertEqual(len(files), 1)
        s = json.load(open(files[0])); self.assertTrue(s["pullback_buy"]); self.assertAlmostEqual(s["setup"]["low"], 1.04)
        self.assertIn("cofnięciu", tr._pullback_note(s))
    def test_setup_blocked_when_entries_closed_or_held(self):
        tr = self.tr
        pf = {"positions": [{"mint": "Z", "symbol": "x"}], "closed": [], "cash": 25.0}
        sig = {"ts": time.time(), "cand": {"mint": "M", "symbol": "X", "pair": "P", "price": 1.0}}
        self.assertEqual(tr._add_setup_locked(pf, dict(PB, stop_at=None), sig), "already_held")
        pf["positions"] = []
        self.assertEqual(tr._add_setup_locked(pf, dict(PB, stop_at="2000-01-01T00:00:00+01:00"), sig), "entries_closed")

    def test_setup_blocked_after_loss_until_eod(self):
        tr = self.tr
        now = time.time()
        pf = {"positions": [], "cash": 25.0,
              "closed": [{"mint": "M", "symbol": "X", "pnl_usd": -0.5, "pnl_pct": -0.1,
                          "exit_ts": now - 10 * 60, "reason": "stop -8%"}]}
        sig = {"ts": now, "cand": {"mint": "M", "symbol": "X", "pair": "P", "price": 1.0}}
        self.assertEqual(tr._add_setup_locked(pf, dict(PB, stop_at=None), sig), "reentry_ban")
        # same ticker, different mint also blocked
        sig2 = {"ts": now, "cand": {"mint": "OTHER", "symbol": "x", "pair": "P", "price": 1.0}}
        self.assertEqual(tr._add_setup_locked(pf, dict(PB, stop_at=None), sig2), "reentry_ban")
        # profitable exit 10 min ago is only the 60-min cooldown
        pf["closed"][0].update(pnl_usd=0.4, pnl_pct=0.08, reason="trailing stop +12.8%")
        self.assertEqual(tr._add_setup_locked(pf, dict(PB, stop_at=None), sig), "reentry_cooldown")
        # profitable exit 61 min ago is allowed
        pf["closed"][0]["exit_ts"] = now - 61 * 60
        self.assertIsNone(tr._add_setup_locked(pf, dict(PB, stop_at=None), sig))

    def test_open_setup_cancelled_after_stop_at_no_buy(self):
        tr = self.tr
        c_open = dict(PB, stop_at=None)
        c_closed = dict(PB, stop_at="2026-11-20T12:25:00+01:00")
        pf = {"positions": [], "closed": [], "cash": 25.0}
        sig = {"ts": time.time(), "cand": {"mint": "M", "symbol": "X", "pair": "P", "price": 1.0}}
        self.assertIsNone(tr._add_setup_locked(pf, c_open, sig))
        self.assertIn("M", tr.SETUPS)
        after = 1795173900
        tr.step_setups_locked(c_closed, {"P": {"priceUsd": "1.04"}}, after)
        tr.step_setups_locked(c_closed, {"P": {"priceUsd": "1.08"}}, after + 3)
        self.assertEqual(tr.SETUPS, {})
        files = glob.glob(os.path.join(self.d.name, "signals", "approved", "*_pb_M.json"))
        self.assertEqual(files, [])

if __name__ == "__main__":
    unittest.main()
