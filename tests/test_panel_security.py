"""The panel renders server data with innerHTML: prove hostile values stay inert.

`node --check` only proves the script parses. This runs the real `render()` under a DOM
shim with a poisoned state (an accounts.json/usage row can be hand-edited or restored
from a backup) and inspects what actually reached innerHTML.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from step2api.dashboard import DASHBOARD_HTML

SHIM = """
const captured = {};
function makeEl(id) {
  const el = {
    id, _html: "", textContent: "", value: "", className: "", style: {}, dataset: {},
    classList: {add(){}, remove(){}, toggle(){}, contains(){return false}},
    addEventListener(){}, appendChild(){}, querySelector(){return null;},
    querySelectorAll(){return [];}, onclick: null,
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return this._html; },
    set(v) { this._html = v; captured[id] = v; },
  });
  return el;
}
const els = {};
global.document = {
  getElementById: (id) => els[id] || (els[id] = makeEl(id)),
  addEventListener(){}, createElement: (t) => makeEl("el-" + t),
  querySelector(){return null;}, querySelectorAll(){return [];}, body: makeEl("body"),
};
global.sessionStorage = {getItem(){return "";}, setItem(){}, removeItem(){}};
global.localStorage = {getItem(){return null;}, setItem(){}, removeItem(){}};
global.window = global;
global.location = {href: "http://127.0.0.1:8790/", origin: "http://127.0.0.1:8790"};
global.fetch = () => Promise.reject(new Error("offline"));
global.alert = () => {};
global.confirm = () => false;
// The panel starts a 15 s auto-refresh timer; without this Node never exits.
global.setInterval = () => 0;
global.clearInterval = () => {};
"""

PAYLOAD = {
    "mode": "playground",
    "password_default": True,
    "settings": {"default_mode": "playground"},
    "summary": {"requests": 1, "tokens_in": 1, "tokens_out": 1, "cached_tokens": 0,
                "errors": 0},
    "quota": {"free_rate": 0.5, "five_hour_usage_left_rate": 0.9},
    "accounts": [{
        "id": 'a1" onmouseover="alert(1)',
        "name": "<img src=x onerror=alert(2)>",
        "requests": "<img src=x onerror=alert(3)>",
        "tokens_in": "<img src=x onerror=alert(4)>",
        "tokens_out": "0",
        "cached_tokens": "0",
        "last_error": '"><svg onload=alert(5)>',
        "disabled": False,
        "cooling": False,
        "token_exp": 0, "refresh_exp": 0,
    }],
    "api_keys": [{"key": "sf-1", "name": "<img src=x onerror=alert(6)>", "enabled": True}],
    "models": [{"id": "<img src=x onerror=alert(7)>", "display_name": "x"}],
    "model_error": '<img src=x onerror=alert(8)>',
    "usage": [{
        "ts": "<img src=x onerror=alert(9)>",
        "status": "ok",
        "account": "<img src=x onerror=alert(10)>",
        "model": "<img src=x onerror=alert(11)>",
        "mode": "<img src=x onerror=alert(12)>",
        "tokens_in": "<img src=x onerror=alert(13)>",
        "tokens_out": "1",
        "ttft": "<img src=x onerror=alert(14)>",
        "duration": "<img src=x onerror=alert(15)>",
        "client": "<img src=x onerror=alert(16)>",
        "tenant": "<img src=x onerror=alert(17)>",
        "finish_reason": "<img src=x onerror=alert(18)>",
        "error": "<img src=x onerror=alert(19)>",
    }],
}


class PanelEscapingTest(unittest.TestCase):
    def test_hostile_state_never_reaches_innerhtml_as_markup(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        js = chr(10).join(re.findall(r"<script>(.*?)</script>", DASHBOARD_HTML, re.S))
        harness = (SHIM + js + chr(10) +
                   "state = " + json.dumps(PAYLOAD) + ";" + chr(10) +
                   "render();" + chr(10) +
                   "console.log(JSON.stringify(captured));" + chr(10))
        tmp = tempfile.mkdtemp(prefix="step2api-xss-")
        path = os.path.join(tmp, "harness.js")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(harness)
            # Pin the encoding: Node writes UTF-8, while `text=True` alone decodes with the
            # process locale (GBK on a zh-CN Windows box) and crashed on the panel's Chinese
            # labels before the assertion ever ran.
            proc = subprocess.run([node, path], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(proc.returncode, 0, (proc.stderr or "")[-2000:])
        lines = (proc.stdout or "").strip().splitlines()
        self.assertTrue(lines, "node rendered nothing (stderr: %s)" % (proc.stderr or "")[-500:])
        rendered = json.loads(lines[-1])
        blob = chr(10).join(rendered.values())
        # The payload did flow through the renderer (otherwise the test is vacuous) ...
        self.assertIn("&lt;img", blob)
        # ... but only ever as text: no tag, no injected attribute.
        self.assertNotRegex(blob, r"(?i)<img\b")
        self.assertNotIn('onmouseover="', blob)
        self.assertNotIn("<svg", blob)


if __name__ == "__main__":
    unittest.main()
