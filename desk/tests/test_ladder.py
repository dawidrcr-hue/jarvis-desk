import os, sys, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.strategy import ladder_update, sim_buy, sim_sell, slippage, position_size, filter_reasons, features, dead_liquidity_reason, position_mark, portfolio_equity, dead_exit, DEAD_REASON, missed_quote_limit, norm_symbol, trail_gap_for, ladder_note_pl, entries_allowed, stop_at_ts
import json
CFG = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))

def run(path):
    pos = {"entry_price": 1.0, "stop_pct": CFG["initial_stop"], "peak_gain": 0.0}
    for px in path:
        ex, why = ladder_update(pos, px, CFG)
        if ex: return pos, px, why
    return pos, None, None

class T(unittest.TestCase):
    def test_initial_stop(self):
        pos, px, why = run([1.02, 0.97, 0.93, 0.92])
        self.assertEqual(px, 0.92); self.assertIn("-8", why)
    def test_no_exit_above_stop(self):
        pos, px, _ = run([1.05, 0.95, 1.08]); self.assertIsNone(px); self.assertAlmostEqual(pos["stop_pct"], CFG["initial_stop"])
    def test_breakeven(self):
        pos, px, why = run([1.05, 1.12, 1.06, 1.0]); self.assertEqual(px, 1.0); self.assertEqual(pos["stop_pct"], 0.0)
        self.assertAlmostEqual(px / 1.0 - 1, 0.0)
    def test_24_then_fall_to_12(self):
        pos, px, why = run([1.1, 1.2, 1.24, 1.18, 1.13, 1.12]); self.assertEqual(px, 1.12)
        self.assertAlmostEqual(pos["stop_pct"], 0.12); self.assertIn("+12", why)
    def test_stop_never_moves_down(self):
        pos, px, _ = run([1.3, 1.2]); self.assertIsNone(px); self.assertAlmostEqual(pos["stop_pct"], 0.18)
    def test_gap_jump_straight_to_24(self):
        pos, px, _ = run([1.25]); self.assertAlmostEqual(pos["stop_pct"], 0.13); self.assertIsNone(px)
    def test_trail_peak60_exit_48(self):
        pos, px, why = run([1.1, 1.3, 1.45, 1.6, 1.55, 1.5, 1.48]); self.assertEqual(px, 1.48)
        self.assertAlmostEqual(pos["stop_pct"], 0.48); self.assertIn("+48", why)
    def test_trail_follows_peak_not_fixed_12(self):
        pos, px, _ = run([1.24, 1.40, 1.30]); self.assertIsNone(px); self.assertAlmostEqual(pos["stop_pct"], 0.28)
        pos, px, _ = run([1.24, 1.40, 1.27]); self.assertEqual(px, 1.27)
    def test_trail_stop_monotonic(self):
        pos = {"entry_price": 1.0, "stop_pct": CFG["initial_stop"], "peak_gain": 0.0}; prev = -1
        for px in [1.05, 0.97, 1.13, 1.02, 1.26, 1.20, 1.5, 1.4, 1.45, 1.39, 1.7, 1.62, 1.75, 1.68]:
            ex, _ = ladder_update(pos, px, CFG)
            self.assertGreaterEqual(pos["stop_pct"], prev); prev = pos["stop_pct"]
            if ex: break
        self.assertAlmostEqual(pos["stop_pct"], 0.63); self.assertFalse(ex)
    def test_below_24_no_trail(self):
        pos, px, _ = run([1.23, 1.15]); self.assertIsNone(px); self.assertEqual(pos["stop_pct"], 0.0)
    def test_gap_down_fills_at_observed(self):
        pos, px, _ = run([1.0, 0.85]); self.assertEqual(px, 0.85)
    def test_costs_breakeven_is_negative_net(self):
        c = CFG["costs"]; b = sim_buy(5, 1.0, 50000, c); s = sim_sell(b["qty"], 1.0, 50000, c)
        net = s["proceeds"] / 5 - 1; self.assertLess(net, -0.02); self.assertGreater(net, -0.03)
    def test_24_12_net(self):
        c = CFG["costs"]; b = sim_buy(5, 1.0, 50000, c); s = sim_sell(b["qty"], 1.12, 50000, c)
        net = s["proceeds"] / 5 - 1; self.assertGreater(net, 0.08); self.assertLess(net, 0.12)
    def test_slippage_scales(self):
        c = CFG["costs"]; self.assertEqual(slippage(5, 1e6, c), 0.01); self.assertAlmostEqual(slippage(500, 20000, c), 0.05)
    def test_size_20pct(self):
        self.assertEqual(position_size(20, 20, CFG), 4.0); self.assertEqual(position_size(24, 24, CFG), 4.8)
        self.assertEqual(position_size(20, 3, CFG), 3.0); self.assertEqual(position_size(4, 4, CFG), 0.0)
    def test_filter(self):
        p = {"baseToken": {"address": "M", "symbol": "X"}, "priceUsd": "0.001", "marketCap": 30000, "liquidity": {"usd": 5000},
             "priceChange": {"m5": -1, "h1": 5}, "txns": {"m5": {"buys": 5, "sells": 9}, "h1": {"buys": 50, "sells": 40}},
             "volume": {"m5": 100, "h1": 6000}, "pairCreatedAt": 0}
        rs = " ".join(filter_reasons(features(p), CFG["filter"], held={"M"}))
        for k in ("already_held", "mcap<", "liq<", "downtrend_m5", "sells>buys_m5"): self.assertIn(k, rs)

