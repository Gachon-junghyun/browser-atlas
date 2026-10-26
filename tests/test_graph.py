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

    def test_plan_without_a_match_lists_known_pages(self):
        _graph.observe("browser_goto", {"url": H + "/"}, {"url": H + "/", "title": "Home"}, "")
        p = _graph.plan(H + "/", "transcript")
        self.assertFalse(p["found"])
        self.assertEqual(p["known_pages"], ["/"])


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


if __name__ == "__main__":
    unittest.main()
