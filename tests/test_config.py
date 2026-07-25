import base64
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


def jwt_with_exp(exp: int) -> str:
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': exp})}.signature"


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

    def test_missing_auth_error_does_not_expose_configured_path(self):
        with tempfile.TemporaryDirectory() as td:
            auth_path = str(Path(td) / "private" / "auth.json")
            service = app.UsageService(
                app.AppConfig(
                    auth_path=auth_path,
                    cache_path=str(Path(td) / "cache.json"),
                    disabled_providers=frozenset({"openai-codex", "anthropic", "kimi-coding", "xai-oauth", "zai"}),
                )
            )

            payload = service.collect_all()

            self.assertNotIn(auth_path, json.dumps(payload))
            self.assertEqual(payload["providers"][0]["error"], "configured auth file not found")

    def test_existing_auth_without_provider_does_not_expose_configured_path(self):
        with tempfile.TemporaryDirectory() as td:
            auth_path = Path(td) / "private" / "auth.json"
            auth_path.parent.mkdir()
            auth_path.write_text(json.dumps({"credential_pool": {}}), encoding="utf-8")
            service = app.UsageService(
                app.AppConfig(
                    auth_path=str(auth_path),
                    cache_path=str(Path(td) / "cache.json"),
                    disabled_providers=frozenset({"openai-codex", "anthropic", "kimi-coding", "xai-oauth", "zai"}),
                )
            )

            payload = service.collect_all()

            self.assertNotIn(str(auth_path), json.dumps(payload))
            self.assertEqual(payload["providers"][0]["error"], "no credential found for minimax")

    def test_unreadable_auth_shape_does_not_expose_configured_path(self):
        with tempfile.TemporaryDirectory() as td:
            auth_path = Path(td) / "private" / "auth.json"
            auth_path.mkdir(parents=True)
            service = app.UsageService(
                app.AppConfig(
                    auth_path=str(auth_path),
                    cache_path=str(Path(td) / "cache.json"),
                    disabled_providers=frozenset({"openai-codex", "anthropic", "kimi-coding", "xai-oauth", "zai"}),
                )
            )

            payload = service.collect_all()

            self.assertNotIn(str(auth_path), json.dumps(payload))
            self.assertEqual(payload["providers"][0]["error"], "configured auth file could not be read")

    def test_unreadable_global_fallback_does_not_expose_its_path(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(json.dumps({"credential_pool": {}}), encoding="utf-8")
            global_path = hermes_root / "auth.json"
            global_path.mkdir()
            service = app.UsageService(
                app.AppConfig(
                    auth_path=str(profile_path),
                    cache_path=str(Path(td) / "cache.json"),
                    disabled_providers=frozenset({"openai-codex", "anthropic", "kimi-coding", "xai-oauth", "zai"}),
                )
            )

            payload = service.collect_all()

            self.assertNotIn(str(global_path), json.dumps(payload))
            self.assertEqual(payload["providers"][0]["error"], "credential fallback auth file could not be read")

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

    def test_load_config_parses_disabled_providers(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.yaml"
            cfg_path.write_text(
                "disabled_providers: zai, kimi-coding, xai-oauth\n",
                encoding="utf-8",
            )

            cfg = app.load_config(str(cfg_path))

            self.assertEqual(
                cfg.disabled_providers,
                frozenset({"zai", "kimi-coding", "xai-oauth"}),
            )

    def test_load_config_rejects_unknown_disabled_provider(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.yaml"
            cfg_path.write_text("disabled_providers: typo-provider\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unknown disabled provider: typo-provider"):
                app.load_config(str(cfg_path))

    def test_collect_all_skips_disabled_providers_and_summary_excludes_them(self):
        service = app.UsageService(
            app.AppConfig(
                auth_path="/tmp/private-auth.json",
                disabled_providers=frozenset({"zai", "kimi-coding", "xai-oauth"}),
            )
        )
        probed = []

        def fake_probe(provider, fn):
            probed.append(provider)
            return app.ProviderResult(provider=provider, status="ok", source="test")

        old_safe_probe = service._safe_probe
        try:
            service._safe_probe = fake_probe
            payload = service.collect_all()
        finally:
            service._safe_probe = old_safe_probe

        self.assertEqual(probed, ["minimax", "openai-codex", "anthropic"])
        self.assertEqual(
            [provider["provider"] for provider in payload["providers"]],
            ["minimax", "openai-codex", "anthropic"],
        )
        self.assertEqual(payload["summary"], {"ok": 3, "degraded": 0, "total": 3, "errors": 0})

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

    def test_profile_auth_falls_back_to_global_hermes_pool(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(json.dumps({"credential_pool": {}}), encoding="utf-8")
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openai-codex": [
                                {
                                    "id": "global-codex",
                                    "source": "device_code",
                                    "access_token": "global-token",
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            credentials = app.AuthStore(str(profile_path)).credentials("openai-codex")

            self.assertEqual(len(credentials), 1)
            self.assertEqual(credentials[0]["access_token"], "global-token")
            self.assertEqual(credentials[0]["__auth_path"], str(global_path))

    def test_missing_profile_auth_falls_back_to_global_hermes_pool(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            hermes_root.mkdir()
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "minimax": [
                                {"id": "global-minimax", "source": "env:MINIMAX_API_KEY"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            credentials = app.AuthStore(str(profile_path)).credentials("minimax")

            self.assertEqual(len(credentials), 1)
            self.assertEqual(credentials[0]["id"], "global-minimax")
            self.assertEqual(credentials[0]["__auth_path"], str(global_path))

    def test_global_env_credential_resolves_from_global_dotenv(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(json.dumps({"credential_pool": {}}), encoding="utf-8")
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "minimax": [
                                {"id": "global-minimax", "source": "env:PUD_GLOBAL_MINIMAX_TOKEN"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            (hermes_root / ".env").write_text("PUD_GLOBAL_MINIMAX_TOKEN=global-dotenv-token\n", encoding="utf-8")
            previous = os.environ.pop("PUD_GLOBAL_MINIMAX_TOKEN", None)
            try:
                service = app.UsageService(app.AppConfig(auth_path=str(profile_path)))
                credential = service.auth.credentials("minimax")[0]
                token = service._require_access_token(credential, "minimax")
            finally:
                if previous is not None:
                    os.environ["PUD_GLOBAL_MINIMAX_TOKEN"] = previous

            self.assertEqual(token, "global-dotenv-token")
            self.assertEqual(credential["__auth_path"], str(global_path))

    def test_global_env_credential_resolves_from_active_profile_dotenv(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(json.dumps({"credential_pool": {}}), encoding="utf-8")
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "minimax": [
                                {"id": "global-minimax", "source": "env:PUD_PROFILE_MINIMAX_TOKEN"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            profile_path.with_name(".env").write_text(
                "PUD_PROFILE_MINIMAX_TOKEN=profile-dotenv-token\n",
                encoding="utf-8",
            )
            previous = os.environ.pop("PUD_PROFILE_MINIMAX_TOKEN", None)
            try:
                service = app.UsageService(app.AppConfig(auth_path=str(profile_path)))
                credential = service.auth.credentials("minimax")[0]
                token = service._require_access_token(credential, "minimax")
            finally:
                if previous is not None:
                    os.environ["PUD_PROFILE_MINIMAX_TOKEN"] = previous

            self.assertEqual(token, "profile-dotenv-token")
            self.assertEqual(credential["__auth_path"], str(global_path))

    def test_healthy_global_credential_beats_stripped_profile_shell_with_same_identity(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            shell = {"id": "shared-codex", "source": "device_code", "last_status": "ok"}
            profile_path.write_text(
                json.dumps({"credential_pool": {"openai-codex": [shell]}}),
                encoding="utf-8",
            )
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openai-codex": [
                                {
                                    **shell,
                                    "access_token": "global-token",
                                    "refresh_token": "global-refresh",
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            credentials = app.AuthStore(str(profile_path)).credentials("openai-codex")

            self.assertEqual(len(credentials), 1)
            self.assertEqual(credentials[0]["access_token"], "global-token")
            self.assertEqual(credentials[0]["__auth_path"], str(global_path))

    def test_usable_profile_credentials_keep_authority_over_global_pool(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openai-codex": [
                                {"id": "profile", "source": "device_code", "access_token": "profile-token"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openai-codex": [
                                {
                                    "id": "global",
                                    "source": "device_code",
                                    "access_token": "global-token",
                                    "priority": 1,
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            credentials = app.AuthStore(str(profile_path)).credentials("openai-codex")

            self.assertEqual([entry["id"] for entry in credentials], ["profile"])

    def test_zai_aliases_exhaust_profile_scope_before_global_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "zai": [
                                {"id": "profile-zai", "source": "api_key", "access_token": "profile-token"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "custom:zai": [
                                {"id": "global-zai", "source": "api_key", "access_token": "global-token"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            credential = app.AuthStore(str(profile_path)).first_credential("custom:zai", "zai")

            self.assertIsNotNone(credential)
            self.assertEqual(credential["id"], "profile-zai")
            self.assertEqual(credential["access_token"], "profile-token")
            self.assertEqual(credential["__auth_path"], str(profile_path))

    def test_zai_primary_provider_precedes_alias_within_profile_scope(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "custom:zai": [
                                {"id": "primary", "source": "api_key", "priority": 50, "access_token": "primary-token"}
                            ],
                            "zai": [
                                {"id": "alias", "source": "api_key", "priority": 1, "access_token": "alias-token"}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            credential = app.AuthStore(str(profile_path)).first_credential("custom:zai", "zai")

            self.assertIsNotNone(credential)
            self.assertEqual(credential["id"], "primary")
            self.assertEqual(credential["access_token"], "primary-token")

    def test_zai_usable_alias_precedes_unusable_primary_shell(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "custom:zai": [
                                {"id": "primary-shell", "source": "api_key", "priority": 1}
                            ],
                            "zai": [
                                {"id": "alias", "source": "api_key", "priority": 50, "access_token": "alias-token"}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            credential = app.AuthStore(str(profile_path)).first_credential("custom:zai", "zai")

            self.assertIsNotNone(credential)
            self.assertEqual(credential["id"], "alias")
            self.assertEqual(credential["access_token"], "alias-token")

    def test_zai_aliases_use_global_scope_when_profile_aliases_are_unusable(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(
                json.dumps({"credential_pool": {"zai": [{"id": "profile-shell", "source": "api_key"}]}}),
                encoding="utf-8",
            )
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "custom:zai": [
                                {"id": "global-zai", "source": "api_key", "access_token": "global-token"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            credential = app.AuthStore(str(profile_path)).first_credential("custom:zai", "zai")

            self.assertIsNotNone(credential)
            self.assertEqual(credential["id"], "global-zai")
            self.assertEqual(credential["access_token"], "global-token")
            self.assertEqual(credential["__auth_path"], str(global_path))

    def test_expired_access_only_profile_credential_uses_global_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            expired_token = jwt_with_exp(int(app.datetime.now(app.timezone.utc).timestamp()) - 60)
            profile_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openai-codex": [
                                {"id": "profile", "source": "device_code", "access_token": expired_token}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openai-codex": [
                                {"id": "global", "source": "device_code", "access_token": "global-token"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            credentials = app.AuthStore(str(profile_path)).credentials("openai-codex")

            self.assertEqual(credentials[0]["id"], "global")
            self.assertEqual(credentials[0]["access_token"], "global-token")
            self.assertEqual(credentials[0]["__auth_path"], str(global_path))

    def test_expired_profile_credential_with_refresh_token_keeps_authority(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            expired_token = jwt_with_exp(int(app.datetime.now(app.timezone.utc).timestamp()) - 60)
            profile_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openai-codex": [
                                {
                                    "id": "profile",
                                    "source": "device_code",
                                    "access_token": expired_token,
                                    "refresh_token": "profile-refresh",
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            (hermes_root / "auth.json").write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openai-codex": [
                                {"id": "global", "source": "device_code", "access_token": "global-token"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            credentials = app.AuthStore(str(profile_path)).credentials("openai-codex")

            self.assertEqual([entry["id"] for entry in credentials], ["profile"])
            self.assertEqual(credentials[0]["refresh_token"], "profile-refresh")

    def test_usable_profile_credentials_ignore_malformed_global_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openai-codex": [
                                {"id": "profile", "source": "device_code", "access_token": "profile-token"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            (hermes_root / "auth.json").write_text("not-json", encoding="utf-8")

            credentials = app.AuthStore(str(profile_path)).credentials("openai-codex")

            self.assertEqual([entry["id"] for entry in credentials], ["profile"])
            self.assertEqual(credentials[0]["access_token"], "profile-token")

    def test_unresolved_profile_env_reference_uses_global_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "minimax": [
                                {"id": "profile", "source": "env:PUD_TEST_MISSING_PROFILE_TOKEN"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "minimax": [
                                {"id": "global", "source": "api_key", "access_token": "global-token"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            previous = os.environ.pop("PUD_TEST_MISSING_PROFILE_TOKEN", None)
            try:
                credentials = app.AuthStore(str(profile_path)).credentials("minimax")
            finally:
                if previous is not None:
                    os.environ["PUD_TEST_MISSING_PROFILE_TOKEN"] = previous

            self.assertEqual(credentials[0]["id"], "global")

    def test_anonymous_credentials_are_not_collapsed(self):
        with tempfile.TemporaryDirectory() as td:
            auth_path = Path(td) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "minimax": [
                                {"access_token": "token-a"},
                                {"access_token": "token-b"},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            credentials = app.AuthStore(str(auth_path)).credentials("minimax")

            self.assertEqual([entry["access_token"] for entry in credentials], ["token-a", "token-b"])

    def test_fallback_credential_updates_its_global_source_store(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(json.dumps({"credential_pool": {}}), encoding="utf-8")
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "openai-codex": [
                                {"id": "global-codex", "source": "device_code", "access_token": "old"}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = app.AuthStore(str(profile_path))
            credential = store.credentials("openai-codex")[0]

            store.update_credential(
                "openai-codex",
                "global-codex",
                {"access_token": "new"},
                auth_path=credential["__auth_path"],
            )

            self.assertEqual(
                json.loads(global_path.read_text())["credential_pool"]["openai-codex"][0]["access_token"],
                "new",
            )
            self.assertEqual(json.loads(profile_path.read_text()), {"credential_pool": {}})

    def test_codex_refresh_persists_global_provider_singleton_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(json.dumps({"credential_pool": {}}), encoding="utf-8")
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "openai-codex": {
                                "auth_mode": "oauth",
                                "tokens": {
                                    "access_token": jwt_with_exp(1),
                                    "refresh_token": "old-refresh",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            service = app.UsageService(app.AppConfig(auth_path=str(profile_path)))
            credential = service.auth.credentials("openai-codex")[0]
            old_http_json = app.http_json
            try:
                app.http_json = lambda *args, **kwargs: {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "id_token": "new-id-token",
                }
                refreshed = service._refresh_codex_if_needed(credential)
            finally:
                app.http_json = old_http_json

            stored = json.loads(global_path.read_text(encoding="utf-8"))
            provider = stored["providers"]["openai-codex"]
            self.assertEqual(refreshed["access_token"], "new-access")
            self.assertEqual(provider["tokens"]["access_token"], "new-access")
            self.assertEqual(provider["tokens"]["refresh_token"], "new-refresh")
            self.assertEqual(provider["tokens"]["id_token"], "new-id-token")
            self.assertEqual(refreshed["id_token"], "new-id-token")
            self.assertIn("last_refresh", provider)
            self.assertEqual(json.loads(profile_path.read_text()), {"credential_pool": {}})

    def test_provider_singleton_refresh_preserves_existing_root_token_location(self):
        with tempfile.TemporaryDirectory() as td:
            auth_path = Path(td) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "openai-codex": {
                                "access_token": "old-access",
                                "refresh_token": "old-refresh",
                                "id_token": "old-id-token",
                                "tokens": {},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = app.AuthStore(str(auth_path))
            credential = store.credentials("openai-codex")[0]

            store.update_credential(
                "openai-codex",
                credential["id"],
                {"access_token": "new-access", "refresh_token": "new-refresh", "id_token": False},
                auth_path=credential["__auth_path"],
            )

            stored = json.loads(auth_path.read_text(encoding="utf-8"))["providers"]["openai-codex"]
            reloaded = app.AuthStore(str(auth_path)).credentials("openai-codex")[0]
            self.assertEqual(stored["access_token"], "new-access")
            self.assertEqual(stored["refresh_token"], "new-refresh")
            self.assertIs(stored["id_token"], False)
            self.assertEqual(stored["tokens"], {})
            self.assertEqual(reloaded["access_token"], "new-access")
            self.assertIs(reloaded["id_token"], False)

    def test_provider_singleton_refresh_updates_duplicate_token_locations(self):
        with tempfile.TemporaryDirectory() as td:
            auth_path = Path(td) / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "openai-codex": {
                                "access_token": "root-old",
                                "refresh_token": "root-refresh",
                                "tokens": {
                                    "access_token": "nested-old",
                                    "refresh_token": "nested-refresh",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = app.AuthStore(str(auth_path))
            credential = store.credentials("openai-codex")[0]

            store.update_credential(
                "openai-codex",
                credential["id"],
                {"access_token": "new-access", "refresh_token": "new-refresh"},
                auth_path=credential["__auth_path"],
            )

            provider = json.loads(auth_path.read_text(encoding="utf-8"))["providers"]["openai-codex"]
            self.assertEqual(provider["access_token"], "new-access")
            self.assertEqual(provider["refresh_token"], "new-refresh")
            self.assertEqual(provider["tokens"]["access_token"], "new-access")
            self.assertEqual(provider["tokens"]["refresh_token"], "new-refresh")

    def test_claude_refresh_persists_global_provider_singleton_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            hermes_root = Path(td) / ".hermes"
            profile_path = hermes_root / "profiles" / "dev" / "auth.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(json.dumps({"credential_pool": {}}), encoding="utf-8")
            global_path = hermes_root / "auth.json"
            global_path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "anthropic": {
                                "auth_mode": "oauth",
                                "tokens": {
                                    "access_token": "old-access",
                                    "refresh_token": "old-refresh",
                                    "id_token": "old-id-token",
                                    "expires_at_ms": 1,
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            service = app.UsageService(app.AppConfig(auth_path=str(profile_path)))
            credential = service.auth.credentials("anthropic")[0]
            old_http_json = app.http_json
            try:
                app.http_json = lambda *args, **kwargs: {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "id_token": None,
                    "expires_in": 3600,
                }
                refreshed = service._refresh_claude_if_needed(credential, "dev-auth")
            finally:
                app.http_json = old_http_json

            stored = json.loads(global_path.read_text(encoding="utf-8"))
            provider = stored["providers"]["anthropic"]
            self.assertEqual(refreshed["access_token"], "new-access")
            self.assertEqual(provider["tokens"]["access_token"], "new-access")
            self.assertEqual(provider["tokens"]["refresh_token"], "new-refresh")
            self.assertIsNone(provider["tokens"]["id_token"])
            self.assertIsNone(refreshed["id_token"])
            self.assertGreater(provider["tokens"]["expires_at_ms"], 1)
            self.assertNotIn("expires_at_ms", provider)
            self.assertIn("last_refresh", provider)
            self.assertEqual(json.loads(profile_path.read_text()), {"credential_pool": {}})

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
                            "start_time": 1783677600000,
                            # The API can report less than the nominal five-hour
                            # span while this is still the general session pool.
                            "end_time": 1783692000000,
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
            self.assertEqual(result.windows[0].label, "session")
            self.assertEqual(result.windows[0].remaining_text, "42.5% left")
            self.assertEqual(result.windows[0].meta["source_model"], "general")
            self.assertEqual(result.windows[0].meta["window_start"], "2026-07-10T10:00:00Z")
            self.assertEqual(result.windows[-1].label, "weekly")
            self.assertEqual(result.windows[-1].remaining_text, "80.0% left")

    def test_minimax_usage_counts_are_used_not_remaining(self):
        with tempfile.TemporaryDirectory() as td:
            auth_path = Path(td) / "auth.json"
            auth_path.write_text(
                json.dumps({"credential_pool": {"minimax": [{"access_token": "token"}]}}),
                encoding="utf-8",
            )
            service = app.UsageService(app.AppConfig(auth_path=str(auth_path)))
            payload = {
                "base_resp": {"status_code": 0},
                "model_remains": [{
                    "model_name": "general",
                    "current_interval_total_count": 10,
                    "current_interval_usage_count": 3,
                    "current_interval_remaining_percent": 1,
                    "current_weekly_total_count": 20,
                    "current_weekly_usage_count": 5,
                    "current_weekly_remaining_percent": 1,
                }],
            }
            old_http_json = app.http_json
            try:
                app.http_json = lambda *args, **kwargs: payload
                result = service.probe_minimax()
            finally:
                app.http_json = old_http_json

            self.assertEqual(result.windows[0].percent_remaining, 70.0)
            self.assertEqual(result.windows[0].remaining_text, "7/10 requests left")
            self.assertEqual(result.windows[0].meta["used_count"], 3)
            self.assertEqual(result.windows[-1].percent_remaining, 75.0)
            self.assertEqual(result.windows[-1].remaining_text, "15/20 requests left")

    def test_minimax_keeps_exhausted_percent_only_windows(self):
        with tempfile.TemporaryDirectory() as td:
            auth_path = Path(td) / "auth.json"
            auth_path.write_text(
                json.dumps({"credential_pool": {"minimax": [{"access_token": "token"}]}}),
                encoding="utf-8",
            )
            service = app.UsageService(app.AppConfig(auth_path=str(auth_path)))
            payload = {
                "base_resp": {"status_code": 0},
                "model_remains": [{
                    "model_name": "general",
                    "current_interval_remaining_percent": 0,
                    "current_weekly_remaining_percent": 0,
                }],
            }
            old_http_json = app.http_json
            try:
                app.http_json = lambda *args, **kwargs: payload
                result = service.probe_minimax()
            finally:
                app.http_json = old_http_json

            self.assertEqual([window.percent_remaining for window in result.windows], [0.0, 0.0])
            self.assertEqual([window.percent_used for window in result.windows], [100.0, 100.0])

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

    def test_weekly_provider_keeps_non_display_fallback_in_session_cell(self):
        self.assertIn(
            "const sessionWindow = windowByLabel(provider, 'session') || fallbackPrimaries.shift() || null;",
            app.HTML,
        )
        self.assertNotIn(
            "windowByLabel(provider, 'session') || (weeklyWindow ? null : fallbackPrimaries.shift())",
            app.HTML,
        )

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