class TCap(unittest.TestCase):
    def test_max_mcap(self):
        base = {"baseToken": {"address": "M", "symbol": "X"}, "priceUsd": "1", "liquidity": {"usd": 50000},
                "priceChange": {"m5": 5, "h1": 20, "h6": 5}, "txns": {"m5": {"buys": 30, "sells": 10}, "h1": {"buys": 300, "sells": 100}},
                "volume": {"m5": 2000, "h1": 10000}, "pairCreatedAt": 1000}
        for mc, ok in ((39999, False), (40000, True), (200000, True), (200001, False), (300000, False), (300001, False), (2000000, False), (145e6, False)):
            rs = filter_reasons(features(dict(base, marketCap=mc)), CFG["filter"])
            self.assertEqual(not rs, ok, (mc, rs))

    def test_min_liquidity(self):
        base = {"baseToken": {"address": "M", "symbol": "X"}, "priceUsd": "1", "marketCap": 80000,
                "priceChange": {"m5": 5, "h1": 20, "h6": 5}, "txns": {"m5": {"buys": 30, "sells": 10}, "h1": {"buys": 300, "sells": 100}},
                "volume": {"m5": 2000, "h1": 10000}, "pairCreatedAt": 1000}
        want = CFG["filter"]["min_liquidity_usd"]
        self.assertEqual(want, 30000)
        rs_lo = filter_reasons(features(dict(base, liquidity={"usd": want - 1})), CFG["filter"])
        rs_ok = filter_reasons(features(dict(base, liquidity={"usd": want})), CFG["filter"])
        self.assertTrue(any(r.startswith("liq<") for r in rs_lo), rs_lo)
        self.assertFalse(any(r.startswith("liq<") for r in rs_ok), rs_ok)

    def test_min_score(self):
        want = CFG["filter"]["min_score"]
        self.assertEqual(want, 69)
        base = {"baseToken": {"address": "M", "symbol": "X"}, "priceUsd": "1", "marketCap": 80000,
                "liquidity": {"usd": 50000},
                "priceChange": {"m5": 5, "h1": 20, "h6": 5}, "txns": {"m5": {"buys": 30, "sells": 10}, "h1": {"buys": 300, "sells": 100}},
                "volume": {"m5": 2000, "h1": 10000}, "pairCreatedAt": 1000}
        # m5=5 -> score 70.5; m5=4 -> score 68.5
        rs_ok = filter_reasons(features(base), CFG["filter"])
        self.assertFalse(any(r.startswith("score<") for r in rs_ok), rs_ok)
        low = dict(base, priceChange={"m5": 4, "h1": 20, "h6": 5})
        rs_lo = filter_reasons(features(low), CFG["filter"])
        self.assertTrue(any(r.startswith("score<") for r in rs_lo), rs_lo)


