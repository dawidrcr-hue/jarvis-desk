import os, sys, json, unittest, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run_actions

class TStop(unittest.TestCase):
    def test_stop_only_for_own_run(self):
        orig = run_actions.P
        with tempfile.TemporaryDirectory() as d:
            run_actions.P = lambda *a: os.path.join(d, *a)
            try:
                self.assertFalse(run_actions.stop_requested("42"))
                os.makedirs(os.path.join(d, "control"))
                with open(os.path.join(d, "control", "stop.json"), "w") as f: json.dump({"run_id": 41}, f)
                self.assertFalse(run_actions.stop_requested("42"))
                self.assertTrue(run_actions.stop_requested("41"))
                self.assertFalse(run_actions.stop_requested(None))
            finally:
                run_actions.P = orig

if __name__ == "__main__":
    unittest.main()
