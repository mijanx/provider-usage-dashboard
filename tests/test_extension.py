import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"


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


if __name__ == "__main__":
    unittest.main()
