import json
import os
import tempfile
import time
import unittest

from browser_atlas import _memory, _rank

MAP = {
    "stats": {"label": "National statistics portal", "access": "open", "hosts": ["stats.example.org"],
              "has": ["inflation tables", "population"],
              "pages": {"search": {"url": "https://stats.example.org/search", "controls": [
                  {"role": "searchbox", "name": "Search tables", "where": "", "act": "search", "does": "finds tables"}]}}},
    "central": {"label": "Central bank data", "access": "api", "hosts": ["bank.example.org"],
                "has": ["inflation", "interest rates"], "prefer": "official REST API"},
    "mirror": {"label": "Inflation tables mirror", "access": "blocked", "hosts": ["mirror.example.org"],
               "why": "terms forbid robots (2026-10-26)"},
    "shop": {"label": "Lamp shop", "access": "open", "hosts": ["www.shop.example.org"], "has": ["lamps"]},
}


class RankTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        os.environ["BROWSER_ATLAS_HOME"] = home
        p = os.path.join(home, "map.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(MAP, f)
        os.environ["BROWSER_ATLAS_MAP"] = p

    def tearDown(self):
        os.environ.pop("BROWSER_ATLAS_MAP", None)

    def names(self, r):
        return [x["site"] or x["host"] for x in r["ranked"]]

    def test_blocked_is_excluded_with_its_reason(self):
        r = _rank.site_rank("inflation tables")
        self.assertNotIn("mirror", self.names(r))
        self.assertEqual(r["excluded"][0]["site"], "mirror")
        self.assertIn("terms", r["excluded"][0]["why"])
        self.assertTrue(all(x["why"] for x in r["ranked"]))                  # every pick explains itself

    def test_experience_lifts_a_site_and_a_recent_block_cuts_it(self):
        for _ in range(3):
            _memory.record("inflation tables", "central", [{"url": "https://bank.example.org/data"}], "success", 0.95)
        r = _rank.site_rank("inflation tables")
        self.assertEqual(self.names(r)[0], "central")
        _memory.record("inflation tables", "central", [{"url": "https://bank.example.org/data"}], "blocked", 0.0)
        r = _rank.site_rank("inflation tables")
        cut = next(x for x in r["ranked"] if x["site"] == "central")
        self.assertTrue(any("blocked within" in w for w in cut["why"]))
        self.assertEqual(self.names(r)[0], "stats")

    def test_unmapped_hosts_from_experience_and_hosts_without_www(self):
        _memory.record("inflation tables", None, [{"url": "https://data.newsite.example.net/t"}], "success", 0.9,
                       notes=json.dumps({"host": "data.newsite.example.net"}))
        _memory.record("lamps to buy", None, [{"url": "https://shop.example.org/lamps"}], "success", 0.9)
        r = _rank.site_rank("inflation tables")
        new = next(x for x in r["ranked"] if x["host"] == "data.newsite.example.net")
        self.assertEqual((new["site"], new["use"]), (None, "unmapped"))
        r = _rank.site_rank("lamps to buy")
        self.assertEqual(r["ranked"][0]["site"], "shop")                    # www.shop... found from shop...

    def test_one_slot_explores_a_site_not_tried(self):
        for _ in range(2):
            _memory.record("inflation tables", "central", [{"url": "https://bank.example.org/data"}], "success", 0.9)
        _memory.record("inflation tables", None, [{"url": "https://other.example.net/"}], "success", 0.9,
                       notes=json.dumps({"host": "other.example.net"}))
        r = _rank.site_rank("inflation tables", k=2)                        # stats matches but was never tried
        self.assertEqual(len(r["ranked"]), 2)
        self.assertIn(("stats", 0), [(x["site"], x["attempts"]) for x in r["ranked"]])
        for _ in range(3):                                                  # now central clearly leads
            _memory.record("inflation tables", "central", [{"url": "https://bank.example.org/data"}], "success", 0.95)
        r = _rank.site_rank("inflation tables", k=2)
        self.assertEqual([(x["pick"], x["site"]) for x in r["ranked"]], [("best", "central"), ("explore", "stats")])
        self.assertEqual([x["pick"] for x in _rank.site_rank("inflation tables", k=1)["ranked"]], ["best"])


if __name__ == "__main__":
    unittest.main()