def _healthy(**over):
    p = {"baseToken": {"address": "MINTA", "symbol": "KFC"}, "priceUsd": "1", "marketCap": 80000,
         "liquidity": {"usd": 50000},
         "priceChange": {"m5": 5, "h1": 20, "h6": 5},
         "txns": {"m5": {"buys": 30, "sells": 10}, "h1": {"buys": 300, "sells": 100}},
         "volume": {"m5": 2000, "h1": 10000}, "pairCreatedAt": 1000}
    p.update(over)
    return features(p)


class TTrailTiers(unittest.TestCase):
    """Wider trailing gaps at higher peaks; stop never moves down across tiers."""

    def test_config_tiers(self):
        self.assertEqual(CFG["trail_tiers"], [[0.24, 0.12], [1.0, 0.2], [2.0, 0.25]])
        self.assertAlmostEqual(trail_gap_for(0.23, CFG) or -1, -1)
        self.assertAlmostEqual(trail_gap_for(0.24, CFG), 0.12)
        self.assertAlmostEqual(trail_gap_for(0.99, CFG), 0.12)
        self.assertAlmostEqual(trail_gap_for(1.0, CFG), 0.20)
        self.assertAlmostEqual(trail_gap_for(1.99, CFG), 0.20)
        self.assertAlmostEqual(trail_gap_for(2.0, CFG), 0.25)
        note = ladder_note_pl(CFG)
        self.assertIn("Stop -8%", note)
        self.assertIn("od +24% trailing 12 pkt", note)
        self.assertIn("od +100% trailing 20 pkt", note)
        self.assertIn("od +200% trailing 25 pkt", note)
        self.assertEqual(CFG["initial_stop"], -0.08)
        self.assertEqual(CFG["stop_at"], "2026-11-20T12:25:00+01:00")
        self.assertTrue(entries_allowed(CFG, now=1795173900 - 1))
        self.assertFalse(entries_allowed(CFG, now=1795173900))

    def test_peak_150_stop_130(self):
        pos, px, _ = run([2.5])
        self.assertIsNone(px)
        self.assertAlmostEqual(pos["stop_pct"], 1.30)

    def test_peak_250_stop_225(self):
        pos, px, _ = run([3.5])
        self.assertIsNone(px)
        self.assertAlmostEqual(pos["stop_pct"], 2.25)

    def test_tier_switch_keeps_higher_stop(self):
        # earlier stop 96%; peak +110% would trail at 90% (20 pkt), so stop stays 96%
        pos = {"entry_price": 1.0, "stop_pct": 0.96, "peak_gain": 1.08}
        ex, _ = ladder_update(pos, 2.10, CFG)
        self.assertFalse(ex)
        self.assertAlmostEqual(pos["peak_gain"], 1.10)
        self.assertAlmostEqual(pos["stop_pct"], 0.96)  # max(0.96, 1.10-0.20=0.90)

    def test_peak_100_uses_20_gap(self):
        pos, px, _ = run([2.0])
        self.assertIsNone(px)
        self.assertAlmostEqual(pos["stop_pct"], 0.80)

    def test_peak_200_uses_25_gap(self):
        pos, px, _ = run([3.0])
        self.assertIsNone(px)
        self.assertAlmostEqual(pos["stop_pct"], 1.75)

    def test_exit_at_150_tier(self):
        pos, px, why = run([2.5, 2.30])
        self.assertEqual(px, 2.30)
        self.assertAlmostEqual(pos["stop_pct"], 1.30)
        self.assertIn("+130", why)
        self.assertIn("20 pkt", why)


class TStopAt(unittest.TestCase):
    """No new entries at or after stop_at; open-position management is unchanged."""

    def test_missing_keeps_entries_open(self):
        after = 1795200000  # well after 20.11 06:00 Warsaw
        self.assertTrue(entries_allowed({}, now=after))
        self.assertTrue(entries_allowed({"stop_at": ""}, now=after))
        self.assertIsNone(stop_at_ts({}))

    def test_before_at_after_warsaw_6am(self):
        c = {"stop_at": "2026-11-20T06:00:00+01:00"}
        ts = stop_at_ts(c)
        self.assertEqual(ts, 1795150800)  # 2026-11-20 05:00:00 UTC
        self.assertTrue(entries_allowed(c, now=ts - 1))
        self.assertFalse(entries_allowed(c, now=ts))
        self.assertFalse(entries_allowed(c, now=ts + 1))

    def test_naive_iso_is_warsaw(self):
        c = {"stop_at": "2026-11-20T06:00:00"}
        self.assertEqual(stop_at_ts(c), stop_at_ts({"stop_at": "2026-11-20T06:00:00+01:00"}))

    def test_unparsable_fails_closed(self):
        self.assertFalse(entries_allowed({"stop_at": "not-a-time"}, now=1))

    def test_live_config_cutoff_1225(self):
        self.assertEqual(CFG.get("stop_at"), "2026-11-20T12:25:00+01:00")
        self.assertEqual(stop_at_ts(CFG), 1795173900)  # 2026-11-20 11:25:00 UTC
        self.assertTrue(entries_allowed(CFG, now=1795173900 - 1))
        self.assertFalse(entries_allowed(CFG, now=1795173900))
        self.assertFalse(entries_allowed(CFG, now=1795173900 + 1))


