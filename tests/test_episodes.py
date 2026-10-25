import json
import os
import tempfile
import unittest

from browser_atlas import _episodes, _memory
from browser_atlas._atlas import Blocked
from browser_atlas._pointer import NoControl


def fresh_env(with_map=True):
    home = tempfile.mkdtemp()
    os.environ["BROWSER_ATLAS_HOME"] = home
    if with_map:
        m = {"shop": {"label": "Example Shop 검색", "access": "open", "hosts": ["shop.example.com"],
                      "pages": {"search": {"url": "https://shop.example.com/search", "controls": [
                          {"role": "button", "name": "Search", "where": "", "act": "search", "does": ""}]}}}}
        p = os.path.join(home, "map.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(m, f)
        os.environ["BROWSER_ATLAS_MAP"] = p
    return home


class EpisodeTest(unittest.TestCase):
    def setUp(self):
        self.home = fresh_env()

    def tearDown(self):
        os.environ.pop("BROWSER_ATLAS_MAP", None)

    def run_episode(self, steps, goal="find a lamp"):
        rec = _episodes.Recorder(recover=False)
        rec.start("t1", "https://shop.example.com/search?q=lamp", goal)
        for s in steps:
            rec.step("t1", **s)
        return rec, rec.finish("t1")

    def test_success_is_recorded_without_the_agent_asking(self):
        _, ep = self.run_episode([
            {"tool": "browser_type", "ok": True, "control": ["searchbox", "Query"],
             "result": {"search": True, "value": "lamp"}, "url": "https://shop.example.com/search"},
            {"tool": "browser_click", "ok": True, "control": ["button", "Search"], "url": "https://shop.example.com/r"},
            {"tool": "browser_read", "ok": True, "result": {"untrusted_page_content": "3 lamps"}}])
        self.assertEqual(ep["outcome"], "success")
        past = _memory.recall("lamp", site="shop")
        self.assertEqual(past[0]["route"][0], {"url": "https://shop.example.com/search"})   # query dropped
        self.assertIn({"control": ["button", "Search"]}, past[0]["route"])

    def test_captcha_page_is_blocked(self):
        _, ep = self.run_episode([{"tool": "browser_read", "ok": True,
                                   "result": {"untrusted_page_content": "보안 확인을 위해 자동입력 방지 문자를 입력"}}])
        self.assertEqual(ep["outcome"], "blocked")
        self.assertEqual(ep["quality"], 0.0)

    def test_blocked_error_and_no_reads(self):
        _, ep = self.run_episode([{"tool": "browser_click", "ok": False, "error": Blocked("x")}])
        self.assertEqual(ep["outcome"], "blocked")
        _, ep = self.run_episode([{"tool": "browser_click", "ok": True, "control": ["button", "Search"]}])
        self.assertEqual(ep["outcome"], "fail")

    def test_errors_lower_quality(self):
        ok_read = {"tool": "browser_read", "ok": True, "result": {"untrusted_page_content": "x"}}
        _, clean = self.run_episode([ok_read])
        err = {"tool": "browser_click", "ok": False, "error": RuntimeError("covered")}
        _, messy = self.run_episode([err, err, ok_read])
        self.assertGreater(clean["quality"], messy["quality"])

    def test_rename_and_new_control_proposals(self):
        self.run_episode([
            {"tool": "browser_click", "ok": False, "control": ["button", "Search"],
             "error": NoControl("button", "Search", ["Search now", "Help"]), "url": "https://shop.example.com/search"},
            {"tool": "browser_click", "ok": True, "control": ["button", "Search now"], "url": "https://shop.example.com/search"},
            {"tool": "browser_read", "ok": True, "result": {"untrusted_page_content": "x"}}])
        kinds = {(p["kind"], p.get("from"), p.get("to"), p.get("name")) for p in _episodes.proposals()}
        self.assertIn(("rename", "Search", "Search now", None), kinds)
        from browser_atlas import _atlas
        names = [c["name"] for c in _atlas.load()["shop"]["pages"]["search"]["controls"]]
        self.assertEqual(names, ["Search now"])                     # applied at once, no approval step
        base = [c["name"] for c in _atlas.load_base()["shop"]["pages"]["search"]["controls"]]
        self.assertEqual(base, ["Search"])                          # the repository map is untouched

    def test_killed_server_episode_is_recovered(self):
        rec = _episodes.Recorder(recover=False)
        rec.start("t9", "https://shop.example.com/search", "recover me")
        rec.step("t9", "browser_read", True, result={"untrusted_page_content": "x"})
        mine = _episodes._open_path()
        mine.rename(_episodes._open_path(4_000_000))    # as if written by a process that no longer exists
        del rec
        _episodes.Recorder()                            # next server start
        self.assertEqual(_memory.stats()["attempts"], 1)
        self.assertFalse(_episodes._open_path(4_000_000).exists())

    def test_live_neighbour_server_is_left_alone(self):
        rec = _episodes.Recorder(recover=False)
        rec.start("t8", "https://shop.example.com/search", "still running elsewhere")
        rec.step("t8", "browser_read", True, result={"untrusted_page_content": "x"})
        _episodes._open_path().rename(_episodes._open_path(os.getppid()))   # a running process
        _episodes.Recorder()
        self.assertEqual(_memory.stats()["attempts"], 0)
        self.assertTrue(_episodes._open_path(os.getppid()).exists())

    def test_only_search_input_is_kept(self):
        _, ep = self.run_episode([
            {"tool": "browser_type", "ok": True, "control": ["textbox", "Password"],
             "result": {"search": False, "value": "hunter2"}},
            {"tool": "browser_read", "ok": True, "result": {"untrusted_page_content": "x"}}])
        row = _memory.connect().execute("select goal, notes from experience").fetchone()
        self.assertNotIn("hunter2", row["goal"] + row["notes"])


if __name__ == "__main__":
    unittest.main()
