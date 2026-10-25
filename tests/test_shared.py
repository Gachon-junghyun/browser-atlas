import json
import os
import tempfile
import unittest
from unittest.mock import patch

from browser_atlas import _atlas, _episodes, _memory, _shared


class SharedTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["BROWSER_ATLAS_HOME"] = self.home
        base = {"shop": {"label": "Example Shop 검색", "access": "open", "hosts": ["shop.example.com"],
                         "pages": {"search": {"url": "https://shop.example.com/search", "controls": [
                             {"role": "button", "name": "Search", "where": "", "act": "search", "does": ""}]}}},
                "bank": {"label": "My Bank", "access": "session", "hosts": ["bank.example.com"]}}
        p = os.path.join(self.home, "map.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(base, f)
        os.environ["BROWSER_ATLAS_MAP"] = p
        with open(os.path.join(self.home, "sites.local.json"), "w", encoding="utf-8") as f:
            json.dump({"myschool": {"label": "학교 포털", "access": "open", "hosts": ["portal.school.example"]}}, f)
        with open(os.path.join(self.home, "shared.json"), "w", encoding="utf-8") as f:
            json.dump({"url": "https://x.supabase.co", "anon_key": "anon"}, f)

    def tearDown(self):
        os.environ.pop("BROWSER_ATLAS_MAP", None)

    def seed(self):
        _memory.record("shop 검색 lamp", "shop", [{"url": "https://shop.example.com/search"},
                                                 {"control": ["button", "Search"]}], "success", 0.9, notes="mine")
        _memory.record("학교 포털 성적 확인", "myschool", [{"url": "https://portal.school.example/"}], "success", 0.9)
        _memory.record("bank balance", "bank", [{"url": "https://bank.example.com/"}], "success", 0.9)
        for _ in range(2):                      # a new control is shared only after two sightings
            _episodes.propose({"kind": "new", "site": "shop", "role": "link", "name": "Next page",
                               "url": "https://shop.example.com/search?page=2"})
        _episodes.propose({"kind": "new", "site": "shop", "role": "link", "name": "Once",
                           "url": "https://shop.example.com/search"})
        _episodes.propose({"kind": "new", "site": "myschool", "role": "link", "name": "성적",
                           "url": "https://portal.school.example/grades"})

    def test_preview_only_public_and_sanitized(self):
        self.seed()
        p = _shared.preview()
        self.assertEqual([r["site"] for r in p["experience"]], ["shop"])
        self.assertEqual([o["site"] for o in p["map_fixes"]], ["shop"])
        blob = json.dumps(p, ensure_ascii=False)
        for private in ("학교", "portal", "bank", "lamp", "mine", "page=2"):
            self.assertNotIn(private, blob)

    def test_upload_marks_shared_once(self):
        self.seed()
        sent = []
        with patch.object(_shared, "_token", return_value="jwt"), \
                patch.object(_shared, "_req", side_effect=lambda m, path, body=None, *a, **k: sent.append((m, path, body))):
            r = _shared.upload()
        self.assertEqual(r["experience"], 1)
        self.assertEqual({path for _, path, _ in sent}, {"/rest/v1/experience", "/rest/v1/map_fixes"})
        self.assertEqual(_shared.preview()["experience"], [])
        self.assertEqual(_shared.preview()["map_fixes"], [])

    def test_sync_mixes_shared_into_recall_and_map(self):
        self.seed()
        community_row = {"id": "a" * 32, "site": "shop", "keywords": ["shop", "검색"],
                         "route": [{"url": "https://shop.example.com/search"}, {"control": ["button", "Search"]}],
                         "outcome": "success", "quality": 0.95, "quality_source": "human", "seconds": 4, "day": "2026-10-20"}
        fix = {"kind": "rename", "site": "shop", "role": "button", "from": "Search", "to": "Find"}

        def fake(method, path, *a, **k):
            return [community_row] if "experience" in path else [fix]
        with patch.object(_shared, "_req", side_effect=fake):
            self.assertEqual(_shared.sync()["experience_new"], 1)
            self.assertEqual(_shared.sync()["experience_new"], 0)          # idempotent
        mine_only = _memory.recall("shop 검색", site="shop", shared=False, explore=0)[0]
        both = _memory.recall("shop 검색", site="shop", shared=True, explore=0)[0]
        self.assertEqual(both["sources"], ["mine", "shared"])               # the same route, seen by both
        self.assertGreater(both["quality"], mine_only["quality"])           # a human-rated shared row lifts it
        self.assertEqual(both["attempts"], 2)
        names = [c["name"] for c in _atlas.load()["shop"]["pages"]["search"]["controls"]]
        self.assertIn("Find", names)                                       # community rename applied
        self.assertNotIn("Search", names)
        self.assertIn("Next page", names)                                  # and my own learned control too

    def test_not_configured(self):
        os.remove(os.path.join(self.home, "shared.json"))
        with self.assertRaises(_shared.NotConfigured):
            _shared.sync()
        self.assertIn("summary", _shared.preview())                          # preview works offline


if __name__ == "__main__":
    unittest.main()
