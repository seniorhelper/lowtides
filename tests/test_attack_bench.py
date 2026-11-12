"""The attack bench (scripts/attack_bench.py): bad purchases against the real HTTP API must all be
stopped with $0.00 moved, and the legitimate control purchases must all go through."""
import importlib.util
import os
import unittest

import helpers  # noqa: F401  (puts the repo root on sys.path)

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "attack_bench.py")

# A row listed here is a gap the bench found in lowtides/ code that someone else owns. It stays failing in the
# bench output (and the bench exits non-zero) until fixed; this test tolerates it so the rest of the suite stays
# useful. Empty it once the gap is closed.
KNOWN_OPEN = set()


def load():
    spec = importlib.util.spec_from_file_location("attack_bench", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAttackBench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bench = load()
        cls.res = cls.bench.run_bench()

    def test_every_attack_is_stopped_and_every_control_succeeds(self):
        bad = [r for r in self.res["rows"] if r["result"] != "PASS" and r["name"] not in KNOWN_OPEN]
        self.assertEqual(bad, [], "\n".join("%s: %s" % (r["name"], r["actual"]) for r in bad))

    def test_known_open_rows_are_real_rows(self):
        names = {r["name"] for r in self.res["rows"]}
        self.assertTrue(KNOWN_OPEN <= names, "KNOWN_OPEN names a row that no longer exists")

    def test_it_is_not_just_refusing_everything(self):
        ctl = [r for r in self.res["rows"] if r["group"] == "control"]
        self.assertGreaterEqual(len(ctl), 4)
        self.assertTrue(all(r["result"] == "PASS" and r["money_moved"] > 0 for r in ctl))

    def test_attacks_cover_the_required_ground(self):
        names = " | ".join(r["name"] for r in self.res["rows"] if r["group"] == "attack").lower()
        for word in ("lookalike", "per-item", "daily", "never-buy", "did not allow", "no-human", "minimum trust", "closed", "off", "stop all",
                     "injection", "tampered pass", "tampered store policy", "tampered receipt", "replay", "/v1/spend", "ask lowtides", "someone else"):
            self.assertIn(word, names)
        self.assertGreaterEqual(self.res["attacks"], 17)

    def test_summary_is_computed_from_the_rows(self):
        rows, res = self.res["rows"], self.res
        atk = [r for r in rows if r["group"] == "attack"]
        stopped = sum(r["result"] == "PASS" for r in atk)
        self.assertEqual(res["summary"], "%d of %d attacks stopped, $%.2f moved; %d of %d legitimate purchases completed" % (
            stopped, len(atk), sum(r["money_moved"] for r in atk), res["controls_ok"], res["controls"]))
        self.assertEqual(res["ok"], all(r["result"] == "PASS" for r in rows))

    def test_exit_code_follows_the_result(self):
        import tempfile
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            code = self.bench.main(["--quiet", "--json", os.path.join(d, "out.json")])
            self.assertEqual(code, 0 if self.res["ok"] else 1)


if __name__ == "__main__":
    unittest.main()
