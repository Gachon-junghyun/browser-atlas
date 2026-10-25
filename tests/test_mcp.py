import io
import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
