import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
NODE = shutil.which("node")


class ExtensionManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    def test_manifest_v3_entry_points_exist(self):
        self.assertEqual(3, self.manifest["manifest_version"])
        entry_points = [
            self.manifest["action"]["default_popup"],
            self.manifest["options_page"],
        ]
        for relative_path in entry_points:
            self.assertTrue((EXTENSION / relative_path).is_file(), relative_path)

    def test_remote_access_is_optional_and_storage_is_required(self):
        self.assertEqual(["storage"], self.manifest["permissions"])
        self.assertEqual(
            {"http://*/*", "https://*/*"},
            set(self.manifest["optional_host_permissions"]),
        )
        self.assertNotIn("host_permissions", self.manifest)

    def test_pages_do_not_use_inline_javascript(self):
        for name in ("popup.html", "options.html"):
            html = (EXTENSION / name).read_text(encoding="utf-8")
            self.assertNotIn("<script>", html)
            self.assertNotIn("onclick=", html.lower())

    def test_shared_client_targets_normalized_usage_api(self):
        source = (EXTENSION / "shared.js").read_text(encoding="utf-8")
        self.assertIn("/api/usage", source)
        self.assertIn("chrome.permissions.request", source)
        self.assertIn("chrome.storage.sync", source)
        self.assertNotIn("innerHTML", source)


class ExtensionBehaviorTests(unittest.TestCase):
    def run_node(self, source):
        self.assertIsNotNone(NODE, "Node.js is required to test extension behavior")
        completed = subprocess.run(
            [NODE, "-"],
            cwd=ROOT,
            input=source,
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        return json.loads(completed.stdout)

    def test_usage_fetch_does_not_race_server_collection_timeout(self):
        result = self.run_node(
            """
const { fetchUsage } = require('./extension/shared.js');
let request;
global.fetch = async (url, options) => {
  request = { url, options };
  return { ok: true, json: async () => ({ providers: [], summary: {} }) };
};
fetchUsage('http://127.0.0.1:8768').then(() => {
  console.log(JSON.stringify({
    url: request.url,
    hasSignal: Object.prototype.hasOwnProperty.call(request.options, 'signal')
  }));
});
"""
        )

        self.assertEqual("http://127.0.0.1:8768/api/usage", result["url"])
        self.assertFalse(result["hasSignal"])

    def test_compact_windows_prioritize_session_and_weekly_quotas(self):
        labels = self.run_node(
            """
const { selectUsageWindows } = require('./extension/shared.js');
const windows = [
  { label: 'model-a' },
  { label: 'session' },
  { label: 'model-b' },
  { label: 'weekly' }
];
console.log(JSON.stringify(selectUsageWindows(windows).map(item => item.label)));
"""
        )

        self.assertEqual(["session", "weekly", "model-a"], labels)

    def test_reset_copy_uses_backend_text_verbatim(self):
        reset_text = self.run_node(
            """
const { resetText } = require('./extension/shared.js');
console.log(JSON.stringify(resetText({ reset_text: 'resets in 2h' })));
"""
        )

        self.assertEqual("resets in 2h", reset_text)

    def test_popup_renders_prioritized_windows_and_verbatim_reset(self):
        rendered = self.run_node(
            """
const fs = require('fs');
const vm = require('vm');
class Element {
  constructor() {
    this.children = [];
    this.className = '';
    this.hidden = false;
    this.style = {};
    this.textContent = '';
  }
  addEventListener() {}
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}
const elements = {};
const context = {
  URL,
  console,
  document: {
    createElement: () => new Element(),
    getElementById: id => elements[id] ||= new Element()
  },
  chrome: {
    permissions: { contains: async () => false, request: async () => false },
    runtime: { openOptionsPage() {} },
    storage: { sync: { get: async defaults => defaults } },
    tabs: { create() {} }
  }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('extension/shared.js', 'utf8'), context);
vm.runInContext(fs.readFileSync('extension/popup.js', 'utf8'), context);
const card = vm.runInContext(`providerNode({
  provider: 'minimax',
  status: 'ok',
  windows: [
    { label: 'model-a', reset_text: 'resets in 1h' },
    { label: 'session', reset_text: 'resets in 2h' },
    { label: 'model-b', reset_text: 'resets in 3h' },
    { label: 'weekly', reset_text: 'resets in 4d' }
  ]
})`, context);
const rows = card.children.slice(1);
console.log(JSON.stringify({
  labels: rows.map(row => row.children[0].children[0].textContent),
  resets: rows.map(row => row.children[2].textContent)
}));
"""
        )

        self.assertEqual(["session", "weekly", "model a"], rendered["labels"])
        self.assertEqual(
            ["resets in 2h", "resets in 4d", "resets in 1h"],
            rendered["resets"],
        )


if __name__ == "__main__":
    unittest.main()
