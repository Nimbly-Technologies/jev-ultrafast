"""Local-browser freshness/execution regressions. No model calls or external websites."""

import json
import threading
import time
from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage, wait_limits
from jev_ultrafast.cdp import drain_events

HTML = """<!doctype html><title>Guard checks</title>
<style>body{margin:30px}button{width:180px;height:50px}#outside{position:absolute;top:3000px}</style>
<p id="context">Cart total: $10</p>
<button id="target" onclick="window.clicks=(window.clicks||0)+1">Continue</button>
<label>City<input id="field" value="Zurich"></label>
<label><input id="toggle" type="checkbox">Refundable</label>
<select aria-label="Category"><option>All</option><option>Design</option></select>
<label>Password<input id="pw" type="password"></label>
<div id="card" style="cursor:pointer" onclick="window.cards=(window.cards||0)+1"><span>Issue Insights</span>
<span>Spot trends</span></div>
<nav><div id="menu" style="cursor:pointer;position:relative;width:200px">User Management
<div style="position:absolute;top:24px;left:0;width:260px;background:#eee">
<div onclick="window.item='Users'"><div><b>Users</b> Add and edit users</div></div>
<div onclick="window.item='User Groups'"><div><b>User Groups</b> Manage groups</div></div></div></div></nav>
<div id="react" style="cursor:auto">Advanced Configurations</div>
<p id="outside">Unrelated offscreen text</p>"""


