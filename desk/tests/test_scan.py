import os, sys, time, socket, threading, unittest, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import http_json, HTTP_TIMEOUT
from lib.market import select_mints_to_price, annotate_and_trim, _add_stat

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

class TStats(unittest.TestCase):
    def test_add_stat_does_not_concat_str_and_int(self):
        stats = {"gt_trending": "ERR HTTP Error 429: Too Many Requests"}
        _add_stat(stats, "gt_trending", 20)
        self.assertEqual(stats["gt_trending"], 20)
        _add_stat(stats, "gt_trending", 15)
        self.assertEqual(stats["gt_trending"], 35)

if __name__ == "__main__":
    unittest.main(verbosity=2)
