import os, sys, time, socket, threading, unittest, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import http_json, HTTP_TIMEOUT
from lib.market import (select_mints_to_price, annotate_and_trim, _add_stat,
                       miss_backoff_s, _take_rotated, reset_discovery_state,
                       remember_unpriceable, hydrate_watch_entry, pairs_for_mints,
                       _seed_pair, _still_in_backoff)

CFG = {
    "filter": {"min_mcap_usd": 40000, "max_mcap_usd": 200000},
    "dead_liq_usd": 1000,
}

class TTimeout(unittest.TestCase):
    def test_default_timeout_is_8(self):
        self.assertEqual(HTTP_TIMEOUT, 8)

    def test_http_json_times_out(self):
        # Listen but never accept, so connect hangs until the socket timeout.
        srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0)); port = srv.getsockname()[1]; srv.listen(1)
        t0 = time.time()
        with self.assertRaises((TimeoutError, urllib.error.URLError, OSError)):
            http_json(f"http://127.0.0.1:{port}/x", timeout=1)
        elapsed = time.time() - t0
        srv.close()
        self.assertLess(elapsed, 3.0)
        self.assertGreaterEqual(elapsed, 0.8)

    def test_deadline_skips_immediately(self):
        t0 = time.time()
        with self.assertRaises(TimeoutError):
            http_json("http://127.0.0.1:9/x", deadline=time.time() - 1)
        self.assertLess(time.time() - t0, 0.5)

class TWatchlist(unittest.TestCase):
    def setUp(self):
        reset_discovery_state()

    def test_select_prices_discovered_and_in_range(self):
        now = 1_000_000
        wl = {
            "NEW": {"last_seen": now, "sources": ["ds"]},
            "IN": {"last_seen": now - 60, "last_mcap": 80000, "last_liq": 20000, "miss_prices": 0},
            "HIGH": {"last_seen": now - 60, "last_mcap": 400000, "last_liq": 50000, "miss_prices": 0},
            "LOW": {"last_seen": now - 60, "last_mcap": 10000, "last_liq": 20000, "miss_prices": 0},
            "DEAD": {"last_seen": now - 60, "last_mcap": 80000, "last_liq": 40, "miss_prices": 0},
            "MISS": {"last_seen": now - 60, "miss_prices": 2},
        }
        got = set(select_mints_to_price(wl, {"NEW", "HIGH"}, now, CFG))
        # HIGH is rediscovered so it is priced again; LOW/DEAD/MISS are skipped
        self.assertEqual(got, {"NEW", "IN", "HIGH"})

    def test_trim_drops_out_of_range_dead_and_stale(self):
        now = 1_000_000
        wl = {
            "IN": {"last_seen": now, "sources": []},
            "HIGH": {"last_seen": now, "sources": []},
            "DEAD": {"last_seen": now, "sources": []},
            "GHOST": {"last_seen": now - 100, "sources": [], "miss_prices": 1},
            "STALE": {"last_seen": now - 7 * 3600, "sources": [], "last_mcap": 80000, "last_liq": 20000},
        }
        meta = {
            "IN": {"mcap": 90000, "liq": 20000},
            "HIGH": {"mcap": 500000, "liq": 80000},
            "DEAD": {"mcap": 90000, "liq": 12},
        }
        attempted = set(wl)
        keep = annotate_and_trim(wl, meta, {"IN", "HIGH", "DEAD"}, attempted, now, CFG, watch_max=400)
        self.assertIn("IN", keep)
        self.assertEqual(keep["IN"]["last_mcap"], 90000)
        self.assertNotIn("HIGH", keep)
        self.assertNotIn("DEAD", keep)
        self.assertNotIn("GHOST", keep)  # second miss, not discovered this round
        self.assertNotIn("STALE", keep)

    def test_rediscovered_unpriced_is_kept(self):
        now = 1_000_000
        wl = {"X": {"last_seen": now, "sources": ["ds"], "miss_prices": 1}}
        keep = annotate_and_trim(wl, {}, {"X"}, {"X"}, now, CFG)
        self.assertIn("X", keep)
        self.assertEqual(keep["X"]["miss_prices"], 2)

    def test_watch_max_caps(self):
        now = 1_000_000
        wl = {f"M{i}": {"last_seen": now - i, "last_mcap": 80000, "last_liq": 20000} for i in range(10)}
        keep = annotate_and_trim(wl, {}, set(), set(), now, CFG, watch_max=4)
        self.assertEqual(len(keep), 4)
        # most recently seen kept
        self.assertIn("M0", keep)