def main():
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        browser.evaluate("document.querySelector('#pw').value='hunter2'")
        page = browser.observe(screenshot=False)
        password = next(a for a in page["actions"] if a["label"] == "Password" and a["kind"] == "fill")
        assert password.get("secret") is True and password["role"] == "textbox" and password["value"] == "(filled)"
        assert "hunter2" not in json.dumps(page), "a password value left the page"
        passed.append("a password field is a masked, secret textbox and its value never leaves the page")
        action = next(a for a in page["actions"] if a["label"] == "Continue")
        browser.evaluate("document.querySelector('#target').style.transform='translateX(200px)'")
        assert browser.fresh(page), "Movement should use fresh geometry, not another model call"
        browser.act(action, page)
        assert browser.evaluate("window.clicks") == 1
        passed.append("moving target clicked at its current location")

        cards = [a for a in page["actions"] if "Issue Insights" in a["label"]]
        assert len(cards) == 1 and cards[0]["role"] == "button", cards
        browser.act(cards[0], page)
        assert browser.evaluate("window.cards") == 1
        passed.append("a scripted pointer-cursor card is one clickable element")
        page = browser.observe(screenshot=False)
        items = [a for a in page["actions"] if a["label"].startswith("User Groups")]
        assert len(items) == 1, [a["label"] for a in page["actions"]]
        browser.act(items[0], page)
        assert browser.evaluate("window.item") == "User Groups"
        passed.append("items of a scripted menu nested in its trigger are separate clickable elements")
        browser.evaluate("document.querySelector('#react').onclick=()=>{window.expanded=1}")
        page = browser.observe(screenshot=False)
        header = next(a for a in page["actions"] if a["label"] == "Advanced Configurations")
        browser.act(header, page)
        assert browser.evaluate("window.expanded") == 1
        passed.append("an element with an onclick handler but no pointer cursor is clickable")
        browser.evaluate("""const box=document.createElement('div');
          box.style.cssText='position:relative;width:220px;height:36px;border:1px solid';
          box.innerHTML='<span style=\"position:absolute;inset:0\">Select...</span>'+
            '<input aria-label=\"Timezone\" style=\"width:2px;opacity:1;border:0;padding:0\">';
          box.addEventListener('mousedown',()=>{window.opened=1});
          document.body.prepend(box)""")
        page = browser.observe(screenshot=False)
        tiny = next(a for a in page["actions"] if a["label"] == "Open Timezone")
        browser.act(tiny, page)
        assert browser.evaluate("window.opened") == 1
        passed.append("a pixel-wide input is pressed through its visible container")
        page = browser.observe(screenshot=False)

        browser.evaluate("document.querySelector('#outside').textContent='Updated outside the viewport'")
        assert browser.fresh(page)
        passed.append("unrelated offscreen text does not invalidate")

        mutations = {
            "visible context": "document.querySelector('#context').textContent='Cart total: $100'",
            "accessible label": "document.querySelector('#target').setAttribute('aria-label','Delete account')",
            "field property": "document.querySelector('#field').value='London'",
            "checkbox property": "document.querySelector('#toggle').checked=true",
            "disabled target": "document.querySelector('#target').disabled=true",
            "read-only field": "document.querySelector('#field').readOnly=true",
            "hidden target": "document.querySelector('#target').style.display='none'",
            "replaced node": "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
            "dropdown option": "document.querySelector('select').options[1].text='Coastal'",
        }
        for label, expression in mutations.items():
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe(screenshot=False)
            browser.evaluate(expression)
            assert not browser.fresh(page), label
            passed.append(label + " invalidates")

        browser.evaluate("document.querySelector('#target').disabled=false; "
                         "document.querySelector('#target').style.display='block'")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Delete account")
        # A textless overlay does not alter the model's semantic state, but must block a click.
        browser.evaluate("const cover=document.createElement('div'); "
                         "cover.style.cssText='position:fixed;inset:0;z-index:9999;background:white'; "
                         "document.body.append(cover)")
        assert browser.fresh(page)
        try:
            browser.act(action, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("Covered target was clicked")
        assert browser.evaluate("window.clicks") == 1
        passed.append("overlay blocked before input")

        browser.evaluate("document.body.innerHTML=" + repr("""
          <form><p id="price">Total $10</p>
          <button type="button" id="buy">Buy</button>
          <label>Search <input id="query" role="combobox" aria-controls="suggestions"></label>
          <div role="listbox" id="suggestions"></div>
          <label><input id="check" type="checkbox">Enabled</label>
          <label><input id="radio" type="radio">Choice</label>
          <input id="readonly" aria-label="Read only" readonly>
          <input id="secret" type="password" value="never expose this">
          <button id="off" disabled>Disabled</button>
          <select id="category" aria-label="Category">
            <option>All</option><option>Design</option><option disabled>Unavailable</option>
          </select></form><aside id="unrelated">News</aside>
        """))
        page = browser.observe(screenshot=False)
        buy = next(a for a in page["actions"] if a["label"] == "Buy")
        browser.evaluate("document.querySelector('#unrelated').textContent='New unrelated news'")
        assert browser.fresh(page, buy)
        assert not browser.fresh(page)
        passed.append("click guard accepts unrelated visible updates; terminal guard rejects them")
        for label, expression in {
            "nearby price": "document.querySelector('#price').textContent='Total $100'",
            "form value": "document.querySelector('#query').value='changed'",
            "form toggle": "document.querySelector('#check').checked=true",
            "target replacement": "document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML",
        }.items():
            page = browser.observe(screenshot=False)
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            browser.evaluate(expression)
            assert not browser.fresh(page, buy), label
            passed.append(label + " invalidates action-specific guard")

        page = browser.observe(screenshot=False)
        actions = page["actions"]
        for role in ("checkbox", "radio"):
            assert {a["kind"] for a in actions if a.get("role") == role} == {"click"}
        assert {a["kind"] for a in actions if a["label"] == "Read only"} == {"click"}
        assert not any(a["label"] == "Disabled" or a.get("value") == "never expose this" for a in actions)
        assert [a["value"] for a in actions if a["kind"] == "select"] == ["Design"]
        passed.append("native controls expose only supported operations and safe values")

        select = next(a for a in actions if a["kind"] == "select")
        browser.act(select, page)
        assert browser.evaluate("document.querySelector('#category').value") == "Design"
        passed.append("native dropdown selects an observed option")

        browser.evaluate("document.querySelector('#query').addEventListener('input',()=>setTimeout(()=>{"
                         "document.querySelector('#suggestions').innerHTML='<div role=option>Generated</div>'"
                         "},60))")
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a["kind"] == "fill" and not a.get("secret"))
        browser.act(field, page, text="Generated")
        page = browser.observe(screenshot=False)
        value = browser.evaluate("document.querySelector('#query').value")
        assert value == "Generated", repr(value)
        assert any(a.get("role") == "option" for a in page["actions"])
        passed.append("real text input waits for asynchronous combobox suggestions")
        # A real request, held by the browser for 1.2 s (no server needed): WAIT must outlast it, then see the
        # change that follows it. Fetch interception pauses the request inside Chrome's network stack.
        browser.call("Fetch.enable", patterns=[{"urlPattern": "*jev-guard.invalid*", "requestStage": "Request"}])
        drain_events()
        page = browser.observe(screenshot=False)
        # The page changes at once ("Loading...") while the request is still in flight, as real pages do: only the
        # in-flight counter keeps WAIT from returning on that first change.
        browser.evaluate("const p=document.createElement('p'); p.textContent='Loading...'; document.body.prepend(p);"
                         "fetch('https://jev-guard.invalid/slow', {mode: 'no-cors'})"
                         ".finally(() => { p.textContent='Loaded' })")

        def release():
            request, deadline = None, time.monotonic() + 5
            while request is None and time.monotonic() < deadline:
                request = next((e["params"]["requestId"] for e in drain_events()
                                if e["method"] == "Fetch.requestPaused"), None)
                time.sleep(0.02)
            time.sleep(1.2)
            browser.call("Fetch.fulfillRequest", requestId=request, responseCode=200, body="b2s=")

        releaser = threading.Thread(target=release)
        releaser.start()
        started = time.monotonic()
        browser.wait_for_change(page)
        elapsed = time.monotonic() - started
        releaser.join()
        browser.call("Fetch.disable")
        quiet_timeout, _ = wait_limits()
        # The upper bound matters: with a dead counter WAIT would still return, but only at the quiet timeout.
        assert 1.2 <= elapsed < min(quiet_timeout, 3), elapsed
        assert "Loaded" in browser.observe(screenshot=False)["text"]
        passed.append("WAIT outlasts a real in-flight request and returns as soon as the page settles")
        browser.evaluate("try { new XMLHttpRequest().send() } catch (_) {}")
        assert browser.evaluate("window.__jevInflight") == 0
        passed.append("a request that throws before dispatch does not leave WAIT believing the page is busy")
        browser.evaluate("dispatchEvent(new Event('beforeunload'))")
        assert browser.busy()
        passed.append("a navigation under way counts as busy")
        # The page scrolls inside a container, not the window; a select-style combobox shows its choice beside
        # an empty input.
        browser.evaluate("""document.body.innerHTML='<div id=\"pane\" style=\"height:600px;overflow-y:auto\">'+
          '<div class=\"control\"><span>Daily Essentials</span><input role=\"combobox\" aria-label=\"Site\"></div>'+
          '<div style=\"height:1500px\"></div><button onclick=\"window.deep=1\">Deep button</button></div>';
          document.body.style.height='700px'; document.documentElement.style.overflow='hidden'""")
        page = browser.observe(screenshot=False)
        site = next(a for a in page["actions"] if a["label"] == "Site" and a["kind"] == "fill")
        assert site["value"] == "Daily Essentials", site
        passed.append("a select-style combobox reports the choice it displays")
        deep = next(a for a in page["actions"] if a["label"] == "Deep button")
        assert deep.get("offscreen") is True and any(a["id"] == "scroll_down" for a in page["actions"])
        browser.act(deep, page)
        assert browser.evaluate("window.deep") == 1
        page = browser.observe(screenshot=False)
        site = next(a for a in page["actions"] if a["label"] == "Open Site")
        assert site.get("offscreen") is True, site  # Above the fold of the scrolled container.
        passed.append("a control below the fold of a scrolling container is offered and scrolled into view")
        browser.call("Page.navigate", url="about:blank")
        assert not browser.fresh(page, field)
        passed.append("navigation invalidates the old document")
    finally:
        browser.close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} browser guard checks; no model calls")


if __name__ == "__main__":
    main()
