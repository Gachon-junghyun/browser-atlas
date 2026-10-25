import os
import tempfile
import unittest

from browser_atlas import _safety


class FakeCDP:
    """A page with one control; records every CDP call."""
    target = {"id": "fake"}
    port = 9555

    def __init__(self, url, name, role="button", search=False):
        self.url, self.name, self.role, self.search = url, name, role, search
        self.calls = []

    def eval(self, js, timeout=30):
        if js == "location.href":
            return self.url
        if "elementFromPoint" in js:
            return {"x": 50, "y": 20, "w": 100, "h": 30, "vw": 800, "vh": 600,
                    "name": self.name, "role": self.role, "search": self.search}
        return self.name

    def call(self, method, params=None, timeout=30):
        self.calls.append(method)
        return {}


class SafetyTest(unittest.TestCase):
    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()

    def test_patterns(self):
        for name in ("결제하기", "주문하기", "Place order", "Submit", "삭제", "지원하기", "Send message"):
            self.assertTrue(_safety.irreversible(name, "button"), name)
        for name in ("검색", "다음 페이지", "Search", "Posted by", "Submitted filings", "상세 보기"):
            self.assertFalse(_safety.irreversible(name, "button"), name)
        self.assertFalse(_safety.irreversible("주문 번호", "textbox"))   # typing into a field is not pressing

    def test_map_can_only_tighten(self):
        self.assertTrue(_safety.irreversible("확인", "button", {"irreversible": True}))
        self.assertTrue(_safety.irreversible("결제", "button", {"irreversible": False, "act": "search"}))
        self.assertTrue(_safety.irreversible("Go", "button", {"act": "pay"}))

    def test_allow_is_one_time(self):
        _safety.allow("결제하기")
        _safety.require("결제하기", "button")                     # consumes it
        with self.assertRaises(_safety.NeedsHuman):
            _safety.require("결제하기", "button")

    def test_pointer_stops_before_any_input_event(self):
        from browser_atlas._pointer import Pointer
        c = FakeCDP("https://shop.example.com/cart", "결제하기")
        with self.assertRaises(_safety.NeedsHuman):
            Pointer(c).click("#pay")
        self.assertEqual([m for m in c.calls if m.startswith("Input.")], [])

    def test_enter_only_in_search_fields(self):
        from browser_atlas._pointer import Pointer
        c = FakeCDP("https://shop.example.com/", "메모", role="textbox", search=False)
        with self.assertRaises(_safety.NeedsHuman):
            Pointer(c).type("#memo", "hello\n")
        self.assertEqual([m for m in c.calls if m.startswith("Input.")], [])

    def test_denied_port(self):
        from browser_atlas._pointer import Pointer
        os.environ["BROWSER_ATLAS_DENY_PORTS"] = "9555"
        try:
            with self.assertRaises(_safety.NeedsHuman):
                Pointer(FakeCDP("https://example.com/", "검색")).click("#go")
        finally:
            del os.environ["BROWSER_ATLAS_DENY_PORTS"]


if __name__ == "__main__":
    unittest.main()
