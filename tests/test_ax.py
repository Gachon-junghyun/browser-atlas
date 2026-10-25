import unittest
from unittest.mock import patch

from browser_atlas import _atlas, _ax, _check
from browser_atlas._mouse import Model
from browser_atlas._pointer import Pointer


def node(role, name, backend=1, ignored=False):
    return {"role": {"value": role}, "name": {"value": name},
            "backendDOMNodeId": backend, "ignored": ignored}


class FakeCDP:
    target = {"id": "own"}

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.nodes = [node("button", "Search"), node("textbox", "Company", 2)]
        self.url = "https://example.com"
        self.hidden = set()

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "Accessibility.getFullAXTree":
            return {"nodes": self.nodes}
        if method == "DOM.getBoxModel" and params["backendNodeId"] in self.hidden:
            raise RuntimeError("no box")
        if method == "DOM.pushNodesByBackendIdsToFrontend":
            return {"nodeIds": [42]}
        return {}

    def eval(self, js):
        return self.url

    def goto(self, url):
        self.url = url

    def until(self, js, timeout):
        self.calls.append(("until", (js, timeout)))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class AXTest(unittest.TestCase):
    def test_snapshot_and_find(self):
        c = FakeCDP()
        c.nodes = [node("button", " Search  now\n", 1), node("button", "Search now", 2),
                   node("button", "Hidden", 3, True), node("paragraph", "Text", 4)]
        c.hidden = {1}
        nodes = _ax.snapshot(c)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(c.calls[0][0], "Accessibility.enable")
        self.assertEqual(_ax.find(c, "button", "Search now", nodes=nodes)["backend"], 2)
        self.assertIsNone(_ax.find(c, "button", "search NOW", nodes=nodes))
        self.assertEqual(_ax.find(c, "button", "sEaRcH", exact=False, nodes=nodes)["backend"], 2)
        self.assertIsNone(_ax.find(c, "link", "Search now", nodes=nodes))
        c.hidden.add(2)
        self.assertEqual(_ax.find(c, "button", "Search now", nodes=nodes)["backend"], 1)
        self.assertIsNone(_ax.find(c, "button", "Hidden"))

    def test_mark(self):
        c = FakeCDP()
        selector = _ax.mark(c, 7)
        self.assertEqual([m for m, _ in c.calls], ["DOM.getDocument",
                         "DOM.pushNodesByBackendIdsToFrontend", "DOM.setAttributeValue"])
        params = c.calls[-1][1]
        self.assertEqual(params["nodeId"], 42)
        self.assertEqual(params["name"], "data-atlas-target")
        self.assertEqual(selector, f'[data-atlas-target="{params["value"]}"]')

    def test_control_paths(self):
        c = FakeCDP()
        with patch("browser_atlas._pointer._load_state", return_value={}):
            p = Pointer(c, model=Model())
        with patch.object(p, "click", return_value={}) as click:
            p.click_control("button", "Search")
            self.assertTrue(click.call_args.args[0].startswith("[data-atlas-target="))
        with patch.object(p, "move", return_value={}) as move:
            p.move_control("button", "Search")
            move.assert_called_once()
        with patch.object(p, "type", return_value={}) as typing:
            p.type_control("textbox", "Company", "abc", clear=True)
            self.assertEqual(typing.call_args.args[1], "abc")
        with self.assertRaisesRegex(RuntimeError, "no control: role=.*nearest:.*Search"):
            p.click_control("button", "Absent")

    def test_blocked_before_input(self):
        c = FakeCDP()
        c.url = "https://claude.ai/new"
        with patch("browser_atlas._pointer._load_state", return_value={}):
            p = Pointer(c, model=Model())
        for action in (lambda: p.click_control("button", "Search"),
                       lambda: p.move_control("button", "Search"),
                       lambda: p.type("input", "abc", clear=True),
                       lambda: p.type_control("textbox", "Company", "abc")):
            with self.assertRaises(_atlas.Blocked):
                action()
        self.assertEqual(c.calls, [])


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.sites = {"example": {"access": "open", "hosts": ["example.com"], "pages": {
            "search": {"url": "https://example.com", "ready": "true", "controls": [
                {"role": "button", "name": "Search"}, {"role": "link", "name": "Missing"}]},
            "other": {"url": "https://example.com/other", "controls": []}}}}

    def test_missing_and_own_tabs_only(self):
        c = FakeCDP()
        with patch.object(_check, "new_tab", side_effect=[{"id": "own1"}, {"id": "own2"}]) as new, \
                patch.object(_check, "close_tab") as close, \
                patch.object(_check, "CDP", return_value=c) as connect, \
                patch.object(_check.time, "sleep") as sleep:
            result = _check.check("example", 9222, self.sites)
        page = result["pages"]["search"]
        self.assertFalse(page["ok"])
        self.assertEqual(page["found"], [{"role": "button", "name": "Search"}])
        self.assertEqual(page["missing"], [{"role": "link", "name": "Missing"}])
        self.assertEqual(close.call_args_list, [unittest.mock.call("own1", 9222),
                                               unittest.mock.call("own2", 9222)])
        self.assertEqual(new.call_count, 2)
        self.assertEqual(connect.call_args_list[0], unittest.mock.call(9222, match="own1"))
        self.assertEqual(sum(m == "Accessibility.getFullAXTree" for m, _ in c.calls), 2)
        self.assertFalse(any(m.startswith("Input.") for m, _ in c.calls))
        sleep.assert_called_once()

    def test_blocked_skipped(self):
        self.sites["example"]["access"] = "blocked"
        with patch.object(_check, "new_tab") as new:
            self.assertEqual(_check.check("example", 9222, self.sites)["pages"], {})
        new.assert_not_called()

    def test_cleanup_on_failure(self):
        with patch.object(_check, "new_tab", return_value={"id": "own"}), \
                patch.object(_check, "close_tab") as close, \
                patch.object(_check, "CDP", side_effect=RuntimeError("connection failed")), \
                patch.object(_check.time, "sleep"):
            result = _check.check("example", 9222, self.sites)
        self.assertFalse(result["pages"]["search"]["ok"])
        self.assertEqual(close.call_count, 2)


if __name__ == "__main__":
    unittest.main()
