"""Observed actions over direct CDP (local or via SSH tunnel); one session, no per-step subprocess."""

import base64
import hashlib
import json
import os
import time
from pathlib import Path

from .cdp import cdp, connection

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"
# How long a WAIT gives a quiet page to change; while requests are in flight it waits up to JEV_WAIT_MAX.
WAIT_TIMEOUT = float(os.environ.get("JEV_WAIT_TIMEOUT", "3"))
WAIT_MAX = float(os.environ.get("JEV_WAIT_MAX", "15"))
# A changed page counts as settled once the network has been quiet this long.
SETTLE_S = 0.3
# Counts the page's own fetch/XHR requests still in flight, so WAIT can tell "loading" from "stuck".
TRACK_REQUESTS = """(() => {
  if (window.__jevInflight !== undefined) return;
  window.__jevInflight = 0;
  const done = () => { window.__jevInflight = Math.max(0, window.__jevInflight - 1); };
  const fetch = window.fetch;
  if (fetch) window.fetch = function (...args) {
    window.__jevInflight++;
    return fetch.apply(this, args).finally(done);
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    window.__jevInflight++;
    this.addEventListener('loadend', done, {once: true});
    return send.apply(this, args);
  };
})()"""

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class JavaScriptError(RuntimeError):
    """Caller-supplied JavaScript threw. Unlike StalePage, this is the script's fault, not the page's."""


def viewport():
    width, _, height = os.environ.get("JEV_VIEWPORT", "1120x780").partition("x")
    return int(width), int(height)


class Browser:
    """One page in its own window. ``context`` is a browser context id from ``isolated_context()``; pages in
    the default context share cookies and storage, pages in an isolated one share nothing."""

    def __init__(self, url="about:blank", *, context=None):
        connection()
        # An occluded background tab throttles animation frames to ~2/s on some browsers, which freezes
        # menu and suggestion animations mid-fade. Own a window instead; CDP_WINDOW=0 restores a background tab.
        window = os.environ.get("CDP_WINDOW", "1") != "0"
        extra = {"browserContextId": context} if context else {}
        self.target = cdp(
            "Target.createTarget", url="about:blank", background=not window, newWindow=window, **extra
        )["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        width, height = viewport()
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.addScriptToEvaluateOnNewDocument", source=TRACK_REQUESTS)
        self.goto(url, timeout=15, required=False)

    def goto(self, url, *, timeout=30, required=True):
        """Navigate and wait for the load event's readyState. Raises on timeout unless not required."""
        # Page.navigate returns once the new document has committed, so readyState belongs to it.
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return
            except StalePage:
                pass
            time.sleep(0.02)
        if required:
            raise TimeoutError(f"{url} did not finish loading within {timeout}s")

    @property
    def url(self):
        return self.evaluate("location.href")

    def title(self):
        return self.evaluate("document.title")

    def text(self):
        return self.evaluate("document.body ? document.body.innerText : ''") or ""

    def run(self, function, *args, timeout=30):
        """Call a JavaScript function source with JSON arguments; await it if it returns a promise.

        This is the deterministic escape hatch for steps a test already knows how to address. It is never
        reachable from a model: no model output is ever passed here.
        """
        expression = f"({function})(...{json.dumps(list(args))})"
        response = cdp(
            "Runtime.evaluate", session_id=self.session, timeout=timeout + 5, expression=expression,
            returnByValue=True, awaitPromise=True, userGesture=True,
        )
        if response.get("exceptionDetails"):
            details = response["exceptionDetails"]
            message = details.get("exception", {}).get("description") or details.get("text", "")
            raise JavaScriptError(message.split("\n")[0])
        return response.get("result", {}).get("value")

    def click_at(self, x, y, *, count=1):
        for event in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=count)

    def press(self, key, code=None, key_code=None, text=None):
        """Press one key, e.g. press("Enter", "Enter", 13, "\\r") or press("Escape", "Escape", 27)."""
        params = {"key": key, "code": code or key}
        if key_code:
            params["windowsVirtualKeyCode"] = key_code
        self.call("Input.dispatchKeyEvent", type="keyDown", **params, **({"text": text} if text else {}))
        self.call("Input.dispatchKeyEvent", type="keyUp", **params)

    def insert_text(self, text):
        self.call("Input.insertText", text=text)

    def screenshot(self, path=None):
        data = self.call("Page.captureScreenshot", format="png")["data"]
        if path:
            Path(path).write_bytes(base64.b64decode(data))
        return data

    def opened_targets(self):
        """Pages this page opened (window.open, target=_blank), which a test should not leave behind."""
        return [t for t in cdp("Target.getTargets")["targetInfos"] if t.get("openerId") == self.target]

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            self.wait_for_change(page)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def busy(self):
        try:
            return bool(self.evaluate("window.__jevInflight || 0"))
        except StalePage:
            return True

    def wait_for_change(self, page):
        """Wait for the page to change and then settle, not a fixed tick: a slow login or a loading spinner
        otherwise burns one model call per tick and looks like a stuck page, and the policy wanders off.

        Returns once the page has changed and the network has been quiet for SETTLE_S; or, if nothing
        changes, after WAIT_TIMEOUT of quiet. While requests are in flight it keeps waiting, up to WAIT_MAX.
        """
        started = time.monotonic()
        quiet_since = started
        while time.monotonic() - started < WAIT_MAX:
            time.sleep(0.05)
            now = time.monotonic()
            if self.busy():
                quiet_since = None
                continue
            quiet_since = quiet_since or now
            quiet = now - quiet_since
            try:
                changed = not self.fresh(page)
            except StalePage:
                changed = True
            if (changed and quiet >= SETTLE_S) or quiet >= WAIT_TIMEOUT:
                return

    def close(self):
        if self.target:
            for opened in self.opened_targets():
                cdp("Target.closeTarget", targetId=opened["targetId"])
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def isolated_context():
    """A fresh browser context: no cookies, storage or cache shared with any other page."""
    return cdp("Target.createBrowserContext", disposeOnDetach=False)["browserContextId"]


def dispose_context(context):
    cdp("Target.disposeBrowserContext", browserContextId=context)


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    # Select-all follows the browser's OS, which may differ from this machine's.
                    select_all = 4 if "Macintosh" in connection().user_agent else 2
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=select_all,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=select_all,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
