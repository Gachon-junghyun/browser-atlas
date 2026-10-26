"""Bugs found by agents using the MCP server (dogfood wave 1, 2026-10-26). One test per finding."""
import json
import os
import tempfile
import unittest

from browser_atlas import _episodes, _memory, _safety


class Wave1(unittest.TestCase):
    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()

    def test_cart_add_stops_but_remove_does_not(self):
        for name in ("Add to Cart", "장바구니 담기", "장바구니에 추가", "바로구매", "바로 구매"):
            self.assertTrue(_safety.irreversible(name, "button"), name)
        for name in ("Remove Stardew Valley", "Remove", "장바구니", "장바구니 보기"):
            self.assertFalse(_safety.irreversible(name, "button"), name)

    def test_unrelated_goals_no_longer_match_through_particles(self):
        dart = _memory.keywords("DART 에서 SK하이닉스 최근 공시 3건 제목과 접수일")
        steam = _memory.keywords("스팀에서 스타듀 밸리 한국 가격 알아보기")
        self.assertEqual(_memory.similarity(dart, steam), 0)
        self.assertIn("광화문", _memory.keywords("광화문 교보문고 위치"))
        self.assertGreater(_memory.similarity(_memory.keywords("삼성전자의 주가"),
                                              _memory.keywords("삼성전자 현재가")), 0)

    def test_recall_stays_on_the_host(self):
        con = _memory.connect()
        _memory.record("스팀 가격 확인", None, [{"url": "https://store.steampowered.com/app/1"}], "success", 0.9, con=con)
        _memory.record("공시 가격 확인", None, [{"url": "https://dart.fss.or.kr/"}], "success", 0.9, con=con)
        picks = _memory.recall("가격 확인", host="dart.fss.or.kr", con=con)
        self.assertEqual([p["route"][0]["url"] for p in picks], ["https://dart.fss.or.kr/"])

    def test_safety_stops_and_missing_controls_cost_nothing(self):
        ep = {"blocked": False, "reads": 1, "needs_human": True, "steps": [
            {"ok": True}, {"ok": False, "soft": True}, {"ok": True}, {"ok": False, "soft": True}]}
        clean = {"blocked": False, "reads": 1, "needs_human": False, "steps": [{"ok": True}, {"ok": True}]}
        self.assertEqual(_episodes.judge(ep), _episodes.judge(clean))

    def test_agent_verdict_wins_except_block(self):
        ep = {"blocked": False, "reads": 2, "needs_human": False, "steps": [{"ok": True}]}
        self.assertEqual(_episodes.judge(ep, "fail")[0], "fail")
        ep["blocked"] = True
        self.assertEqual(_episodes.judge(ep, "success")[0], "blocked")

    def test_tab_ownership_survives_another_server(self):
        from browser_atlas import mcp_server as m
        m._remember_tab("TAB_A", 9261)
        m._remember_tab("TAB_B", 9262)          # as if written by a second server process
        self.assertEqual(m._opened(), {"TAB_A", "TAB_B"})
        self.assertEqual(m._tab_port("TAB_B"), 9262)
        self.assertEqual(m._port({"tab": "TAB_A"}), 9261)

    def test_read_budget_applies_to_lists(self):
        from browser_atlas import mcp_server as m

        class C:
            def eval(self, js, timeout=30):
                return "u" if js == "location.href" else [["link", "x" * 50]] * 100

        r = m._read(C(), {"mode": "links", "max_chars": 300})
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len(json.dumps(r["untrusted_page_content"])), 400)


