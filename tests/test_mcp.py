import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from browser_atlas import mcp_server


def run(*msgs):
    out = io.StringIO()
    mcp_server.serve(io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


class McpTest(unittest.TestCase):
    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()

    def test_handshake_and_tool_list(self):
        r = run({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(r), 2)                    # the notification gets no reply
        self.assertEqual(r[0]["result"]["serverInfo"]["name"], "browser-atlas")
        names = {t["name"] for t in r[1]["result"]["tools"]}
        self.assertIn("memory_recall", names)
        self.assertIn("atlas_guard", names)
        self.assertIn("lesson_record", names)
        self.assertIn("lesson_record", r[0]["result"]["instructions"])      # agents are told to leave lessons
        self.assertNotIn("share_merge", names)                               # merging is a maintainer's CLI job

    def test_lesson_record_applies_locally(self):
        r = run({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "lesson_record", "arguments": {
                     "site": "kyobobook", "page": "search", "text": "Results load after the filter bar; wait for it"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "lesson_record", "arguments": {"site": "nowhere", "text": "some lesson here"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "lesson_record", "arguments": {"site": "kyobobook", "text": "call me at 010-1234-5678"}}})
        self.assertEqual(json.loads(r[0]["result"]["content"][0]["text"])["stored"], "this machine only")
        self.assertTrue(r[1]["result"]["isError"])                          # unknown site
        self.assertTrue(r[2]["result"]["isError"])                          # personal data refused
        site = mcp_server._site("kyobobook")
        self.assertTrue(any(t.startswith("Results load after the filter bar") for t in site["pages"]["search"]["traps"]))

    def test_record_then_recall(self):
        args = {"goal": "공시 원문 찾기", "site": "dart", "route": [{"page": "search"}],
                "outcome": "success", "quality": 0.9}
        r = run({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "memory_record", "arguments": args}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "memory_recall", "arguments": {"goal": "공시 원문"}}})
        recalled = json.loads(r[1]["result"]["content"][0]["text"])
        self.assertEqual(recalled[0]["site"], "dart")

    def test_guard_and_errors_do_not_kill_the_server(self):
        r = run({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "atlas_guard", "arguments": {"url": "https://claude.ai/"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "memory_record", "arguments": {"goal": "x", "route": [], "outcome": "great"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertFalse(json.loads(r[0]["result"]["content"][0]["text"])["allowed"])
        self.assertTrue(r[1]["result"]["isError"])
        self.assertEqual(r[2]["error"]["code"], -32601)


@unittest.skipUnless(shutil.which("node"), "Node is needed to execute the DOM fixture")
class FrameTextTest(unittest.TestCase):
    def test_nested_frames_reads_and_waits_share_text(self):
        from browser_atlas import _cdp
        fixture = r"""
function doc(text, frames = []) {
  return {body: {innerText: text}, baseURI: 'https://example.org/',
          querySelectorAll: () => frames};
}
function frame(name, text, frames = []) {
  return {name, src: '/table.html', contentDocument: doc(text, frames)};
}
const blocked = {get contentDocument() { throw Error('cross-origin'); }};
const document = doc('Skip to content', [blocked, frame('outer', 'Table window', [
  frame('', 'Producer prices year-on-year: 2.5%', [
    frame('third', 'Depth three', [frame('fourth', 'Must be excluded')])])]),
  frame('sibling', 'Other table')]);
"""
        class FakeCDP:
            def eval(self, js):
                if js == "location.href":
                    return "https://example.org/stats"
                result = subprocess.run([shutil.which("node"), "-e", fixture +
                                         "console.log(JSON.stringify(" + js + "));"],
                                        check=True, capture_output=True, encoding="utf-8")
                return json.loads(result.stdout)

            def until(self, js, timeout=15):
                if not self.eval(js):
                    raise TimeoutError("missing text")
                return True

            def goto(self, url, js):
                return {"until": self.until(js)}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

        c = FakeCDP()
        whole = mcp_server._read(c, {"mode": "text"})["untrusted_page_content"]
        self.assertEqual(whole, "Skip to content\n--- frame: outer ---\nTable window\n"
                         "--- frame: /table.html ---\nProducer prices year-on-year: 2.5%\n"
                         "--- frame: third ---\nDepth three\n--- frame: sibling ---\nOther table")
        found = mcp_server._read(c, {"mode": "text", "find": "YEAR-ON-YEAR"})
        self.assertIn("2.5%", "".join(found["untrusted_page_content"]))
        cut = mcp_server._read(c, {"mode": "text", "max_chars": 35})
        self.assertEqual(cut["untrusted_page_content"], whole[:35])
        self.assertTrue(cut["truncated"])
        js = mcp_server._wait_js({"wait_text": "year-on-year"})
        self.assertTrue(mcp_server._goto(c, "https://example.org/stats", js)["until"])
        with mock.patch.object(_cdp, "CDP", return_value=c):
            result = mcp_server._browser("browser_wait", {"tab": "fixture", "wait_text": "2.5%"})
        self.assertTrue(result["met"])
        with self.assertRaises(TimeoutError):
            c.until(mcp_server._wait_js({"wait_text": "Must be excluded"}))


if __name__ == "__main__":
    unittest.main()
