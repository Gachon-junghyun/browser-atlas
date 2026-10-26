import json
import os
import tempfile
import unittest
from unittest import mock

from browser_atlas import _episodes, _memory, _robots

ROBOTS = """
User-agent: Googlebot
Disallow: /

User-agent: *
User-agent: Bingbot
Disallow: /search
Allow: /search/about
Disallow: /*.pdf$
"""


class RobotsTest(unittest.TestCase):
    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()
        os.environ.pop("BROWSER_ATLAS_MAP", None)

    def test_longest_match_wins_and_only_the_star_group_counts(self):
        rules = _robots.parse(ROBOTS)
        self.assertNotIn(["disallow", "/"], rules)                         # Googlebot's line is not ours
        self.assertEqual(_robots.decide(rules, "/search?q=x"), (False, "disallow: /search"))
        self.assertEqual(_robots.decide(rules, "/search/about"), (True, "allow: /search/about"))
        self.assertFalse(_robots.decide(rules, "/files/a.pdf")[0])
        self.assertTrue(_robots.decide(rules, "/files/a.pdf?x=1")[0])     # $ anchors the end
        self.assertEqual(_robots.decide(rules, "/"), (True, "no matching rule"))

    def test_fetched_once_a_day_and_unknown_is_not_allowed(self):
        calls = []

        def fake(origin):
            calls.append(origin)
            return {"status": "ok", "rules": _robots.parse(ROBOTS), "why": ""}
        with mock.patch.object(_robots, "_fetch", fake):
            self.assertFalse(_robots.check("https://new.example.org/search?q=a")["allowed"])
            self.assertTrue(_robots.check("https://new.example.org/")["allowed"])
        self.assertEqual(calls, ["https://new.example.org"])               # cached
        with mock.patch.object(_robots, "_fetch", lambda o: {"status": "unknown", "rules": [], "why": "HTTP 403"}):
            self.assertIsNone(_robots.check("https://other.example.org/")["allowed"])
        with mock.patch.object(_robots, "_fetch", lambda o: {"status": "missing", "rules": [], "why": "HTTP 404"}):
            self.assertTrue(_robots.check("https://third.example.org/x")["allowed"])

    def test_gate_refuses_only_unmapped_sites(self):
        from browser_atlas import mcp_server
        with mock.patch.object(_robots, "_fetch", lambda o: {"status": "ok", "rules": [["disallow", "/"]], "why": ""}):
            with self.assertRaises(_robots.Disallowed):
                mcp_server._robots_gate("https://new.example.org/page")
            self.assertIsNone(mcp_server._robots_gate("https://kosis.kr/"))    # mapped: reviewed verdict


class TrailTest(unittest.TestCase):
    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()
        os.environ.pop("BROWSER_ATLAS_MAP", None)

    def test_every_step_is_kept_and_typed_text_is_not(self):
        rec = _episodes.Recorder(recover=False)
        rec.start("t1", "https://new.example.org/", "find the data table")
        rec.open["t1"]["robots"] = {"allowed": True, "rule": "no matching rule"}
        rec.step("t1", "browser_click", False, error=RuntimeError("covered by a modal"),
                 control=["button", "Data"], url="https://new.example.org/")
        rec.step("t1", "browser_type", False, error=RuntimeError("input did not stick: expected 'secret words', got ''"),
                 control=["textbox", "Search"], url="https://new.example.org/")
        rec.step("t1", "browser_click", True, control=["link", "Tables"], url="https://new.example.org/tables")
        rec.step("t1", "browser_read", True, result={"untrusted_page_content": "table 1"})
        rec.finish("t1", said="success")
        row = _memory.connect().execute("select notes from experience").fetchone()
        notes = json.loads(row["notes"])
        self.assertEqual([t[0] for t in notes["trail"]],
                         ["browser_click", "browser_type", "browser_click", "browser_read"])
        self.assertEqual(notes["robots"]["allowed"], True)
        self.assertNotIn("secret words", row["notes"])
        d = _memory.host_digest("new.example.org")
        self.assertEqual(d["attempts"], 1)
        self.assertIn(["link", "Tables", 1], d["worked_controls"])
        self.assertTrue(any(f[0] == "browser_click" and "Data" in f[1] for f in d["failed_steps"]))
        self.assertEqual(_memory.host_digest("elsewhere.example.org")["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