class Wave2(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        os.environ["BROWSER_ATLAS_HOME"] = home
        m = {"poly": {"label": "Poly", "access": "open", "hosts": ["poly.example"], "pages": {"search": {
            "url": "https://poly.example/search", "controls": [
                {"role": "link", "name": "<model name>", "dynamic": True, "where": "", "act": "", "does": ""},
                {"role": "button", "name": "search", "where": "", "act": "", "does": ""}]}}}}
        p = os.path.join(home, "m.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(m, f)
        os.environ["BROWSER_ATLAS_MAP"] = p

    def tearDown(self):
        os.environ.pop("BROWSER_ATLAS_MAP", None)

    def run_clicks(self, clicks):
        rec = _episodes.Recorder(recover=False)
        rec.start("t", "https://poly.example/search", "find a tree")
        for role, name, same in clicks:
            rec.step("t", "browser_click", True, control=[role, name], result={"same_name_controls": same},
                     url="https://poly.example/search")
        rec.step("t", "browser_read", True, result={"untrusted_page_content": "x"})
        rec.finish("t")

    def test_result_names_are_not_learned_as_controls(self):
        self.run_clicks([("link", "Tree", 1),                    # role held as a template in the map
                         ("button", "Sort", 20),                 # repeats on the page
                         ("button", "Page 2025", 1)])            # carries a number
        self.assertEqual(_episodes.proposals(), [])
        self.run_clicks([("button", "Filters", 1)])               # a real piece of interface
        self.assertEqual([p["name"] for p in _episodes.proposals()], ["Filters"])

    def test_read_find_merges_overlapping_windows(self):
        from browser_atlas import mcp_server as m

        class C:
            def eval(self, js, timeout=30):
                return "u" if js == "location.href" else ("a" * 400 + "Stardew Valley Stardew Valley" + "b" * 400)

        r = m._read(C(), {"mode": "text", "find": "stardew"})
        self.assertEqual(len(r["untrusted_page_content"]), 1)

    def test_read_find_on_a_word_in_every_row_is_cut_not_dropped(self):
        from browser_atlas import mcp_server as m

        class C:
            def eval(self, js, timeout=30):
                return "u" if js == "location.href" else "10-Q filed 2026-08-26\n" * 300

        r = m._read(C(), {"mode": "text", "find": "10-Q", "max_chars": 1200})
        self.assertTrue(r["truncated"])
        self.assertTrue(r["untrusted_page_content"][0].startswith("10-Q filed 2026-08-26"))

    def test_atlas_site_by_host_and_hint(self):
        from browser_atlas import mcp_server as m
        self.assertEqual(m._site("poly.example")["site"], "poly")
        self.assertIn("did_you_mean", m._site("polly"))


class Wave2a(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        os.environ["BROWSER_ATLAS_HOME"] = home
        m = {"books": {"label": "Books", "access": "open", "hosts": ["books.example"], "pages": {
            "search": {"url": "https://books.example/search?q={query}", "controls": [
                {"role": "searchbox", "name": "통합검색어 입력", "where": "", "act": "search", "does": ""}]}}}}
        p = os.path.join(home, "m.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(m, f)
        os.environ["BROWSER_ATLAS_MAP"] = p

    def tearDown(self):
        os.environ.pop("BROWSER_ATLAS_MAP", None)

    def episode(self, url):
        from browser_atlas._pointer import NoControl
        rec = _episodes.Recorder(recover=False)
        rec.start("t", url, "search a book")
        rec.step("t", "browser_type", False, error=NoControl("searchbox", "통합검색어 입력", ["검색어 입력"]),
                 control=["searchbox", "통합검색어 입력"], url=url)
        rec.step("t", "browser_type", True, control=["searchbox", "검색어 입력"], result={"search": True}, url=url)
        rec.step("t", "browser_read", True, result={"untrusted_page_content": "x"})
        rec.finish("t")

    def test_no_rename_from_another_page(self):
        self.episode("https://books.example/")                   # the home page, not the mapped search page
        self.assertFalse([p for p in _episodes.proposals() if p["kind"] == "rename"])

    def test_rename_on_the_mapped_page_only_touches_that_page(self):
        self.episode("https://books.example/search")
        renames = [p for p in _episodes.proposals() if p["kind"] == "rename"]
        self.assertEqual([(r["page"], r["to"]) for r in renames], [("search", "검색어 입력")])

    def test_generic_close_is_not_learned(self):
        rec = _episodes.Recorder(recover=False)
        rec.start("t", "https://books.example/", "x")
        rec.step("t", "browser_click", True, control=["link", "닫기"], result={}, url="https://books.example/")
        rec.step("t", "browser_read", True, result={"untrusted_page_content": "x"})
        rec.finish("t")
        self.assertEqual(_episodes.proposals(), [])

    def test_page_matches(self):
        from browser_atlas._atlas import page_matches
        self.assertTrue(page_matches("https://www.books.example/item/{id}", "https://books.example/item/42?x=1"))
        self.assertFalse(page_matches("https://books.example/item/{id}", "https://books.example/"))

    def test_recall_follows_the_redirect(self):
        from browser_atlas import mcp_server as m
        _memory.record("삼성전자 현재가", None, [{"url": "https://stock.naver.example/price"}], "success", 0.9)
        got = m._recall_for("삼성전자 현재가", ["https://finance.naver.example/item", "https://stock.naver.example/price"])
        self.assertEqual(len(got), 1)


if __name__ == "__main__":
    unittest.main()
