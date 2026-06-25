import importlib.util
import json
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
    def test_default_host_is_loopback(self):
        self.assertEqual(app.DEFAULT_HOST, "127.0.0.1")
        self.assertEqual(app.AppConfig().host, "127.0.0.1")

    def test_parse_simple_yaml_expands_types(self):
        data = app.parse_simple_yaml("""port: 9999
flag: true
name: demo
""")
        self.assertEqual(data, {"port": 9999, "flag": True, "name": "demo"})

    def test_usage_payload_does_not_expose_auth_path(self):
        service = app.UsageService(app.AppConfig(auth_path="/tmp/private-auth.json"))

        def fake_probe(provider, fn):
            return app.ProviderResult(provider=provider, status="error", source="test")

        old_safe_probe = service._safe_probe
        try:
            service._safe_probe = fake_probe
            payload = service.collect_all()
        finally:
            service._safe_probe = old_safe_probe

        self.assertNotIn("auth_path", payload)
        self.assertEqual(payload["host"], "127.0.0.1")

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

    def test_require_access_token_can_resolve_env_sourced_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = app.AppConfig(auth_path=str(Path(td) / "auth.json"))
            service = app.UsageService(cfg)
            env_old = os.environ.get("PUD_TEST_TOKEN")
            os.environ.pop("PUD_TEST_TOKEN", None)
            Path(td, ".env").write_text("PUD_TEST_TOKEN=from-dotenv\n", encoding="utf-8")
            try:
                token = service._require_access_token({"source": "env:PUD_TEST_TOKEN"}, "minimax")
                self.assertEqual(token, "from-dotenv")
            finally:
                if env_old is None:
                    os.environ.pop("PUD_TEST_TOKEN", None)
                else:
                    os.environ["PUD_TEST_TOKEN"] = env_old

    def test_minimax_probe_accepts_percent_only_rows(self):
        with tempfile.TemporaryDirectory() as td:
            auth_path = Path(td) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "minimax": [
                                {"id": "minimax-env", "source": "env:PUD_MINIMAX_TEST_TOKEN"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            cfg = app.AppConfig(auth_path=str(auth_path), cache_path=str(Path(td) / "cache.json"))
            service = app.UsageService(cfg)
            env_old = os.environ.get("PUD_MINIMAX_TEST_TOKEN")
            os.environ["PUD_MINIMAX_TEST_TOKEN"] = "token"
            old_http_json = app.http_json
            seen = {}

            def fake_http_json(url, **kwargs):
                seen["url"] = url
                return {
                    "base_resp": {"status_code": 0},
                    "model_remains": [
                        {
                            "model_name": "general",
                            "current_interval_remaining_percent": 42.5,
                            "current_interval_status": "available",
                            "current_weekly_remaining_percent": 80,
                            "current_weekly_status": "available",
                        }
                    ],
                }

            try:
                app.http_json = fake_http_json
                result = service.probe_minimax()
            finally:
                app.http_json = old_http_json
                if env_old is None:
                    os.environ.pop("PUD_MINIMAX_TEST_TOKEN", None)
                else:
                    os.environ["PUD_MINIMAX_TEST_TOKEN"] = env_old

            self.assertEqual(seen["url"], "https://api.minimax.io/v1/token_plan/remains")
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.windows[0].remaining_text, "42.5% left")
            self.assertEqual(result.windows[-1].label, "weekly")
            self.assertEqual(result.windows[-1].remaining_text, "80.0% left")

    def test_claude_statusline_weekly_does_not_override_api_aggregate(self):
        service = app.UsageService(app.AppConfig(auth_path="/tmp/nonexistent-auth.json"))
        future = (app.datetime.now(app.timezone.utc) + app.timedelta(days=3)).isoformat()
        past = (app.datetime.now(app.timezone.utc) - app.timedelta(hours=1)).isoformat()
        api_windows = [
            app.Window(label="session", percent_remaining=100.0, percent_used=0.0),
            app.Window(label="weekly", percent_remaining=1.0, percent_used=99.0),
            app.Window(label="weekly-sonnet", percent_remaining=53.0, percent_used=47.0),
        ]
        statusline_windows = [
            # stale 5h/statusline sessions must not override the API value
            app.Window(label="session", percent_remaining=88.0, percent_used=12.0, reset_at=past),
            # live Claude Code statusline weekly can disagree with Anthropic account usage
            app.Window(label="weekly", percent_remaining=91.0, percent_used=9.0, reset_at=future),
        ]

        merged = service._merge_claude_statusline_windows(api_windows, statusline_windows)

        self.assertEqual([window.label for window in merged], ["session", "weekly", "weekly-sonnet", "weekly-statusline"])
        self.assertEqual(merged[0].percent_used, 0.0)
        self.assertEqual(merged[1].percent_used, 99.0)
        self.assertEqual(merged[2].percent_used, 47.0)
        self.assertEqual(merged[3].percent_used, 9.0)
        self.assertEqual(merged[3].meta["source"], "claude-code-statusline")

    def test_claude_current_statusline_session_can_override_api_session(self):
        service = app.UsageService(app.AppConfig(auth_path="/tmp/nonexistent-auth.json"))
        future = (app.datetime.now(app.timezone.utc) + app.timedelta(hours=3)).isoformat()
        api_windows = [
            app.Window(label="session", percent_remaining=100.0, percent_used=0.0),
            app.Window(label="weekly", percent_remaining=1.0, percent_used=99.0),
        ]
        statusline_windows = [app.Window(label="session", percent_remaining=20.0, percent_used=80.0, reset_at=future)]

        merged = service._merge_claude_statusline_windows(api_windows, statusline_windows)

        self.assertEqual([window.label for window in merged], ["session", "weekly"])
        self.assertEqual(merged[0].percent_used, 80.0)
        self.assertEqual(merged[1].percent_used, 99.0)

    def test_primary_cells_lead_with_used_percent_not_remaining_percent(self):
        self.assertIn(
            '<span class="pct ${cls.pct}">${pct(window.percent_used)} used</span>',
            app.HTML,
        )
        self.assertIn(
            '<span class="used">${pct(window.percent_remaining)} left</span>',
            app.HTML,
        )
        self.assertIn(
            '<div class="secondary__item__pct">${pct(window.percent_used)} used</div>',
            app.HTML,
        )
        self.assertIn("live · statusline", app.HTML)

    def test_root_html_is_sent_with_no_store_cache_header(self):
        self.assertIn('self.send_header("Cache-Control", "no-store")', APP_PATH.read_text())

    def test_weekly_pace_marker_uses_weekly_summary_target(self):
        self.assertIn(
            "const marker = weekly && summary ? summary.expectedUsed : windowExpectedUsed(window);",
            app.HTML,
        )
        self.assertIn("if (!spanMs && label.startsWith('weekly'))", app.HTML)


if __name__ == "__main__":
    unittest.main()
