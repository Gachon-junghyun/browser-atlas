"""Episodes must survive the failures that once lost a whole visit: a wait that timed out on open,
steps in a tab with no open episode, a lesson named by its login site, a logout link in a hidden menu."""
import json
import os
import tempfile
import unittest
from unittest import mock

from browser_atlas import _episodes, _login, _memory, mcp_server


class FakeCDP:
    def __init__(self, url="https://portal.example.org/p/", raise_wait=True, doc_has_logout=True, visible=False):
        self.url, self.raise_wait = url, raise_wait
        self.doc_has_logout, self.visible = doc_has_logout, visible

    def goto(self, url, until=None, timeout=30.0):
        self.url = url
        if until and self.raise_wait:
            raise TimeoutError(f"not met in 28s: {until}")
        return {"url": url, "title": "t", "elapsed_s": 0.1, "until": True if until else None}

    def eval(self, js):
        if js == "location.href":
            return self.url
        if js == "document.title":
            return "Portal"
        if js.startswith("!!document.querySelector"):
            return self.doc_has_logout
        if "getBoundingClientRect" in js:
            return self.visible
        return None


class RecordingTest(unittest.TestCase):
    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()
        os.environ["BROWSER_ATLAS_CREDS"] = tempfile.mkdtemp()
        os.environ.pop("BROWSER_ATLAS_MAP", None)
        mcp_server.REC.open.clear()

    def tearDown(self):
        os.environ.pop("BROWSER_ATLAS_CREDS", None)

    def test_a_timed_out_wait_still_returns_the_page(self):
        r = mcp_server._goto(FakeCDP(), "https://portal.example.org/p/", "document.body.innerText.includes('cyber')")
        self.assertEqual((r["url"], r["until"]), ("https://portal.example.org/p/", False))
        self.assertIn("not met", r["wait_timed_out"])

    def test_open_with_a_timed_out_wait_is_recorded_and_later_steps_kept(self):
        opened = {"tab": "T1", "port": 9999, "url": "https://cyber.example.org/", "title": "x", "until": False,
                  "wait_timed_out": "not met in 28s"}

        def fake_browser(name, args):
            if name == "browser_open":
                return dict(opened)
            if name == "browser_close":
                return {"closed": True}
            return {"url": "https://cyber.example.org/calendar", "untrusted_page_content": "due today"}
        with mock.patch.object(mcp_server, "_browser", side_effect=fake_browser), \
                mock.patch.object(mcp_server, "_robots_gate", return_value=None), \
                mock.patch.object(mcp_server, "_opened", return_value={"T1"}):
            mcp_server._tracked("browser_open", {"url": "https://cyber.example.org/", "goal": "deadlines today"})
            for _ in range(3):
                mcp_server._tracked("browser_read", {"tab": "T1", "mode": "text"})
            ep = mcp_server._tracked("browser_close", {"tab": "T1", "result": "success"})["episode"]
        self.assertIsNotNone(ep)
        notes = json.loads(_memory.connect().execute("select notes from experience").fetchone()["notes"])
        self.assertEqual([t[0] for t in notes["trail"]],
                         ["browser_open", "browser_wait", "browser_read", "browser_read", "browser_read"])
        self.assertFalse(notes["trail"][1][1])                                    # the timeout is a failed step

    def test_steps_in_a_tab_with_no_episode_are_adopted(self):
        with mock.patch.object(mcp_server, "_browser", return_value={"url": "https://cyber.example.org/x"}), \
                mock.patch.object(mcp_server, "_opened", return_value={"T2"}), \
                mock.patch.object(mcp_server._cdp, "tabs", return_value=[{"id": "T2", "url": "https://cyber.example.org/"}]):
            mcp_server._tracked("browser_read", {"tab": "T2", "mode": "text"})
            self.assertIn("T2", mcp_server.REC.open)
            self.assertEqual(mcp_server.REC.open["T2"]["host"], "cyber.example.org")
        with mock.patch.object(mcp_server, "_browser", return_value={"url": "https://elsewhere.example.org/"}), \
                mock.patch.object(mcp_server, "_opened", return_value=set()):
            mcp_server._tracked("browser_read", {"tab": "NOT_MINE", "mode": "text"})
            self.assertNotIn("NOT_MINE", mcp_server.REC.open)                    # someone else's tab stays untouched

    def test_a_lesson_named_by_its_login_site(self):
        recipe = {"school_portal": {"mode": "auto", "check_url": "https://portal.school.example.org/", "steps": []}}
        with mock.patch.object(_login, "recipes", return_value=recipe):
            r = _episodes.record_lesson("school_portal", "The portal wait text is Log out, not the site name")
        self.assertEqual(r["host"], "portal.school.example.org")

    def test_a_logout_link_in_a_hidden_menu_counts_as_logged_in(self):
        r = {"logged_in_when": {"url_not_contains": "sso.", "css": "a[href*='logout']"}}
        self.assertTrue(_login.is_logged_in(FakeCDP(doc_has_logout=True, visible=False), r, navigate=False))
        self.assertFalse(_login.is_logged_in(FakeCDP(doc_has_logout=False), r, navigate=False))


if __name__ == "__main__":
    unittest.main()