class TAlreadyHeld(unittest.TestCase):
    """One open position per mint, and also per ticker (copycat KFC mints)."""

    def test_same_mint_already_held(self):
        ft = _healthy()
        rs = filter_reasons(ft, CFG["filter"], held={ft["mint"]})
        self.assertIn("already_held", rs)
        self.assertFalse(filter_reasons(ft, CFG["filter"], held=set()))

    def test_same_symbol_different_mint_already_held(self):
        a = _healthy(baseToken={"address": "MINTA", "symbol": "KFC"})
        b = _healthy(baseToken={"address": "MINTB", "symbol": "kfc"})
        self.assertNotEqual(a["mint"], b["mint"])
        # mint-only hold of A does not block B
        self.assertFalse(filter_reasons(b, CFG["filter"], held={a["mint"]}))
        # ticker hold catches the copy, even with different casing
        rs = filter_reasons(b, CFG["filter"], held={a["mint"]}, held_symbols={a["symbol"]})
        self.assertIn("already_held", rs)
        # a different ticker is still allowed
        other = _healthy(baseToken={"address": "MINTC", "symbol": "OTHER"})
        self.assertFalse(filter_reasons(other, CFG["filter"], held={a["mint"]}, held_symbols={a["symbol"]}))

    def test_snapshot_rejects_second_same_symbol(self):
        # mimics the filter loop: first PASS occupies the ticker so later copies are rejected
        a = _healthy(baseToken={"address": "5eu27b1ErYhmQ5unp7WEcwuFwtQ9R8JzU7j3eCy49Dw", "symbol": "KFC"})
        b = _healthy(baseToken={"address": "PmUF6Zp8dSkboXYVAi3VvmrcC2PL8qFbzjAjSsRrTn6", "symbol": "KFC"})
        held, held_symbols, passed = set(), set(), []
        for ft in (a, b):
            rs = filter_reasons(ft, CFG["filter"], held, (), held_symbols)
            if not rs:
                passed.append(ft["mint"])
                held.add(ft["mint"])
                held_symbols.add(norm_symbol(ft["symbol"]))
        self.assertEqual(passed, [a["mint"]])
        self.assertEqual(held_symbols, {"KFC"})

    def test_symbol_cooldown(self):
        ft = _healthy(baseToken={"address": "MINTB", "symbol": "KFC"})
        rs = filter_reasons(ft, CFG["filter"], cooldown_symbols={"kfc"})
        self.assertIn("reentry_cooldown", rs)
        self.assertFalse(filter_reasons(ft, CFG["filter"], cooldown_symbols={"OTHER"}))


