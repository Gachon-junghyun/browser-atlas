"""Pure typing plans; times are milliseconds and events are CDP method/parameter pairs."""
from __future__ import annotations

import math
import random
import unicodedata
from dataclasses import dataclass

INITIAL = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
MEDIAL = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
FINAL = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"
COMPOUND = dict(zip("ㅘㅙㅚㅝㅞㅟㅢㄳㄵㄶㄺㄻㄼㄽㄾㄿㅀㅄ",
                    ("ㅗㅏ", "ㅗㅐ", "ㅗㅣ", "ㅜㅓ", "ㅜㅔ", "ㅜㅣ", "ㅡㅣ", "ㄱㅅ",
                     "ㄴㅈ", "ㄴㅎ", "ㄹㄱ", "ㄹㅁ", "ㄹㅂ", "ㄹㅅ", "ㄹㅌ", "ㄹㅍ", "ㄹㅎ", "ㅂㅅ")))
SHIFTED = dict(zip('~!@#$%^&*()_+{}|:"<>?', "`1234567890-=[]\\;',./"))
PUNCT = dict(zip("`-=[]\\;',./", ("Backquote", "Minus", "Equal", "BracketLeft", "BracketRight",
                                 "Backslash", "Semicolon", "Quote", "Comma", "Period", "Slash")))
VK = dict(zip("`-=[]\\;',./", (192, 189, 187, 219, 221, 220, 186, 222, 188, 190, 191)))


def decompose(char: str) -> list[str]:
    n = ord(char) - 0xAC00
    if not 0 <= n < 11172:
        raise ValueError("not a Hangul syllable")
    parts = INITIAL[n // 588] + MEDIAL[n // 28 % 21] + FINAL[n % 28].strip()
    return [j for part in parts for j in COMPOUND.get(part, part)]


def compositions(char: str) -> list[str]:
    n = ord(char) - 0xAC00
    initial, medial, final = n // 588, n // 28 % 21, n % 28
    out = [INITIAL[initial]]
    vowel = MEDIAL[medial]
    if vowel in COMPOUND:
        out.append(chr(0xAC00 + initial * 588 + MEDIAL.index(COMPOUND[vowel][0]) * 28))
    base = 0xAC00 + initial * 588 + medial * 28
    out.append(chr(base))
    if final:
        tail = FINAL[final]
        if tail in COMPOUND:
            out.append(chr(base + FINAL.index(COMPOUND[tail][0])))
        out.append(char)
    return out


def key_events(char: str) -> list[tuple[str, dict]]:
    special = {"\n": ("Enter", 13), "Enter": ("Enter", 13), "\t": ("Tab", 9),
               "Tab": ("Tab", 9), "Backspace": ("Backspace", 8), "Delete": ("Delete", 46)}
    shift = char in SHIFTED or (char.isupper() and len(char) == 1)
    base = SHIFTED.get(char, char.lower())
    if char in special:
        key, vk = special[char]
        params = {"key": key, "code": key, "windowsVirtualKeyCode": vk}
        if key == "Enter":
            params["text"] = "\r"
    else:
        code = ("Key" + base.upper() if base.isalpha() and base.isascii() else
                "Digit" + base if base.isdigit() and base.isascii() else PUNCT.get(base, "Space" if base == " " else ""))
        upper = char.upper()
        fallback = ord(upper) if len(upper) == 1 else ord(char)
        vk = ord(base.upper()) if base.isascii() and base.isalnum() else VK.get(base, 32 if base == " " else fallback)
        params = {"key": char, "code": code, "text": char, "windowsVirtualKeyCode": vk}
    params["modifiers"] = 8 if shift else 0
    modifier = {"key": "Shift", "code": "ShiftLeft", "windowsVirtualKeyCode": 16, "modifiers": 8}
    out = [("Input.dispatchKeyEvent", {"type": "keyDown", **modifier})] if shift else []
    out += [("Input.dispatchKeyEvent", {"type": "keyDown", **params}),
            ("Input.dispatchKeyEvent", {"type": "keyUp", **{k: v for k, v in params.items() if k != "text"}})]
    if shift:
        out.append(("Input.dispatchKeyEvent", {"type": "keyUp", **modifier, "modifiers": 0}))
    return out


def character_events(char: str) -> list[list[tuple[str, dict]]]:
    if 0xAC00 <= ord(char) <= 0xD7A3:
        groups = []
        for partial in compositions(char):
            params = {"key": "Process", "windowsVirtualKeyCode": 229}
            groups.append([("Input.dispatchKeyEvent", {"type": "keyDown", **params}),
                           ("Input.imeSetComposition", {"text": partial, "selectionStart": len(partial),
                                                        "selectionEnd": len(partial)}),
                           ("Input.dispatchKeyEvent", {"type": "keyUp", **params})])
        groups[-1].append(("Input.insertText", {"text": char}))
        return groups
    if char.isascii() or "LATIN" in unicodedata.name(char, ""):
        return [key_events(char)]
    return [[("Input.insertText", {"text": char})]]


def neighbour(char: str, rng) -> str | None:
    rows = ("1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm")
    for row in rows:
        if char.lower() in row:
            i = row.index(char.lower())
            choices = row[max(0, i - 1):i] + row[i + 1:i + 2]
            wrong = rng.choice(choices)
            return wrong.upper() if char.isupper() else wrong
    return None


@dataclass
class TypingModel:
    interval_ms: float = 140.0
    hold_ms: float = 85.0
    spread: float = 0.25
    space_pause_ms: float = 90.0
    punctuation_pause_ms: float = 180.0
    word_pause_ms: float = 450.0
    words_per_pause: int = 5
    typo_rate: float = 0.0

    def plan(self, text: str, rng=None) -> list[tuple[float, str, dict]]:
        rng = rng or random.Random()
        out, now, words = [], 0.0, 0

        def emit(groups):
            nonlocal now
            for group in groups:
                start = now
                hold = max(1.0, rng.lognormvariate(math.log(max(1.0, self.hold_ms)), self.spread))
                for i, (method, params) in enumerate(group):
                    if i:
                        now += 1.0
                    if params.get("type") == "keyUp" and params.get("key") != "Shift":
                        now = max(now, start + hold + 1.0)
                    out.append((now, method, params))
                interval = rng.lognormvariate(math.log(max(1.0, self.interval_ms)), self.spread)
                now = max(now + 1.0, start + interval)

        for char in text:
            wrong = neighbour(char, rng) if self.typo_rate and rng.random() < self.typo_rate else None
            if wrong:
                emit(character_events(wrong))
                now += 120.0
                emit([key_events("Backspace")])
            emit(character_events(char))
            if char == " ":
                words += 1
                now += self.space_pause_ms
                if self.words_per_pause > 0 and words % self.words_per_pause == 0:
                    now += self.word_pause_ms
            elif unicodedata.category(char).startswith("P"):
                now += self.punctuation_pause_ms
        return out