class TBackoff(unittest.TestCase):
    def setUp(self):
        reset_discovery_state()

    def test_backoff_curve(self):
        self.assertEqual(miss_backoff_s(0), 0)
        self.assertEqual(miss_backoff_s(1), 0)
        self.assertEqual(miss_backoff_s(2), 180)
        self.assertEqual(miss_backoff_s(3), 360)
        self.assertEqual(miss_backoff_s(5), 1440)
        self.assertEqual(miss_backoff_s(8), 3600)

    def test_skips_rediscovered_unpriced_while_cooling(self):
        now = 1_000_000
        wl = {"JUNK": {"last_seen": now, "miss_prices": 5, "last_attempt": now - 10,
                       "skip_until": now + 1000, "sources": ["launchlab_lasttrade"]}}
        got = select_mints_to_price(wl, {"JUNK"}, now, CFG)
        self.assertEqual(got, [])

    def test_retries_after_backoff_expires(self):
        now = 1_000_000
        wl = {"JUNK": {"last_seen": now, "miss_prices": 5, "last_attempt": now - 4000,
                       "skip_until": now - 10, "sources": ["launchlab_lasttrade"]}}
        got = select_mints_to_price(wl, {"JUNK"}, now, CFG)
        self.assertEqual(got, ["JUNK"])

    def test_in_range_priced_before_fresh_and_retries(self):
        now = 1_000_000
        wl = {
            "RETRY": {"last_seen": now, "miss_prices": 2, "last_attempt": now - 4000, "skip_until": 0},
            "FRESH": {"last_seen": now, "sources": ["ds"]},
            "IN": {"last_seen": now, "last_mcap": 80000, "last_liq": 20000, "miss_prices": 0},
        }
        got = select_mints_to_price(wl, {"RETRY", "FRESH"}, now, CFG)
        self.assertEqual(got, ["IN", "FRESH", "RETRY"])

    def test_annotate_sets_skip_until_and_remember(self):
        now = 1_000_000
        wl = {"X": {"last_seen": now, "sources": ["ds"], "miss_prices": 1}}
        keep = annotate_and_trim(wl, {}, {"X"}, {"X"}, now, CFG)
        self.assertIn("X", keep)
        self.assertEqual(keep["X"]["miss_prices"], 2)
        self.assertEqual(keep["X"]["skip_until"], now + 180)
        self.assertEqual(keep["X"]["last_attempt"], now)

    def test_successful_price_clears_tomb(self):
        now = 1_000_000
        wl = {"X": {"last_seen": now, "sources": ["ds"], "miss_prices": 4, "skip_until": now + 100}}
        remember_unpriceable(wl)
        keep = annotate_and_trim(wl, {"X": {"mcap": 90000, "liq": 20000}}, {"X"}, {"X"}, now, CFG)
        self.assertEqual(keep["X"]["miss_prices"], 0)
        self.assertNotIn("skip_until", keep["X"])
        e = {"last_seen": now, "sources": ["ds"]}
        hydrate_watch_entry("X", e)
        self.assertNotIn("miss_prices", e)  # cache cleared, nothing to hydrate

    def test_hydrate_restores_evicted_tomb(self):
        now = 1_000_000
        remember_unpriceable({"Z": {"last_seen": now, "miss_prices": 6, "skip_until": now + 500, "last_attempt": now}})
        e = {"first_seen": now, "last_seen": now, "sources": ["launchlab_new"]}
        hydrate_watch_entry("Z", e)
        self.assertEqual(e["miss_prices"], 6)
        self.assertEqual(e["skip_until"], now + 500)
        got = select_mints_to_price({"Z": e}, {"Z"}, now, CFG)
        self.assertEqual(got, [])

    def test_tomb_cap_keeps_newest(self):
        now = 1_000_000
        wl = {f"T{i}": {"last_seen": now - i, "miss_prices": 4, "sources": ["x"]} for i in range(8)}
        # rediscovered so they are not dropped as ordinary misses
        discovered = set(wl)
        keep = annotate_and_trim(wl, {}, discovered, set(), now, CFG, watch_max=400, tomb_max=3)
        tombs = [m for m, e in keep.items() if e.get("miss_prices", 0) >= 3]
        self.assertEqual(len(tombs), 3)
        self.assertIn("T0", keep)
        self.assertNotIn("T7", keep)

    def test_old_high_miss_pruned(self):
        now = 1_000_000
        wl = {"OLD": {"last_seen": now - 8000, "miss_prices": 8, "sources": ["x"]}}
        keep = annotate_and_trim(wl, {}, {"OLD"}, set(), now, CFG)
        self.assertNotIn("OLD", keep)

class TRotate(unittest.TestCase):
    def setUp(self):
        reset_discovery_state()

    def test_rotates_and_wraps(self):
        items = ["a", "b", "c", "d"]
        self.assertEqual(_take_rotated("k", items, 2), ["a", "b"])
        self.assertEqual(_take_rotated("k", items, 2), ["c", "d"])
        self.assertEqual(_take_rotated("k", items, 2), ["a", "b"])
        self.assertEqual(_take_rotated("k", items, 3), ["c", "d", "a"])
        self.assertEqual(_take_rotated("empty", [], 4), [])
        self.assertEqual(_take_rotated("k", items, 0), [])

class TSeedPairs(unittest.TestCase):
    def test_seed_keeps_higher_liq_base_only(self):
        seed = {}
        lo = {"baseToken": {"address": "M"}, "quoteToken": {"address": "SOL"}, "liquidity": {"usd": 10}}
        hi = {"baseToken": {"address": "M"}, "quoteToken": {"address": "SOL"}, "liquidity": {"usd": 99}}
        quote = {"baseToken": {"address": "SOL"}, "quoteToken": {"address": "M"}, "liquidity": {"usd": 5000}}
        _seed_pair(seed, "M", lo)
        _seed_pair(seed, "M", hi)
        _seed_pair(seed, "M", quote)
        self.assertEqual(seed["M"]["liquidity"]["usd"], 99)

    def test_pairs_for_mints_uses_seed_and_skips_fetch(self):
        pair = {"baseToken": {"address": "AAA"}, "liquidity": {"usd": 40}}
        got = pairs_for_mints(["AAA", "BBB"], seed={"AAA": pair, "ZZZ": pair}, deadline=0)
        # deadline 0 means no HTTP for the rest; seeded AAA must still be returned
        self.assertEqual(got, {"AAA": pair})

class TStats(unittest.TestCase):
    def test_add_stat_does_not_concat_str_and_int(self):
        stats = {"gt_trending": "ERR HTTP Error 429: Too Many Requests"}
        _add_stat(stats, "gt_trending", 20)
        self.assertEqual(stats["gt_trending"], 20)
        _add_stat(stats, "gt_trending", 15)
        self.assertEqual(stats["gt_trending"], 35)

if __name__ == "__main__":
    unittest.main(verbosity=2)