class TDeadLiq(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        self.base = {"entry_price": 1.0, "qty": 4.0, "cost_usd": 4.0, "last_price": 2.66,
                     "entry_ts": self.now - 60, "last_quote_ts": self.now - 20, "missed_quotes": 0}

    def test_liq_below_1000_is_dead(self):
        pos = {**self.base, "last_liq": 0.04}
        q = {"priceUsd": "2.66", "liquidity": {"usd": 0.04}}
        self.assertEqual(dead_liquidity_reason(pos, quote=q, now=self.now, cfg=CFG), DEAD_REASON)
        self.assertEqual(position_mark(pos, now=self.now, cfg=CFG), 0.0)

    def test_liq_999_is_dead_1000_is_not(self):
        pos = {**self.base, "last_liq": 999, "last_price": 1.1}
        self.assertEqual(dead_liquidity_reason(pos, quote={"priceUsd": "1.1", "liquidity": {"usd": 999}}, now=self.now, cfg=CFG), DEAD_REASON)
        pos2 = {**self.base, "last_liq": 1000, "last_price": 1.1}
        self.assertIsNone(dead_liquidity_reason(pos2, quote={"priceUsd": "1.1", "liquidity": {"usd": 1000}}, now=self.now, cfg=CFG))
        self.assertAlmostEqual(position_mark(pos2, now=self.now, cfg=CFG), 4.4)

    def test_healthy_liq_not_dead(self):
        pos = {**self.base, "last_liq": 50000, "last_price": 1.2}
        self.assertIsNone(dead_liquidity_reason(pos, quote={"priceUsd": "1.2", "liquidity": {"usd": 50000}}, now=self.now, cfg=CFG))
        self.assertAlmostEqual(position_mark(pos, now=self.now, cfg=CFG), 4.8)

    def test_no_quote_under_10min_not_dead(self):
        pos = {**self.base, "last_liq": 50000, "last_price": 2.0, "missed_quotes": 5,
               "last_quote_ts": self.now - 120, "entry_ts": self.now - 180}
        self.assertIsNone(dead_liquidity_reason(pos, quote=None, now=self.now, cfg=CFG))
        self.assertAlmostEqual(position_mark(pos, now=self.now, cfg=CFG), 8.0)

    def test_no_quote_10min_by_missed_quotes(self):
        lim = missed_quote_limit(CFG)
        expected = max(1, int(CFG.get("no_quote_timeout_s", 600) / max(1, int(CFG.get("price_poll_s", 20)))))
        self.assertEqual(lim, expected)
        pos = {**self.base, "last_liq": 50000, "last_price": 2.5, "missed_quotes": lim,
               "last_quote_ts": self.now - 100, "entry_ts": self.now - 200}
        self.assertEqual(dead_liquidity_reason(pos, quote=None, now=self.now, cfg=CFG), DEAD_REASON)
        self.assertEqual(position_mark(pos, now=self.now, cfg=CFG), 0.0)

    def test_no_quote_10min_by_wallclock(self):
        pos = {**self.base, "last_liq": 80000, "last_price": 1.8, "missed_quotes": 2,
               "last_quote_ts": self.now - 600, "entry_ts": self.now - 900}
        self.assertEqual(dead_liquidity_reason(pos, quote=None, now=self.now, cfg=CFG), DEAD_REASON)
        self.assertEqual(position_mark(pos, now=self.now, cfg=CFG), 0.0)

    def test_stale_inflated_price_not_in_equity(self):
        # INFI-like: last print +166% but liquidity $0.04 — equity must not include the fake gain
        pos = {**self.base, "qty": 5.0, "cost_usd": 5.0, "entry_price": 1.0, "last_price": 2.663, "last_liq": 0.04}
        pf = {"cash": 15.0, "positions": [pos]}
        naive = pf["cash"] + pos["qty"] * pos["last_price"]
        self.assertGreater(naive, 28)
        self.assertEqual(portfolio_equity(pf, now=self.now, cfg=CFG), 15.0)

    def test_dead_exit_is_total_loss(self):
        pos = {**self.base, "cost_usd": 4.0, "last_price": 2.66, "last_liq": 0.04}
        d = dead_exit(pos)
        self.assertEqual(d["proceeds"], 0.0)
        self.assertEqual(d["pnl_usd"], -4.0)
        self.assertEqual(d["pnl_pct"], -1.0)
        self.assertEqual(d["gross_pct"], -1.0)
        self.assertEqual(d["reason"], "rug/brak płynności")

    def test_pair_gone_after_timeout_marks_zero_even_if_last_liq_was_high(self):
        pos = {**self.base, "last_liq": 57007.07, "last_price": 0.0001431, "qty": 91952.24,
               "missed_quotes": 30, "last_quote_ts": self.now - 700}
        self.assertEqual(dead_liquidity_reason(pos, quote=None, now=self.now, cfg=CFG), DEAD_REASON)
        self.assertEqual(position_mark(pos, now=self.now, cfg=CFG), 0.0)

if __name__ == "__main__": unittest.main(verbosity=2)
