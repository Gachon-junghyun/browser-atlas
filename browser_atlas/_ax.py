"""Find controls by their accessibility role and name."""
from __future__ import annotations

import secrets

INTERACTIVE = frozenset(("button", "link", "textbox", "searchbox", "combobox", "checkbox",
                         "radio", "tab", "menuitem", "option", "switch", "slider",
                         "spinbutton", "listbox"))


def snapshot(cdp, roles=INTERACTIVE) -> list[dict]:
    cdp.call("Accessibility.enable")
    nodes = cdp.call("Accessibility.getFullAXTree")["nodes"]
    return [{"role": n["role"]["value"], "name": n.get("name", {}).get("value", ""),
             "backend": n.get("backendDOMNodeId")} for n in nodes
            if not n.get("ignored") and n.get("role", {}).get("value") in roles]


def find(cdp, role, name, exact=True, nodes=None) -> dict | None:
    name = " ".join(name.split())
    matches = []
    for node in snapshot(cdp) if nodes is None else nodes:
        actual = " ".join(node["name"].split())
        if node["role"] == role and (actual == name if exact else name.casefold() in actual.casefold()):
            matches.append(node)
    for node in matches:
        try:
            cdp.call("DOM.getBoxModel", {"backendNodeId": node["backend"]})
            return node
        except RuntimeError:
            pass
    return matches[0] if matches else None


def mark(cdp, backend) -> str:
    cdp.call("DOM.getDocument")
    node = cdp.call("DOM.pushNodesByBackendIdsToFrontend", {"backendNodeIds": [backend]})["nodeIds"][0]
    token = secrets.token_hex(12)
    cdp.call("DOM.setAttributeValue", {"nodeId": node, "name": "data-atlas-target", "value": token})
    return f'[data-atlas-target="{token}"]'
