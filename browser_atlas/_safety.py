"""Local safety rules for actions. Decided on this machine, never by shared data.

The button map may say a control is harmless; it may be wrong or written by someone hostile.
So whether a control is irreversible is judged here, from the live control's own name and role,
and the map can only make the verdict stricter (it can add `irreversible: true`, never remove it).

Irreversible controls (pay, submit, send, delete, ...) need a one-time permission that a human
grants from the command line:

    python -m browser_atlas allow "결제하기"            # next click on that name, within 10 minutes

No tool exposed to an agent can create that permission.
"""
from __future__ import annotations

import json
import os
import re
import time

from ._mouse import home

ACTS = ("navigate", "search", "filter", "sort", "paginate", "tab", "expand", "open_detail",
        "download", "input", "login", "submit", "pay", "send", "delete", "apply", "publish")
IRREVERSIBLE_ACTS = {"submit", "pay", "send", "delete", "apply", "publish"}

_WORDS = [
    # Korean
    "결제", "구매", "주문", "송금", "이체", "제출", "신청", "지원하기", "전송", "보내기", "발송", "삭제",
    "탈퇴", "해지", "게시", "발행", "등록하기", "예매", "예약하기", "충전", "환전", "매수", "매도", "동의하고",
    # English
    "pay", "purchase", "buy", "order", "checkout", "check out", "submit", "send", "delete", "remove",
    "unsubscribe", "apply", "publish", "post", "transfer", "book now", "reserve", "place order", "confirm",
]
_PATTERN = re.compile("|".join(re.escape(w) if not w.isascii() else r"\b" + re.escape(w) + r"\b"
                               for w in _WORDS), re.IGNORECASE)
TOKEN_TTL = 600


class NeedsHuman(PermissionError):
    pass


def irreversible(name: str, role: str = "", mapped: dict | None = None) -> bool:
    """True if this control must not be used without a human's permission."""
    if mapped and (mapped.get("irreversible") or mapped.get("act") in IRREVERSIBLE_ACTS):
        return True                                      # the map can only tighten
    if role in ("button", "link", "menuitem", "") and _PATTERN.search(name or ""):
        return True
    return False


def _allow_path():
    return home() / "allow.json"


def _load() -> list[dict]:
    try:
        return json.loads(_allow_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def allow(name: str, ttl: int = TOKEN_TTL) -> dict:
    """Human-side: permit one irreversible action on a control with this name."""
    items = [a for a in _load() if a["expires"] > time.time()]
    item = {"name": " ".join(name.split()), "expires": time.time() + ttl}
    items.append(item)
    p = _allow_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return item


def consume(name: str) -> bool:
    """Use up a matching permission, if one exists and has not expired."""
    name = " ".join((name or "").split())
    items = [a for a in _load() if a["expires"] > time.time()]
    for i, a in enumerate(items):
        if a["name"] == name:
            del items[i]
            _allow_path().write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            return True
    return False


def require(name: str, role: str = "", mapped: dict | None = None) -> None:
    """Raise NeedsHuman for an irreversible control unless a human granted it just now."""
    if irreversible(name, role, mapped) and not consume(name):
        raise NeedsHuman(
            f"'{name}' looks irreversible (pay/submit/send/delete...). Stop and ask the human. "
            f"If they want it done, they run: python -m browser_atlas allow \"{name}\"")


def require_enter(field: str) -> None:
    """Enter in a non-search field submits its form: same rule as an irreversible button."""
    if not consume("Enter"):
        raise NeedsHuman(f"Enter in '{field}' would submit a form. Stop and ask the human. "
                         f"If they want it done, they run: python -m browser_atlas allow Enter")


def port_allowed(port: int) -> bool:
    """BROWSER_ATLAS_DENY_PORTS lists ports whose profiles belong to someone else."""
    deny = {p.strip() for p in os.environ.get("BROWSER_ATLAS_DENY_PORTS", "").split(",") if p.strip()}
    return str(port) not in deny
