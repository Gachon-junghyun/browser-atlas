"""The local page graph fills itself from use and plans steps for browser_run."""
import base64
import json
import os
import tempfile
import unittest
from unittest import mock

from browser_atlas import _graph, mcp_server

H = "https://lms.example.org"


class GraphTest(unittest.TestCase):
    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()
        os.environ.pop("BROWSER_ATLAS_MAP", None)
        mcp_server.REC.open.clear()

    def test_identity_query_values_select_and_keep_the_thing(self):
        base = "https://sec.gov/cgi-bin/browse-edgar?CIK={}&action=getcompany&count=40&owner=include&type=10-Q"
        urls = {v: base.format(v) for v in ("NVDA", "MSFT")}
        for url in urls.values():
            _graph.page(url, "EDGAR Search Results", direct=True)
        for target, picks, want in [("NVDA EDGAR filings", None, "NVDA"),
                                     ("EDGAR", None, "MSFT"),
                                     ("EDGAR", {"thing": "NVDA"}, "NVDA"),
                                     ("nvda EDGAR", None, "MSFT")]:
            with self.subTest(target=target, picks=picks):
                plan = _graph.plan(urls["MSFT"], target, picks=picks)
                self.assertTrue(plan["found"], plan)
                self.assertEqual(plan["page"], _graph.path_of(urls[want]))
                self.assertNotIn("choose", plan)
                for step in plan["steps"]:
                    if step[0] == "goto":
                        self.assertEqual(_graph.path_of(step[1]), _graph.path_of(urls[want]))
        plan = _graph.plan("https://sec.gov/", "EDGAR")
        self.assertEqual(plan["choose"]["kind"], "thing")
        self.assertEqual(plan["choose"]["candidates"], ["MSFT", "NVDA"])
        plan = _graph.plan("https://sec.gov/", "EDGAR", picks={"thing": "NVDA"})
        self.assertEqual(plan["page"], _graph.path_of(urls["NVDA"]))
        groups = _graph.kinds("sec.gov")
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0]["things"]), ["MSFT", "NVDA"])
        self.assertIn("CIK={thing}", groups[0]["template"])

    def test_query_rebase_preserves_other_url_values(self):
        start = "/filings?CIK=MSFT&count=40"
        other = "/filings?CIK=NVDA&count=40"
        slot = _graph._slot(start, [other])
        url = "https://sec.gov/filings?count=40&q=&CIK=NVDA&token=example#results"
        rebased = _graph._rebase_url(url, slot)
        self.assertEqual(rebased, "https://sec.gov/filings?count=40&q=&CIK=MSFT&token=example#results")
        self.assertIsNone(_graph._slot(start, ["/filings?CIK=NVDA&count=10"]))
        self.assertIsNone(_graph._slot(_graph.path_of("https://sec.gov/filings?q=MSFT"),
                                      [_graph.path_of("https://sec.gov/filings?q=NVDA")]))

    def test_path_thing_rebase_keeps_queries_and_last_segment_rule(self):
        start = "/stock/MSFT/news?count=10"
        other = "/stock/NVDA/news?count=40"
        slot = _graph._slot(start, [other])
        self.assertEqual(_graph._rebase_path(other, slot), "/stock/MSFT/news?count=40")
        self.assertIsNone(_graph._slot("/stock/MSFT/news", ["/stock/MSFT/price"]))

    def test_a_goto_records_the_page_and_a_redirect_marks_the_asked_page(self):
        _graph.observe("browser_goto", {"url": H + "/calendar/view.php?view=day"},
                       {"url": H + "/calendar/view.php?view=day", "title": "Calendar"}, "")
        _graph.observe("browser_goto", {"url": H + "/grade/report.php?id=3"}, {"url": H + "/login/index.php"}, "")
        m = {p["path"]: p for p in _graph.host_map("lms.example.org")["top"]}
        self.assertEqual(m["/calendar/view.php?view=day"]["direct"], [1, 0])
        self.assertEqual(m["/calendar/view.php?view=day"]["title"], "Calendar")
        self.assertEqual(m["/grade/report.php?id=3"]["direct"], [0, 1])  # asked for, sent to the login page
        self.assertEqual(m["/login/index.php"]["direct"], [0, 0])        # reached, but nobody asked for it

    def test_a_css_click_keeps_the_element_role_and_name_and_the_edge(self):
        new = _graph.observe("browser_click", {"css": "a.notice"},
                             {"url": H + "/mod/board/view.php?id=9", "role": "link", "name": "Class Announcements"},
                             H + "/course/view.php?id=1")
        self.assertEqual(new, {"pages": 1, "controls": 1})
        m = {p["path"]: p for p in _graph.host_map("lms.example.org")["top"]}
        self.assertIn(["link", "Class Announcements", 1, 0, "css"], m["/course/view.php?id=1"]["controls"])

    def test_a_controls_read_keeps_interface_and_skips_content(self):
        data = [["button", "Search"], ["link", "Log out"], ["button", "닫기"], ["link", "2026-10-26 23:59 HW5"],
                ["link", "Row"], ["link", "Row"], ["link", "Row"]]
        _graph.observe("browser_read", {"mode": "controls"}, {"url": H + "/", "untrusted_page_content": data}, H + "/")
        names = {c[1] for p in _graph.host_map("lms.example.org")["top"] for c in p["controls"]}
        self.assertEqual(names, {"Search", "Log out"})

    def test_links_read_adds_unvisited_pages_and_other_hosts_are_ignored(self):
        data = [["Notices", H + "/mod/board/view.php?id=7"], ["Elsewhere", "https://evil.example.com/x"]]
        _graph.observe("browser_read", {"mode": "links"}, {"url": H + "/course/view.php?id=1",
                                                             "untrusted_page_content": data}, "")
        m = {p["path"]: p for p in _graph.host_map("lms.example.org")["top"]}
        self.assertEqual(m["/mod/board/view.php?id=7"]["visits"], 0)
        self.assertEqual(m["/mod/board/view.php?id=7"]["title"], "Notices")
        self.assertEqual(_graph.host_map("evil.example.com")["pages"], 0)

    def test_plan_prefers_a_direct_goto(self):
        _graph.observe("browser_goto", {"url": H + "/calendar/view.php?view=upcoming"},
                       {"url": H + "/calendar/view.php?view=upcoming", "title": "Upcoming events"}, "")
        p = _graph.plan(H + "/", "upcoming")
        self.assertTrue(p["found"])
        self.assertEqual(p["steps"], [["goto", H + "/calendar/view.php?view=upcoming"]])

    def test_plan_walks_clicks_when_a_page_was_only_reached_by_clicking(self):
        _graph.observe("browser_goto", {"url": H + "/course/view.php?id=1"}, {"url": H + "/course/view.php?id=1"}, "")
        _graph.observe("browser_click", {"role": "link", "name": "Grades"},
                       {"url": H + "/grade/user.php?token=abc"}, H + "/course/view.php?id=1")
        p = _graph.plan("https://other.example.net/", "grade user", host="lms.example.org")
        self.assertEqual(p["steps"], [["goto", H + "/course/view.php?id=1"], ["click", "link", "Grades"]])
        here = _graph.plan(H + "/course/view.php?id=1", "/grade/user.php?token=zzz")
        self.assertEqual(here["steps"], [["click", "link", "Grades"]])

    def test_plan_follows_a_link_seen_on_the_page(self):
        _graph.observe("browser_goto", {"url": H + "/course/view.php?id=1"}, {"url": H + "/course/view.php?id=1"}, "")
        _graph.observe("browser_read", {"mode": "links"}, {"url": H + "/course/view.php?id=1",
                       "untrusted_page_content": [["Class Announcements", H + "/mod/board/view.php?id=7"]]}, "")
        p = _graph.plan(H + "/course/view.php?id=1", "class announcements")
        self.assertEqual(p["steps"], [["goto", H + "/mod/board/view.php?id=7"]])

    def test_query_ids_separate_pages_and_searches_and_tokens_do_not(self):
        self.assertEqual(_graph.path_of(H + "/mod/board/view.php?id=7&q=my+search&sesskey=abc"), "/mod/board/view.php?id=7")
        self.assertNotEqual(_graph.path_of(H + "/v.php?id=7"), _graph.path_of(H + "/v.php?id=8"))
        self.assertEqual(_graph.path_of(H + "/x?id=" + "a" * 40), "/x")       # a long value is a token, not an id

    def test_plan_picks_the_board_of_the_course_the_tab_is_on(self):
        for cid, board in (("1", "11"), ("2", "22")):
            _graph.observe("browser_goto", {"url": H + "/course/view.php?id=" + cid}, {"url": H + "/course/view.php?id=" + cid}, "")
            _graph.observe("browser_read", {"mode": "links"}, {"url": H + "/course/view.php?id=" + cid, "untrusted_page_content": [
                ["Announcements", H + "/mod/board/view.php?id=1"], ["Class Announcements", H + "/mod/board/view.php?id=" + board]]}, "")
        p = _graph.plan(H + "/course/view.php?id=2", "Class Announcements")
        self.assertEqual(p["steps"], [["goto", H + "/mod/board/view.php?id=22"]])

    def test_a_click_among_same_named_controls_keeps_its_index(self):
        S = "https://stock.example.com"
        _graph.observe("browser_goto", {"url": S + "/stock/NVDA/price"}, {"url": S + "/stock/NVDA/price"}, "")
        _graph.observe("browser_click", {"role": "link", "name": "News"},           # the first News: the site menu
                       {"url": S + "/news", "same_name_controls": 2}, S + "/stock/NVDA/price")
        _graph.observe("browser_click", {"role": "link", "name": "News", "index": 1},
                       {"url": S + "/stock/NVDA/worldnews", "same_name_controls": 2}, S + "/stock/NVDA/price")
        p = _graph.plan(S + "/stock/NVDA/price", "/stock/NVDA/worldnews")
        self.assertEqual(p["steps"], [["click", "link", "News", 1]])
        self.assertEqual(_graph.plan(S + "/stock/NVDA/price", "/news")["steps"], [["click", "link", "News", 0]])

    def test_a_missed_click_costs_that_edge(self):
        _graph.observe("browser_goto", {"url": H + "/a"}, {"url": H + "/a"}, "")
        _graph.observe("browser_click", {"role": "link", "name": "B"}, {"url": H + "/b"}, H + "/a")
        _graph.miss(H + "/a", ["link", "B"], "/b")
        p = _graph.plan(H + "/a", "/b")
        self.assertEqual(p["cost"], 2.0)                                      # 1 + 2 * (1 fail / 2 tries)

    def test_plan_rebases_pages_learned_about_another_thing(self):
        S = "https://stock.example.com"
        _graph.observe("browser_goto", {"url": S + "/stock/NVDA/price"}, {"url": S + "/stock/NVDA/price"}, "")
        _graph.observe("browser_click", {"role": "link", "name": "Analysis"},
                       {"url": S + "/stock/NVDA/finance/overview"}, S + "/stock/NVDA/price")
        _graph.observe("browser_goto", {"url": S + "/stock/NVDA/finance/primary"},
                       {"url": S + "/stock/NVDA/finance/primary", "title": "Key financials"}, "")
        _graph.observe("browser_goto", {"url": S + "/stock/AMD/price"}, {"url": S + "/stock/AMD/price"}, "")
        p = _graph.plan(S + "/stock/AMD/price", "key financials")
        self.assertEqual(p["steps"], [["goto", S + "/stock/AMD/finance/primary"]])   # not NVDA's
        self.assertEqual((p["page"], p["rebased"]["to"]), ("/stock/AMD/finance/primary", "AMD"))
        p = _graph.plan(S + "/stock/AMD/price", "Analysis")
        self.assertEqual(p["steps"], [["click", "link", "Analysis"]])
        self.assertEqual(p["page"], "/stock/AMD/finance/overview")
        same = _graph.plan(S + "/stock/NVDA/price", "key financials")              # on NVDA nothing moves
        self.assertEqual(same["steps"], [["goto", S + "/stock/NVDA/finance/primary"]])
        self.assertNotIn("rebased", same)

    def test_a_target_naming_a_used_control_presses_it_on_arrival(self):
        _graph.observe("browser_goto", {"url": H + "/fin"}, {"url": H + "/fin", "title": "Financials"}, "")
        _graph.observe("browser_click", {"role": "radio", "name": "Quarterly"}, {"url": H + "/fin"}, H + "/fin")
        p = _graph.plan(H + "/", "Quarterly")
        self.assertEqual(p["steps"], [["goto", H + "/fin"], ["click", "radio", "Quarterly"]])

    def test_a_full_url_target_is_a_goto_on_its_own_host(self):
        sec = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=NVDA&type=10-Q"
        _graph.observe("browser_goto", {"url": sec}, {"url": sec, "title": "EDGAR"}, "")
        p = _graph.plan(H + "/course/view.php?id=1", sec)
        self.assertEqual((p["found"], p["host"], p["steps"]), (True, "sec.gov", [["goto", sec]]))
        _graph.observe("browser_goto", {"url": H + "/c"}, {"url": H + "/c"}, "")
        _graph.observe("browser_click", {"role": "link", "name": "D"}, {"url": H + "/d"}, H + "/c")
        self.assertEqual(_graph.plan(H + "/", H + "/d")["steps"], [["goto", H + "/d"]])   # the caller gave the address

    def test_a_search_page_is_planned_with_the_target_words(self):
        K = "https://stats.example.kr"
        _graph.observe("browser_goto", {"url": K + "/search/search.do?query=cpi"},
                       {"url": K + "/search/search.do?query=cpi", "title": "Portal"}, "")
        _graph.observe("browser_read", {"mode": "controls"}, {"url": K + "/search/search.do?query=cpi",
                       "untrusted_page_content": [["link", "Monthly consumer prices"]]}, "")
        p = _graph.plan(K + "/", "unemployment rate")                              # nothing matches: search it
        self.assertEqual((p["found"], p["steps"], p["searched"]),
                         (True, [["goto", K + "/search/search.do?query=unemployment+rate"]], True))
        _graph.observe("browser_goto", {"url": K + "/search/search.do?query=jobs"},
                       {"url": K + "/search/search.do?query=jobs"}, "")
        p = _graph.plan(K + "/", "consumer prices")                                # matches, but not the last search
        self.assertEqual(p["steps"], [["goto", K + "/search/search.do?query=consumer+prices"]])

    def test_a_full_url_target_never_seen_is_still_a_goto(self):
        seen = "https://www.sec.gov/cgi-bin/browse-edgar?CIK=NVDA&action=getcompany&count=40&type=10-Q"
        _graph.observe("browser_goto", {"url": seen}, {"url": seen}, "")
        other = "https://www.sec.gov/cgi-bin/browse-edgar?CIK=NVDA&action=getcompany&count=10&type=10-Q"
        p = _graph.plan("https://stock.example.com/stock/NVDA/price", other)
        self.assertEqual((p["found"], p["steps"], p["known"]), (True, [["goto", other]], False))

    def test_a_toggle_named_inside_a_longer_target_is_pressed(self):
        S = "https://stock.example.com"
        _graph.observe("browser_goto", {"url": S + "/stock/NVDA/finance"}, {"url": S + "/stock/NVDA/finance",
                       "title": "Financials | NVDA"}, "")
        _graph.observe("browser_click", {"role": "radio", "name": "Quarterly"}, {"url": S + "/stock/NVDA/finance"},
                       S + "/stock/NVDA/finance")
        _graph.observe("browser_click", {"role": "link", "name": "News"}, {"url": S + "/news"}, S + "/stock/NVDA/finance")
        p = _graph.plan(S + "/", "NVDA quarterly")
        self.assertEqual(p["steps"], [["goto", S + "/stock/NVDA/finance"], ["click", "radio", "Quarterly"]])
        p = _graph.plan(S + "/", "NVDA financials news")               # a link inside the words is not pressed
        self.assertNotIn(["click", "link", "News"], p["steps"])

    def test_from_a_page_about_no_thing_the_target_must_name_it(self):
        S = "https://stock.example.com"
        for t in ("NVDA.O", "JPM"):
            _graph.observe("browser_goto", {"url": S + f"/stock/{t}/finance"},
                           {"url": S + f"/stock/{t}/finance", "title": "Financials"}, "")
        _graph.observe("browser_goto", {"url": S + "/news"}, {"url": S + "/news", "title": "News"}, "")
        p = _graph.plan(S + "/news", "financials")
        self.assertEqual(p["ambiguous"]["candidates"], ["JPM", "NVDA.O"])
        p = _graph.plan(S + "/news", "NVDA financials")
        self.assertEqual((p["page"], "ambiguous" in p), ("/stock/NVDA.O/finance", False))
        p = _graph.plan(S + "/stock/JPM/price", "financials")          # on a thing's page it is that thing
        self.assertEqual((p["page"], "ambiguous" in p), ("/stock/JPM/finance", False))

    def test_go_refuses_an_ambiguous_thing(self):
        S = "https://stock.example.com"
        for t in ("NVDA.O", "JPM"):
            _graph.observe("browser_goto", {"url": S + f"/stock/{t}/finance"},
                           {"url": S + f"/stock/{t}/finance", "title": "Financials"}, "")
        with mock.patch.object(mcp_server, "_browser", side_effect=ToolsTest._fake(None, {})), \
                mock.patch.object(mcp_server, "_opened", return_value={"T"}):
            mcp_server.call("browser_open", {"url": S + "/news", "goal": "financials"})
            r = mcp_server.call("browser_run", {"tab": "T", "steps": [["go", "financials"]]})
        self.assertIn("ambiguous", r["steps"][0]["error"])
        self.assertEqual(r["completed"], 0)

    def test_a_wait_that_never_held_is_not_a_page_to_go_to(self):
        S = "https://stock.example.com"
        _graph.observe("browser_open", {"url": S + "/stock/JPM.K/price"},
                       {"url": S + "/stock/JPM.K/price", "wait_timed_out": "not met"}, "")
        m = {p["path"]: p for p in _graph.host_map("stock.example.com")["top"]}
        self.assertEqual((m["/stock/JPM.K/price"]["visits"], m["/stock/JPM.K/price"]["direct"]), (1, [0, 1]))
        self.assertFalse(_graph.plan(S + "/", "/stock/JPM.K/price")["found"])

    def test_plan_without_a_match_lists_known_pages(self):
        _graph.observe("browser_goto", {"url": H + "/"}, {"url": H + "/", "title": "Home"}, "")
        p = _graph.plan(H + "/", "transcript")
        self.assertFalse(p["found"])
        self.assertEqual(p["known_pages"], ["/"])

    def test_thing_choose_and_explicit_pick(self):
        for thing in ("NVDA.O", "JPM"):
            _graph.page(H + f"/stock/{thing}/finance", "Financials", direct=True)
        p = _graph.plan(H + "/", "financials")
        self.assertEqual(p["choose"]["kind"], "thing")
        self.assertEqual(p["choose"]["at"], 0)
        self.assertEqual(p["choose"]["candidates"], ["JPM", "NVDA.O"])
        self.assertEqual(p["choose"]["picked"], p["ambiguous"]["picked"])
        self.assertEqual(set(p["choose"]), {"kind", "at", "candidates", "picked", "read", "answer"})
        p = _graph.plan(H + "/", "financials", picks={"thing": "NVDA"})
        self.assertEqual(p["page"], "/stock/NVDA.O/finance")
        self.assertNotIn("choose", p)
        self.assertNotIn("ambiguous", p)

    def test_toggle_choose_lists_seen_controls_with_used_first(self):
        _graph.page(H + "/fin", "Financials", direct=True)
        _graph.control(H + "/fin", "radio", "Annual")
        _graph.control(H + "/fin", "radio", "Quarterly", used=True)
        p = _graph.plan(H + "/", "financials")
        self.assertEqual(p["choose"], {
            "kind": "toggle", "at": 1, "candidates": [["radio", "Quarterly"], ["radio", "Annual"]],
            "picked": None, "read": "the page's toggles: which one answers the target",
            "answer": 'picks={"toggle": "<name>"} or {"toggle": ""} to keep the default'})
        self.assertEqual(p["steps"], [["goto", H + "/fin"]])
        self.assertNotIn("choose", _graph.plan(H + "/", "Quarterly"))

    def test_toggle_pick_and_keep_default(self):
        _graph.page(H + "/fin", "Financials", direct=True)
        _graph.control(H + "/fin", "radio", "Annual")
        _graph.control(H + "/fin", "radio", "Quarterly", used=True)
        for name, extra in (("Annual", [["click", "radio", "Annual"]]), ("", [])):
            with self.subTest(name=name):
                p = _graph.plan(H + "/", "financials", picks={"toggle": name})
                self.assertEqual(p["steps"], [["goto", H + "/fin"]] + extra)
                self.assertNotIn("choose", p)
        p = _graph.plan(H + "/", "Quarterly", picks={"toggle": ""})
        self.assertEqual(p["steps"], [["goto", H + "/fin"]])

    def test_unused_toggles_do_not_require_a_choice(self):
        _graph.page(H + "/fin", "Financials", direct=True)
        _graph.control(H + "/fin", "tab", "Quarterly")
        self.assertNotIn("choose", _graph.plan(H + "/", "financials"))

    def test_news_checks_and_fresh_overrides(self):
        _graph.page(H + "/news", "Headlines", direct=True)
        self.assertEqual(_graph.plan(H + "/", "Headlines")["checks"], {"fresh_days": 14})
        self.assertEqual(_graph.plan(H + "/", "Headlines", picks={"fresh": 3})["checks"], {"fresh_days": 3})
        self.assertNotIn("checks", _graph.plan(H + "/", "Headlines", picks={"fresh": 0}))
        self.assertEqual(_graph.plan(H + "/", H + "/news")["checks"], {"fresh_days": 14})
        _graph.page(H + "/fin", "Financials", direct=True)
        self.assertNotIn("checks", _graph.plan(H + "/", "Financials"))
        self.assertEqual(_graph.plan(H + "/", "Financials", picks={"fresh": 7})["checks"], {"fresh_days": 7})

    def test_dated_list_words_in_titles_and_targets(self):
        for word in ("news", "뉴스", "공시", "기사", "filing", "filings", "press", "보도"):
            with self.subTest(word=word):
                _graph.page(H + "/list", word, direct=True)
                self.assertEqual(_graph.plan(H + "/", "/list")["checks"], {"fresh_days": 14})
                _graph.page(H + "/list", "List", direct=True)
                self.assertEqual(_graph.plan(H + "/", word + " List")["checks"], {"fresh_days": 14})

    def test_named_other_thing_beats_start_rebase(self):
        for thing in ("AMD.O", "NVDA.O"):
            _graph.page(H + f"/stock/{thing}/price", "Price", direct=True)
        _graph.page(H + "/stock/NVDA.O/news", "NVDA news", direct=True)
        p = _graph.plan(H + "/stock/AMD.O/price", "NVDA news")
        self.assertEqual(p["page"], "/stock/NVDA.O/news")
        self.assertEqual(p["steps"], [["goto", H + "/stock/NVDA.O/news"]])
        self.assertNotIn("choose", p)
        p = _graph.plan(H + "/stock/AMD.O/price", "news", picks={"thing": "NVDA"})
        self.assertEqual(p["page"], "/stock/NVDA.O/news")

    def test_popup_observe_and_plan_expansion_and_failure_cost(self):
        _graph.page(H + "/search", "Search", direct=True)
        r = _graph.observe("browser_click", {"role": "link", "name": "Table", "index": 1},
                           {"url": H + "/search", "new_tab": {"tab": "N", "url": H + "/table"}}, H + "/search")
        self.assertEqual(r["pages"], 1)
        p = _graph.plan(H + "/search", "/table")
        self.assertEqual(p["steps"], [["click", "link", "Table", 1], ["tab", "new"]])
        self.assertEqual(p["cost"], 1.0)
        _graph.miss(H + "/search", ["link", "Table", 1], "/table")
        self.assertEqual(_graph.plan(H + "/search", "/table")["cost"], 2.0)
        _graph.observe("browser_click", {"role": "link", "name": "Continue"},
                       {"url": H + "/end"}, H + "/table")
        self.assertEqual(_graph.plan(H + "/search", "/end")["steps"],
                         [["click", "link", "Table", 1], ["tab", "new"], ["click", "link", "Continue"]])

    def test_popup_cross_host_records_page_without_edge(self):
        _graph.observe("browser_click", {"role": "button", "name": "Open"},
                       {"new_tab": {"tab": "N", "url": "https://elsewhere.org/table"}}, H + "/search")
        self.assertEqual(_graph.host_map("elsewhere.org")["pages"], 1)
        self.assertEqual(_graph.host_map("lms.example.org")["edges"], 0)

    def test_kinds_fold_pages_edges_and_toggles_without_writes(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        for thing in ("NVDA.O", "JPM"):
            for suffix in ("price", "news"):
                _graph.page(H + f"/stock/{thing}/{suffix}", suffix.title(), con=con)
            _graph.edge(H + f"/stock/{thing}/price", "click", ["link", "News"],
                        H + f"/stock/{thing}/news", True, con=con)
            _graph.control(H + f"/stock/{thing}/price", "tab", "Daily", used=True, con=con)
        _graph.page(H + "/search", "Search", con=con)
        _graph.edge(H + "/search", "popup", ["button", "Open"], H + "/stock/JPM/price", True, con=con)
        _graph.edge(H + "/search", "link", "News", H + "/stock/JPM/news", None, con=con)
        before = list(con.iterdump())
        result = _graph.kinds("www.lms.example.org", con=con)
        by = {k["template"]: k for k in result}
        self.assertEqual(by["/stock/{thing}/price"], {
            "template": "/stock/{thing}/price", "things": ["JPM", "NVDA.O"], "title": "Price",
            "next": [{"template": "/stock/{thing}/news", "via": "click", "label": ["link", "News"]}],
            "toggles": [["tab", "Daily"]]})
        self.assertEqual(by["/search"]["things"], [])
        self.assertEqual({n["via"] for n in by["/search"]["next"]}, {"link", "popup"})
        self.assertEqual(_graph.host_map("lms.example.org", con=con)["kinds"], result)
        self.assertEqual(list(con.iterdump()), before)
        self.assertEqual(_graph.kinds("unknown.org", con=con), [])


class FakeTab:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "Page.getLayoutMetrics":
            return {"cssContentSize": {"width": 1200, "height": 30000}}
        return {"data": base64.b64encode(b"\x89PNG fake").decode()}


class ToolsTest(unittest.TestCase):
    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()
        os.environ.pop("BROWSER_ATLAS_MAP", None)
        mcp_server.REC.open.clear()

    def test_full_screenshot_clips_the_page_and_saves_without_overwriting(self):
        c = FakeTab()
        path = os.path.join(tempfile.mkdtemp(), "shot.png")
        r = mcp_server._screenshot(c, full=True, save=path)
        params = c.calls[-1][1]
        self.assertTrue(params["captureBeyondViewport"])
        self.assertEqual(params["clip"]["height"], mcp_server.FULL_MAX_PX)
        self.assertEqual((r["page_px"], r["cut_at_px"], r["saved"]), ([1200, 30000], mcp_server.FULL_MAX_PX, path))
        self.assertTrue(os.path.exists(path))
        with self.assertRaises(FileExistsError):
            mcp_server._screenshot(c, save=path)
        with self.assertRaises(ValueError):
            mcp_server._screenshot(c, save="relative.png")

    def _fake(self, pages):
        state = {"url": H + "/course/view.php?id=1"}

        def fake_browser(name, args):
            if name == "browser_open":
                state["url"] = args["url"]
                return {"tab": "T", "url": args["url"], "title": "x"}
            if name == "browser_goto":
                state["url"] = args["url"]
                return {"url": args["url"], "title": "x"}
            if name == "browser_read":
                body = pages.get(state["url"], "")
                if args.get("mode") == "links":
                    links = [[t, u] for t, u in body if args.get("find", "") in (t + u)] if isinstance(body, list) else []
                    return {"url": state["url"], "untrusted_page_content": links}
                text = body if isinstance(body, str) else ""
                hit = args.get("find", "") in text
                return {"url": state["url"], "untrusted_page_content": [text] if hit else []}
            if name == "browser_close":
                return {"closed": True}
            return {"url": state["url"]}
        return fake_browser

    def test_each_link_runs_steps_per_same_host_link_and_if_text_branches(self):
        pages = {H + "/course/view.php?id=1": [["Board A", H + "/mod/board/view.php?id=1"],
                                              ["Board B", H + "/mod/board/view.php?id=2"],
                                              ["Board X", "https://evil.example.com/mod/board/view.php?id=3"]],
                 H + "/mod/board/view.php?id=1": "Notice: no class on 10/27",
                 H + "/mod/board/view.php?id=2": "Notice: midterm results"}
        with mock.patch.object(mcp_server, "_browser", side_effect=self._fake(pages)), \
                mock.patch.object(mcp_server, "_opened", return_value={"T"}):
            mcp_server.call("browser_open", {"url": H + "/course/view.php?id=1", "goal": "notices"})
            r = mcp_server.call("browser_run", {"tab": "T", "steps": [
                ["each_link", "board/view.php", [["goto", "{url}"], ["if_text", "no class", [["read", "text"]]]]]]})
        ops = [(s["step"], s["op"]) for s in r["steps"]]
        self.assertEqual(r["steps"][0]["result"], {"links": 2, "running": 2})   # the other host was dropped
        self.assertIn(("0.0.0", "goto"), ops)
        self.assertIn(("0.0.1.0", "read"), ops)                  # branch taken on the first board
        self.assertNotIn(("0.1.1.0", "read"), ops)               # and not on the second
        self.assertTrue(all(s["ok"] for s in r["steps"]))

    def test_go_plans_from_where_the_tab_is_and_runs_it(self):
        pages = {H + "/course/view.php?id=1": [["Class Announcements", H + "/mod/board/view.php?id=7"]]}
        with mock.patch.object(mcp_server, "_browser", side_effect=self._fake(pages)), \
                mock.patch.object(mcp_server, "_opened", return_value={"T"}):
            mcp_server.call("browser_open", {"url": H + "/course/view.php?id=1", "goal": "notices"})
            mcp_server.call("browser_read", {"tab": "T", "mode": "links"})
            r = mcp_server.call("browser_run", {"tab": "T", "steps": [["go", "class announcements"]]})
            plan = mcp_server.call("browser_plan", {"target": "nothing like this", "tab": "T"})
            closed = mcp_server.call("browser_close", {"tab": "T", "result": "success"})
        self.assertEqual(r["steps"][0]["result"]["planned"], [["goto", H + "/mod/board/view.php?id=7"]])
        self.assertEqual(r["steps"][1]["step"], "0.0")
        self.assertFalse(plan["found"])
        self.assertGreaterEqual(closed["episode"]["learned"]["pages"], 2)

    def test_go_fails_when_the_tab_lands_elsewhere_and_counts_the_click(self):
        _graph.observe("browser_goto", {"url": H + "/course/view.php?id=1"}, {"url": H + "/course/view.php?id=1"}, "")
        _graph.observe("browser_click", {"role": "link", "name": "Grades"},
                       {"url": H + "/grade/user.php"}, H + "/course/view.php?id=1")
        with mock.patch.object(mcp_server, "_browser", side_effect=self._fake({})), \
                mock.patch.object(mcp_server, "_opened", return_value={"T"}), mock.patch("time.sleep"):
            mcp_server.call("browser_open", {"url": H + "/course/view.php?id=1", "goal": "grades"})
            r = mcp_server.call("browser_run", {"tab": "T", "steps": [["go", "Grades"], ["read", "text"]]})
        self.assertIn("landed on", r["steps"][-1]["error"])           # the fake click goes nowhere
        self.assertEqual((r["completed"], r["of"]), (0, 2))
        self.assertEqual(_graph.plan(H + "/course/view.php?id=1", "/grade/user.php")["cost"], 2.0)

    def test_open_reports_the_local_map_once_the_host_has_pages(self):
        for p in ("/a", "/b"):
            _graph.observe("browser_goto", {"url": H + p}, {"url": H + p, "title": p}, "")
        with mock.patch.object(mcp_server, "_browser", side_effect=self._fake({})), \
                mock.patch.object(mcp_server, "_opened", return_value={"T"}):
            r = mcp_server.call("browser_open", {"url": H + "/a", "goal": "x"})
        self.assertEqual(r["local_map"]["pages"], 2)
        self.assertIn("browser_plan", r["local_map"]["hint"])

    def test_nothing_in_the_graph_reaches_the_share_preview(self):
        _graph.observe("browser_goto", {"url": H + "/secret?q=my+search"}, {"url": H + "/secret?q=my+search"}, "")
        from browser_atlas import _memory
        self.assertNotIn("my+search", json.dumps(_memory.shareable(), ensure_ascii=False))


class ChoiceReviewTest(unittest.TestCase):
    """Cases a review found in the first choice-point version, each reproduced before the fix."""

    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()
        os.environ.pop("BROWSER_ATLAS_MAP", None)

    def test_an_ordinary_word_is_not_a_ticker(self):
        for thing in ("AMD", "ON"):
            for suffix, title in (("price", "Price"), ("finance", "Return on equity")):
                _graph.page(H + f"/stock/{thing}/{suffix}", title, direct=True)
        self.assertEqual(_graph.plan(H + "/stock/AMD/price", "return on equity")["page"], "/stock/AMD/finance")
        self.assertEqual(_graph.plan(H + "/stock/AMD/price", "ON return on equity")["page"], "/stock/ON/finance")

    def test_a_named_toggle_only_seen_is_pressed(self):
        _graph.page(H + "/fin", "Financials", direct=True)
        _graph.control(H + "/fin", "radio", "Annual", used=True)
        _graph.control(H + "/fin", "radio", "Quarterly")
        p = _graph.plan(H + "/", "Financials Quarterly")
        self.assertEqual(p["steps"][-1], ["click", "radio", "Quarterly"])
        self.assertNotIn("choose", p)

    def test_an_unknown_toggle_pick_is_asked_again(self):
        _graph.page(H + "/fin", "Financials", direct=True)
        _graph.control(H + "/fin", "radio", "Annual", used=True)
        p = _graph.plan(H + "/", "Financials", picks={"toggle": "Monthly"})
        self.assertEqual((p["choose"]["kind"], p["choose"]["unmatched"]), ("toggle", "Monthly"))
        p = _graph.plan(H + "/", "Financials", picks={"toggle": " annual "})
        self.assertEqual(p["steps"][-1], ["click", "radio", "Annual"])

    def test_a_one_letter_thing_can_be_picked(self):
        for thing in ("F", "T"):
            _graph.page(H + f"/stock/{thing}/finance", "Financials", direct=True)
        p = _graph.plan(H + "/", "Financials", picks={"thing": "F"})
        self.assertEqual((p["page"], "choose" in p), ("/stock/F/finance", False))

    def test_a_pick_reaches_a_thing_past_the_shortlist(self):
        for i in range(13):
            _graph.page(H + f"/stock/T{i:02}/finance", "Financials", direct=True)
        p = _graph.plan(H + "/", "Financials", picks={"thing": "T00"})
        self.assertEqual((p["page"], "choose" in p), ("/stock/T00/finance", False))
        p = _graph.plan(H + "/", "Financials", picks={"thing": "ZZZ"})
        self.assertEqual(p["choose"]["unmatched"], "ZZZ")

    def test_a_toggle_pick_keeps_other_buttons(self):
        _graph.page(H + "/fin", "Financials", direct=True)
        _graph.control(H + "/fin", "button", "Download", used=True)
        p = _graph.plan(H + "/", "Download", picks={"toggle": ""})
        self.assertEqual(p["steps"][-1], ["click", "button", "Download"])

    def test_press_inside_a_word_is_not_a_dated_list(self):
        _graph.page(H + "/compression", "Compression", direct=True)
        self.assertNotIn("checks", _graph.plan(H + "/", "Compression"))
        _graph.page(H + "/newsroom", "Newsroom", direct=True)
        self.assertIn("checks", _graph.plan(H + "/", "Newsroom"))


if __name__ == "__main__":
    unittest.main()
