import random
import unittest
from unittest.mock import patch

from browser_atlas._keys import TypingModel, compositions, decompose, key_events
from browser_atlas._mouse import Model
from browser_atlas._pointer import Pointer


class FakeCDP:
    target = {"id": "fake"}

    def __init__(self, value):
        self.value, self.calls = value, []

    def eval(self, js):
        return "https://example.com" if js == "location.href" else self.value

    def call(self, method, params):
        self.calls.append((method, params))
        return {}


class KeysTest(unittest.TestCase):
    def test_hangul_decomposition(self):
        for char, expected in (("한", "ㅎㅏㄴ"), ("과", "ㄱㅗㅏ"), ("닭", "ㄷㅏㄹㄱ")):
            self.assertEqual(decompose(char), list(expected))
        self.assertEqual(compositions("한"), ["ㅎ", "하", "한"])
        self.assertEqual(compositions("과"), ["ㄱ", "고", "과"])
        self.assertEqual(compositions("닭"), ["ㄷ", "다", "달", "닭"])

    def test_composition_events(self):
        plan = TypingModel().plan("한", random.Random(1))
        self.assertEqual([p["text"] for _, m, p in plan if m == "Input.imeSetComposition"],
                         ["ㅎ", "하", "한"])
        self.assertEqual(plan[-1][1:], ("Input.insertText", {"text": "한"}))
        keys = [p for _, m, p in plan if m == "Input.dispatchKeyEvent"]
        self.assertEqual([p["type"] for p in keys], ["keyDown", "keyUp"] * 3)
        self.assertTrue(all(p["key"] == "Process" and p["windowsVirtualKeyCode"] == 229 for p in keys))

    def test_shift(self):
        for char, code, vk in (("A", "KeyA", 65), ("!", "Digit1", 49)):
            events = key_events(char)
            self.assertEqual([p["key"] for _, p in events], ["Shift", char, char, "Shift"])
            self.assertEqual([p["type"] for _, p in events], ["keyDown", "keyDown", "keyUp", "keyUp"])
            self.assertEqual(events[1][1], {"type": "keyDown", "key": char, "code": code,
                                          "text": char, "windowsVirtualKeyCode": vk, "modifiers": 8})
            self.assertEqual(events[-1][1]["modifiers"], 0)

    def test_timestamps_and_fallback(self):
        plan = TypingModel().plan("A! 한 과 닭 é 中😀\n\t", random.Random(3))
        self.assertTrue(all(a[0] < b[0] for a, b in zip(plan, plan[1:])))
        inserts = [p["text"] for _, m, p in plan if m == "Input.insertText"]
        self.assertEqual(inserts, ["한", "과", "닭", "中", "😀"])
        self.assertEqual(TypingModel().plan(""), [])
        keys = [p["key"] for _, m, p in plan if m == "Input.dispatchKeyEvent"]
        self.assertIn("Enter", keys)
        self.assertIn("Tab", keys)
        self.assertNotIn("Backspace", keys)

    def test_typos_corrected(self):
        plan = TypingModel(typo_rate=1).plan("a", random.Random(2))
        downs = [p["key"] for _, m, p in plan if m == "Input.dispatchKeyEvent" and p["type"] == "keyDown"]
        self.assertEqual(downs, ["s", "Backspace", "a"])

    def test_hold_time_and_latin_expansion(self):
        plan = TypingModel(spread=0).plan("A!ß", random.Random(1))
        downs = {}
        for due, method, params in plan:
            if method != "Input.dispatchKeyEvent" or params["key"] == "Shift":
                continue
            if params["type"] == "keyDown":
                downs[params["key"]] = due
            else:
                self.assertAlmostEqual(due - downs[params["key"]], 85, delta=1)

    def test_every_hangul_syllable(self):
        for codepoint in range(0xAC00, 0xD7A4):
            char = chr(codepoint)
            partials = compositions(char)
            self.assertEqual(len(partials), len(decompose(char)))
            self.assertEqual(partials[-1], char)

    def test_type_verifies_and_clears(self):
        c = FakeCDP("prefix abc")
        with patch("browser_atlas._pointer._load_state", return_value={}):
            p = Pointer(c, model=Model())
        with patch.object(p, "click") as click, patch("browser_atlas._pointer.time.sleep"), \
                patch.object(p, "target", return_value={"search": False, "name": "q"}):
            result = p.type("input", "abc", clear=True)
            click.assert_called_once_with("input")
            self.assertEqual(result["value"], "prefix abc")
            self.assertEqual([params["key"] for _, params in c.calls[:6]],
                             ["Control", "a", "a", "Control", "Backspace", "Backspace"])
            c.value = "wrong"
            with self.assertRaisesRegex(RuntimeError, "input did not stick: expected 'abc', got 'wrong'"):
                p.type("input", "abc")


if __name__ == "__main__":
    unittest.main()
