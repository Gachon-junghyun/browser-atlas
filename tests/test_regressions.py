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


if __name__ == "__main__":
    unittest.main()
