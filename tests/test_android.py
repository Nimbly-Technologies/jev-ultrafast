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
