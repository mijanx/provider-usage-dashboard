import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("provider_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
sys.modules["provider_app"] = app
spec.loader.exec_module(app)


class ConfigTests(unittest.TestCase):
    def test_parse_simple_yaml_expands_types(self):
        data = app.parse_simple_yaml("""port: 9999
flag: true
name: demo
""")
        self.assertEqual(data, {"port": 9999, "flag": True, "name": "demo"})

    def test_load_config_expands_user_and_env(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("PUD_TMP")
            os.environ["PUD_TMP"] = td
            try:
                cfg_path = Path(td) / "config.yaml"
                cfg_path.write_text("""auth_path: $PUD_TMP/auth.json
cache_path: ~/provider-cache.json
port: 9876
""")
                cfg = app.load_config(str(cfg_path))
                self.assertEqual(cfg.auth_path, str(Path(td) / "auth.json"))
                self.assertTrue(cfg.cache_path.endswith("provider-cache.json"))
                self.assertEqual(cfg.port, 9876)
            finally:
                if old is None:
                    os.environ.pop("PUD_TMP", None)
                else:
                    os.environ["PUD_TMP"] = old


if __name__ == "__main__":
    unittest.main()
