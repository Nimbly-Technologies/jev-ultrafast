from jev_ultrafast.android import fingerprint, parse
from jev_ultrafast.model import action_space

XML = """<hierarchy rotation="0">
<node index="0" class="android.widget.FrameLayout" package="com.example" bounds="[0,0][1080,2400]" enabled="true">
  <node class="android.widget.EditText" package="com.example" text="me@example.com" hint="Email"
        bounds="[40,400][1040,520]" enabled="true" clickable="true" password="false"/>
  <node class="android.widget.EditText" package="com.example" text="••••" hint="Password"
        bounds="[40,560][1040,680]" enabled="true" clickable="true" password="true"/>
  <node class="android.view.ViewGroup" bounds="[40,720][1040,840]" enabled="true" clickable="true">
    <node class="android.widget.TextView" text="Sign in" bounds="[400,760][680,800]" enabled="true"/>
  </node>
  <node class="android.widget.Button" text="Hidden" bounds="[0,2600][100,2700]" enabled="true" clickable="true"/>
  <node class="android.widget.Button" text="Off" bounds="[0,900][100,1000]" enabled="false" clickable="true"/>
</node>
</hierarchy>"""


def test_parse_builds_jev_action_space():
    package, heading, text, actions = parse(XML, 1080, 2400)
    assert package == "com.example"
    by_label = {a["label"]: a for a in actions}
    assert by_label["Email"]["kind"] == "fill" and by_label["Email"]["value"] == "me@example.com"
    assert by_label["Password"] == {**by_label["Password"], "kind": "fill", "secret": True, "value": ""}
    assert by_label["Sign in"]["kind"] == "click"  # label comes from the child text view
    assert "Hidden" not in by_label and "Off" not in by_label  # off-screen and disabled are not offered
    assert "••••" not in [a.get("value") for a in actions]
    elements, targets, controls = action_space(actions)
    assert set(targets) == {"CLICK", "TYPE_TEXT"} and {"BACK", "WAIT"} <= set(controls)


def test_fingerprint_ignores_bounds():
    _, _, text, actions = parse(XML, 1080, 2400)
    moved = [{**a, "bounds": [0, 0, 1, 1]} if "bounds" in a else a for a in actions]
    page = {"url": "android://com.example", "text": text}
    assert fingerprint({**page, "actions": actions}) == fingerprint({**page, "actions": moved})


class FakeField:
    def __init__(self, calls, exists):
        self.calls, self.exists = calls, exists

    def set_text(self, text):
        self.calls.append(("set_text", text))


class FakeD:
    # Records input calls; the jsonrpc key/swipe injectors are absent so using them fails the test.
    def __init__(self, focused):
        self.calls, self.focused = [], focused

    def __call__(self, focused):
        return FakeField(self.calls, self.focused)

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def shell(self, cmd):
        self.calls.append(("shell", cmd))

    def send_keys(self, text, clear=False):
        self.calls.append(("send_keys", text, clear))


def device(focused=True):
    from jev_ultrafast.android import AndroidDevice

    phone = AndroidDevice.__new__(AndroidDevice)
    phone.d, phone.acted, phone.fresh = FakeD(focused), False, lambda page, action=None: True
    return phone


def test_fill_sets_text_on_focused_field():
    # Android 16 removed InputManager.getInstance, which send_keys(clear=True) needs.
    phone = device()
    phone.act({"kind": "fill", "bounds": [0, 0, 100, 50]}, {}, "hi")
    assert phone.d.calls == [("click", 50, 25), ("set_text", "hi")]
    phone = device(focused=False)
    phone.act({"kind": "fill", "bounds": [0, 0, 100, 50]}, {}, "hi")
    assert phone.d.calls[-1] == ("send_keys", "hi", True)


def test_scroll_uses_input_swipe():
    phone = device()
    phone.act({"kind": "scroll", "area": [0, 0, 1000, 1000], "dy": 1}, {})
    assert phone.d.calls == [("shell", "input swipe 500 800 500 200 150")]
