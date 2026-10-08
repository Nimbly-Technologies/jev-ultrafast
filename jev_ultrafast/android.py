"""An Android screen as a jev page: `Agent(browser=AndroidDevice(serial))` drives a phone with the same policy.

The accessibility hierarchy becomes the same indexed action space the browser observer produces, so the model
still only picks an operation and a numbered target, never a coordinate. Reads and input go through the
uiautomator2 on-device server (a few hundred ms per screen) rather than `adb shell uiautomator dump` (1-2 s).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import xml.etree.ElementTree as ET

from .browser import StalePage

SETTLE_S = 0.1
FRESH_S = 0.5
WAIT_TIMEOUT_S = 3.0
EDITABLE = ("EditText", "AutoCompleteTextView")


def bounds(node):
    left, top, right, bottom = (int(n) for n in node.get("bounds", "[0,0][0,0]").replace("][", ",")[1:-1].split(","))
    return left, top, right, bottom


def own_label(node):
    return " ".join(node.get(k, "").strip() for k in ("text", "content-desc") if node.get(k, "").strip())


def label_for(node):
    """Own text, else the visible text of its descendants: React Native puts a button's words in child views."""
    label = own_label(node) or " ".join(filter(None, (own_label(n) for n in node.iter() if n is not node)))
    if not label:
        label = node.get("hint", "") or node.get("resource-id", "").rpartition("/")[2].replace("_", " ")
    return " ".join(label.split())[:120] or "[unlabeled control]"


def parse(xml, width, height):
    """Visible interactive nodes as jev actions, plus the screen's text, in document order."""
    root = ET.fromstring(xml)
    actions, texts, package, scrollable, heading = [], [], "", None, ""
    for index, node in enumerate(root.iter("node")):
        if node.get("package") == "com.android.systemui":
            continue  # status bar and notification shade, not the app
        package = package or node.get("package", "")
        left, top, right, bottom = bounds(node)
        if right <= max(left, 0) or bottom <= max(top, 0) or left >= width or top >= height:
            continue
        if own_label(node):
            texts.append(own_label(node))
            # ponytail: the screen's name is its first text in the top fifth (the toolbar); good enough for
            # Settings and React Native headers, wrong on a screen with no header.
            if (not heading and bottom <= height // 5 and node.get("clickable") != "true"
                    and node.get("class", "").endswith("TextView") and node.get("text", "").strip()):
                heading = node.get("text").strip()
        if node.get("scrollable") == "true" and scrollable is None:
            scrollable = (left, top, right, bottom)
        cls = node.get("class", "")
        editable = cls.endswith(EDITABLE)
        if node.get("enabled") != "true" or not (editable or node.get("clickable") == "true"
                                                 or node.get("checkable") == "true"):
            continue
        base = {
            "node": index,
            "label": label_for(node),
            "role": cls.rpartition(".")[2],
            "value": node.get("text", "") if editable and node.get("password") != "true" else "",
            "bounds": [left, top, right, bottom],
        }
        if node.get("checkable") == "true":
            base["checked"] = node.get("checked") == "true"
        if editable:
            secret = node.get("password") == "true"
            # A field is named by its hint, not by what is typed in it; the vault is keyed on that label too.
            base["label"] = node.get("hint") or base["label"]
            actions.append({**base, "id": f"f{index}", "kind": "fill", **({"secret": True} if secret else {})})
        else:
            actions.append({**base, "id": f"c{index}", "kind": "click"})
    if scrollable:
        actions.append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "area": scrollable, "dy": 1})
        actions.append({"id": "scroll_up", "kind": "scroll", "label": "Scroll up", "area": scrollable, "dy": -1})
    actions.append({"id": "back", "kind": "back", "label": "Press the Android back button"})
    actions.append({"id": "wait", "kind": "wait", "label": "Wait for the screen to update"})
    return package, heading, "\n".join(dict.fromkeys(texts)), actions


def fingerprint(page):
    # Bounds are left out so an animation or a blinking cursor does not read as a new screen.
    stable = [{k: a.get(k) for k in ("kind", "label", "role", "value", "checked")} for a in page["actions"]]
    return hashlib.sha256(json.dumps([page["url"], page["text"], stable]).encode()).hexdigest()


class AndroidDevice:
    def __init__(self, serial=None, package=None):
        import uiautomator2

        self.d = uiautomator2.connect(serial)
        if package:
            self.d.app_start(package, wait=True)
        self.width, self.height = self.d.window_size()
        self.acted, self.last = False, (None, 0.0)

    def read(self, screenshot=False):
        package, heading, text, actions = parse(self.d.dump_hierarchy(compressed=True), self.width, self.height)
        # The heading stands in for a path, so the model and the revisit diagnostics can tell screens apart.
        page = {"url": f"android://{package}/{heading}", "title": heading or package, "text": text, "actions": actions}
        page["fingerprint"] = page["marker"] = fingerprint(page)
        self.last = (page["fingerprint"], time.monotonic())
        if screenshot:
            page["screenshot"] = base64.b64encode(self.d.screenshot(format="raw")).decode()
        return page

    def observe(self, screenshot=False):
        page = self.read()
        if self.acted:
            # After input, read until two consecutive screens agree, so a half-drawn transition is not chosen from.
            self.acted = False
            deadline = time.monotonic() + WAIT_TIMEOUT_S
            while time.monotonic() < deadline:
                time.sleep(SETTLE_S)
                again = self.read()
                if again["fingerprint"] == page["fingerprint"]:
                    break
                page = again
        if screenshot:
            page["screenshot"] = base64.b64encode(self.d.screenshot(format="raw")).decode()
        return page

    def fresh(self, page, action=None):
        # A screen read a moment ago is fresh unless input is about to land on it: then always look again.
        if action is None and self.last[0] == page["fingerprint"] and time.monotonic() - self.last[1] < FRESH_S:
            return True
        return self.read()["fingerprint"] == page["fingerprint"]

    def wait_for_change(self, page):
        deadline = time.monotonic() + WAIT_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(SETTLE_S)
            if self.read()["fingerprint"] != page["fingerprint"]:
                self.acted = True
                return

    def act(self, action, page, text=None):
        kind = action["kind"]
        if kind == "wait":
            return self.wait_for_change(page)
        if kind in {"click", "fill"} and not self.fresh(page, action):
            raise StalePage("Screen changed since this decision. Observe again.")
        if kind == "back":
            self.d.press("back")
        elif kind == "scroll":
            left, top, right, bottom = action["area"]
            x, span = (left + right) // 2, (bottom - top) * 0.3
            mid = (top + bottom) / 2
            self.d.swipe(x, mid + span * action["dy"], x, mid - span * action["dy"], duration=0.15)
        else:
            left, top, right, bottom = action["bounds"]
            self.d.click((left + right) // 2, (top + bottom) // 2)
            if kind == "fill":
                self.d.send_keys(text or "", clear=True)
        self.acted = True

    def text(self):
        return self.read()["text"]

    def close(self):
        pass
