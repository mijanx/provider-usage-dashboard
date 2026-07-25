#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8768
DEFAULT_AUTH_PATH = os.environ.get("PROVIDER_USAGE_AUTH_PATH", str(Path.home() / ".hermes" / "profiles" / "dev" / "auth.json"))
DEFAULT_TIMEOUT = 15
DEFAULT_REFRESH_SECONDS = 60
CLAUDE_STATUSLINE_MAX_AGE_SECONDS = 30 * 60
PROVIDER_PROBES = (
    ("minimax", "probe_minimax"),
    ("zai", "probe_zai"),
    ("openai-codex", "probe_codex"),
    ("anthropic", "probe_claude"),
    ("kimi-coding", "probe_kimi"),
    ("xai-oauth", "probe_xai_oauth"),
)
KNOWN_PROVIDERS = frozenset(provider for provider, _ in PROVIDER_PROBES)

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_REFRESH_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_BETA = "oauth-2025-04-20"
CLAUDE_SCOPES = "user:profile user:inference user:sessions:claude_code"

KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
KIMI_OAUTH_REFRESH_URL = "https://auth.kimi.com/api/oauth/token"
KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
MINIMAX_USAGE_URL = "https://api.minimax.io/v1/token_plan/remains"
ZAI_LIMIT_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
ZAI_MODEL_USAGE_URL = "https://api.z.ai/api/monitor/usage/model-usage?timeRange=7d"
XAI_ME_URL = "https://api.x.ai/v1/me"
XAI_SUBSCRIPTIONS_URL = "https://grok.com/rest/subscriptions"
XAI_CREDITS_URL = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"


@dataclass
class AppConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    auth_path: str = DEFAULT_AUTH_PATH
    timeout_seconds: int = DEFAULT_TIMEOUT
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS
    claude_credentials_path: str = os.environ.get("CLAUDE_CREDENTIALS_PATH", str(Path.home() / ".claude" / ".credentials.json"))
    claude_statusline_path: str = os.environ.get("CLAUDE_STATUSLINE_PATH", str(Path.home() / ".claude" / "statusline-rate-limits.json"))
    cache_path: str = os.environ.get("PROVIDER_USAGE_CACHE_PATH", str(Path.cwd() / "cache" / "usage-cache.json"))
    kimi_credentials_path: str = os.environ.get("KIMI_CREDENTIALS_PATH", str(Path.home() / ".kimi" / "credentials" / "kimi-code.json"))
    disabled_providers: frozenset[str] = frozenset()


@dataclass
class Window:
    label: str
    percent_remaining: float | None = None
    percent_used: float | None = None
    remaining_text: str | None = None
    reset_at: str | None = None
    reset_text: str | None = None
    meta: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "percent_remaining": self.percent_remaining,
            "percent_used": self.percent_used,
            "remaining_text": self.remaining_text,
            "reset_at": self.reset_at,
            "reset_text": self.reset_text,
            "meta": self.meta or {},
        }


@dataclass
class ProviderResult:
    provider: str
    status: str
    source: str
    plan: str | None = None
    email: str | None = None
    windows: list[Window] | None = None
    error: str | None = None
    checked_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "source": self.source,
            "plan": self.plan,
            "email": self.email,
            "windows": [window.as_dict() for window in (self.windows or [])],
            "error": self.error,
            "checked_at": self.checked_at,
        }


class ProbeError(RuntimeError):
    pass


class AuthStore:
    def __init__(self, path: str):
        self.path = Path(expand_path(path))
        self._cache_mtime: float | None = None
        self._cache: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise ProbeError("configured auth file not found")
        try:
            stat = self.path.stat()
        except OSError:
            raise ProbeError("configured auth file could not be read") from None
        if self._cache is None or self._cache_mtime != stat.st_mtime:
            self._cache = self._read_document(self.path, "configured auth file could not be read")
            self._cache_mtime = stat.st_mtime
        return self._cache

    @staticmethod
    def _read_document(path: Path, error_message: str) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ProbeError(error_message) from None
        if not isinstance(data, dict):
            raise ProbeError(error_message)
        return data

    def _global_fallback_path(self) -> Path | None:
        """Return Hermes' global auth store for a profile-scoped auth path."""
        parts = self.path.parts
        try:
            profiles_index = parts.index("profiles")
        except ValueError:
            return None
        if profiles_index < 1 or parts[profiles_index - 1] != ".hermes":
            return None
        if len(parts) != profiles_index + 3 or parts[-1] != "auth.json":
            return None
        candidate = Path(*parts[:profiles_index]) / "auth.json"
        return candidate if candidate != self.path and candidate.exists() else None


    def save(self, data: dict[str, Any]) -> None:
        try:
            self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            mtime = self.path.stat().st_mtime
        except OSError:
            raise ProbeError("configured auth file could not be updated") from None
        self._cache = data
        self._cache_mtime = mtime

    def resolve_env_credential(self, credential: dict[str, Any]) -> str | None:
        """Resolve env-backed credentials in the active profile, then source store."""
        source = str(credential.get("source") or "")
        if not source.startswith("env:"):
            return None
        env_key = source.split(":", 1)[1].strip()
        if not env_key:
            return None
        token = os.environ.get(env_key)
        if token:
            return token
        source_auth_path = Path(str(credential.get("__auth_path") or self.path))
        dotenv_paths = [self.path.with_name(".env"), source_auth_path.with_name(".env")]
        for dotenv_path in dict.fromkeys(dotenv_paths):
            token = load_env_value(dotenv_path, env_key)
            if token:
                return token
        return None

    def _document_credentials(self, auth_path: Path, data: dict[str, Any], provider: str) -> list[dict[str, Any]]:
        pool = data.get("credential_pool", {}).get(provider)
        candidates = [entry for entry in (pool or []) if isinstance(entry, dict)] if isinstance(pool, list) else []

        # Hermes shared auth can hold a fresher provider singleton while the credential pool
        # still contains an expired/stale slot. The dashboard should not let that stale pool
        # entry poison the whole provider card.
        provider_entry = self._provider_singleton_credential(data, provider)
        if provider_entry:
            candidates.append(provider_entry)
        prepared: list[dict[str, Any]] = []
        for raw_entry in candidates:
            entry = dict(raw_entry)
            entry["__auth_path"] = str(auth_path)
            prepared.append(entry)
        return prepared

    def _has_auth_material(self, entry: dict[str, Any]) -> bool:
        if entry.get("refresh_token"):
            return True
        access_token = entry.get("access_token")
        if access_token:
            token_exp = as_int(jwt_claim(access_token, "exp"))
            if token_exp is None:
                return True
            expires_at = datetime.fromtimestamp(token_exp, tz=timezone.utc)
            return expires_at - datetime.now(timezone.utc) > timedelta(minutes=10)
        return bool(self.resolve_env_credential(entry))

    def credentials(self, provider: str) -> list[dict[str, Any]]:
        profile_exists = self.path.exists()
        profile_entries = self._document_credentials(self.path, self.load(), provider) if profile_exists else []

        # This is fallback, not a competing global pool: when the profile has usable
        # authority, do not even read a malformed or unavailable global auth store.
        if any(self._has_auth_material(entry) for entry in profile_entries):
            entries = profile_entries
        else:
            fallback_entries: list[dict[str, Any]] = []
            fallback = self._global_fallback_path()
            if fallback is not None:
                fallback_data = self._read_document(fallback, "credential fallback auth file could not be read")
                fallback_entries = self._document_credentials(fallback, fallback_data, provider)
            if not profile_exists and fallback is None:
                self.load()  # Preserve the configured-path error when no auth store exists.
            entries = profile_entries + fallback_entries
        if not entries:
            return []

        def sort_key(entry: dict[str, Any]) -> tuple[int, int, int, int]:
            status = str(entry.get("last_status") or "").lower()
            error_code = as_int(entry.get("last_error_code"))
            error_reason = str(entry.get("last_error_reason") or "").lower()
            priority = as_int(entry.get("priority")) or 9999
            usable_rank = 0 if self._has_auth_material(entry) else 1
            token_exp = as_int(jwt_claim(entry.get("access_token"), "exp"))
            token_is_fresh = token_exp is not None and datetime.fromtimestamp(token_exp, tz=timezone.utc) - datetime.now(timezone.utc) > timedelta(minutes=10)
            token_is_expired = token_exp is not None and not token_is_fresh
            if token_is_fresh:
                health_rank = 0
            elif status == "ok" and not token_is_expired:
                health_rank = 1
            elif not status and error_code is None and not error_reason and not token_is_expired:
                health_rank = 1
            elif status in {"exhausted", "rate_limited"} or error_code in {401, 402, 429} or error_reason in {"exhausted", "rate_limited"} or token_is_expired:
                health_rank = 2
            else:
                health_rank = 1
            refresh_hint = parse_iso(entry.get("last_refresh") or entry.get("last_status_at"))
            freshness_rank = -int(refresh_hint.timestamp()) if refresh_hint is not None else 0
            return usable_rank, health_rank, priority, freshness_rank

        # Rank before deduplicating: stripped profile credential shells may have the
        # same identity as a healthy global credential. Stable sorting still makes
        # the profile entry win when both copies have equivalent health/priority.
        ordered = sorted(entries, key=sort_key)
        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for entry in ordered:
            identity = (str(entry.get("id") or ""), str(entry.get("source") or ""))
            if identity == ("", "") or identity not in seen:
                seen.add(identity)
                deduplicated.append(entry)
        return deduplicated

    def _provider_singleton_credential(self, data: dict[str, Any], provider: str) -> dict[str, Any] | None:
        raw_providers = data.get("providers")
        provider_data = raw_providers.get(provider) if isinstance(raw_providers, dict) else None
        if not isinstance(provider_data, dict):
            return None
        raw_tokens = provider_data.get("tokens")
        tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
        access_token = provider_data.get("access_token") or tokens.get("access_token")
        refresh_token = provider_data.get("refresh_token") or tokens.get("refresh_token")
        if not access_token and not refresh_token:
            return None
        return {
            "id": f"__provider__:{provider}",
            "label": provider_data.get("label") or provider,
            "source": provider_data.get("auth_mode") or "providers",
            "priority": -1,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": provider_data.get("id_token") or tokens.get("id_token"),
            "last_refresh": provider_data.get("last_refresh"),
            "last_status": "ok" if access_token else None,
        }

    def first_credential(self, provider: str) -> dict[str, Any] | None:
        ordered = self.credentials(provider)
        return ordered[0] if ordered else None

    def update_credential(
        self,
        provider: str,
        credential_id: str,
        updates: dict[str, Any],
        *,
        auth_path: str | None = None,
    ) -> dict[str, Any]:
        target_path = Path(auth_path) if auth_path else self.path
        data = self.load() if target_path == self.path else self._read_document(
            target_path,
            "credential source auth file could not be read",
        )
        pool = data.get("credential_pool", {}).get(provider)
        if not isinstance(pool, list):
            raise ProbeError(f"credential pool missing for {provider}")
        for entry in pool:
            if isinstance(entry, dict) and entry.get("id") == credential_id:
                entry.update(updates)
                data["updated_at"] = datetime.now(timezone.utc).isoformat()
                if target_path == self.path:
                    self.save(data)
                else:
                    try:
                        target_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    except OSError:
                        raise ProbeError("credential source auth file could not be updated") from None
                return {**entry, "__auth_path": str(target_path)}
        raise ProbeError(f"credential {credential_id} not found for {provider}")


class UsageService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.auth = AuthStore(config.auth_path)
        self.cache_path = Path(expand_path(config.cache_path))

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cache_result(self, result: ProviderResult) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache = self._load_cache()
        cache[result.provider] = result.as_dict()
        self.cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _cached_result(self, provider: str) -> ProviderResult | None:
        cache = self._load_cache().get(provider)
        if not isinstance(cache, dict):
            return None
        windows_raw = cache.get("windows") or []
        windows = [Window(**window) for window in windows_raw if isinstance(window, dict)]
        return ProviderResult(
            provider=provider,
            status=str(cache.get("status") or "stale"),
            source=str(cache.get("source") or "cache"),
            plan=cache.get("plan"),
            email=cache.get("email"),
            windows=windows,
            error=cache.get("error"),
            checked_at=cache.get("checked_at"),
        )

    def _degraded_result(self, provider: str, exc: Exception) -> ProviderResult:
        cached = self._cached_result(provider)
        if cached is not None and cached.windows:
            cached.status = "stale"
            cached.source = f"{cached.source}+cache"
            cached.error = f"Fresh probe failed; showing last good snapshot. {friendly_error(provider, str(exc))}"
            cached.checked_at = iso_now()
            return cached
        return ProviderResult(
            provider=provider,
            status=status_from_error(provider, str(exc)),
            source="probe",
            error=friendly_error(provider, str(exc)),
            windows=[],
            checked_at=iso_now(),
        )

    def collect_all(self) -> dict[str, Any]:
        now = iso_now()
        results = [
            self._safe_probe(provider, getattr(self, method_name))
            for provider, method_name in PROVIDER_PROBES
            if provider not in self.config.disabled_providers
        ]
        ok = sum(1 for result in results if result.status == "ok")
        degraded = sum(1 for result in results if result.status in {"stale", "rate_limited", "auth_required"})
        return {
            "generated_at": now,
            "host": self.config.host,
            "port": self.config.port,
            "providers": [result.as_dict() for result in results],
            "summary": {
                "ok": ok,
                "degraded": degraded,
                "total": len(results),
                "errors": len(results) - ok - degraded,
            },
        }

    def _safe_probe(self, provider: str, fn) -> ProviderResult:
        try:
            result = fn()
            result.checked_at = iso_now()
            self._save_cache_result(result)
            return result
        except Exception as exc:
            return self._degraded_result(provider, exc)

    def probe_minimax(self) -> ProviderResult:
        credential = self._require_credential("minimax")
        token = self._require_access_token(credential, "minimax")
        payload = http_json(
            MINIMAX_USAGE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=self.config.timeout_seconds,
        )
        base_resp = payload.get("base_resp") if isinstance(payload, dict) else None
        if isinstance(base_resp, dict) and as_int(base_resp.get("status_code")) not in (None, 0):
            raise ProbeError(f"MiniMax usage API returned {base_resp.get('status_code')}: {base_resp.get('status_msg')}")
        rows = payload.get("model_remains") or payload.get("modelRemains") or []
        if not isinstance(rows, list) or not rows:
            raise ProbeError("MiniMax returned no model quota rows")
        windows: list[Window] = []
        weekly_candidates: list[Window] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_model = str(row.get("model_name") or row.get("modelName") or "model")
            total = as_int(row["current_interval_total_count"] if "current_interval_total_count" in row else row.get("currentIntervalTotalCount"))
            usage_count = as_int(row["current_interval_usage_count"] if "current_interval_usage_count" in row else row.get("currentIntervalUsageCount"))
            percent_remaining = as_float(
                row["current_interval_remaining_percent"]
                if "current_interval_remaining_percent" in row
                else row.get("currentIntervalRemainingPercent")
            )
            window_start = from_epoch_ms(row.get("start_time") or row.get("startTime"))
            reset_at = from_epoch_ms(row.get("end_time") or row.get("endTime"))
            # MiniMax exposes the coding-plan session pool as the "general"
            # current interval. Its reported start can advance within the rolling
            # window, so the observed span is not reliably exactly five hours.
            label = "session" if source_model == "general" else source_model
            meta = {
                "status": row.get("current_interval_status") or row.get("currentIntervalStatus"),
                "source_model": source_model,
                "window_start": window_start,
                "window_end": reset_at,
            }
            if total is not None and usage_count is not None and total > 0:
                used_count = max(0, min(total, usage_count))
                remaining_count = total - used_count
                percent_remaining = round(remaining_count / total * 100, 1)
                remaining_text = f"{remaining_count}/{total} requests left"
                meta.update({"used_count": used_count, "total": total})
            elif percent_remaining is not None:
                percent_remaining = round(max(0.0, min(100.0, percent_remaining)), 1)
                remaining_text = f"{percent_remaining:.1f}% left"
            else:
                continue
            windows.append(
                Window(
                    label=label,
                    percent_remaining=percent_remaining,
                    percent_used=round(100 - percent_remaining, 1),
                    remaining_text=remaining_text,
                    reset_at=reset_at,
                    reset_text=relative_reset_text(reset_at),
                    meta=meta,
                )
            )

            weekly_total = as_int(row["current_weekly_total_count"] if "current_weekly_total_count" in row else row.get("currentWeeklyTotalCount"))
            weekly_usage = as_int(row["current_weekly_usage_count"] if "current_weekly_usage_count" in row else row.get("currentWeeklyUsageCount"))
            weekly_percent = as_float(
                row["current_weekly_remaining_percent"]
                if "current_weekly_remaining_percent" in row
                else row.get("currentWeeklyRemainingPercent")
            )
            weekly_start = from_epoch_ms(row.get("weekly_start_time") or row.get("weeklyStartTime"))
            weekly_end = from_epoch_ms(row.get("weekly_end_time") or row.get("weeklyEndTime"))
            weekly_meta = {
                "source_model": source_model,
                "status": row.get("current_weekly_status") or row.get("currentWeeklyStatus"),
                "window_start": weekly_start,
                "window_end": weekly_end,
            }
            if weekly_total is not None and weekly_usage is not None and weekly_total > 0:
                weekly_used = max(0, min(weekly_total, weekly_usage))
                weekly_remaining = weekly_total - weekly_used
                weekly_percent = round(weekly_remaining / weekly_total * 100, 1)
                weekly_text = f"{weekly_remaining}/{weekly_total} requests left"
                weekly_meta.update({"used_count": weekly_used, "total": weekly_total})
            elif weekly_percent is not None:
                weekly_percent = round(max(0.0, min(100.0, weekly_percent)), 1)
                weekly_text = f"{weekly_percent:.1f}% left"
            else:
                continue
            weekly_candidates.append(
                Window(
                    label="weekly",
                    percent_remaining=weekly_percent,
                    percent_used=round(100 - weekly_percent, 1),
                    remaining_text=weekly_text,
                    reset_at=weekly_end,
                    reset_text=relative_reset_text(weekly_end),
                    meta=weekly_meta,
                )
            )
        collapsed_windows: list[Window] = []
        dedupe_map: dict[tuple[Any, ...], Window] = {}
        for window in windows:
            meta = window.meta or {}
            is_coding_family = window.label == "MiniMax-M*" or "coding-plan" in window.label
            if not is_coding_family:
                collapsed_windows.append(window)
                continue
            dedupe_key = (
                meta.get("used_count"),
                meta.get("total"),
                window.percent_remaining,
                window.reset_at,
                window.remaining_text,
            )
            existing = dedupe_map.get(dedupe_key)
            if existing is None:
                dedupe_map[dedupe_key] = window
                collapsed_windows.append(window)
                continue
            aliases = list(existing.meta.get("aliases") or []) if isinstance(existing.meta, dict) else []
            aliases.append(window.label)
            existing.meta = {**(existing.meta or {}), "aliases": aliases}
        windows = collapsed_windows
        preferred = next((item for item in weekly_candidates if (item.meta or {}).get("source_model") in {"general", "MiniMax-M*"}), None)
        if preferred is None:
            preferred = next((item for item in weekly_candidates if "coding-plan" in str((item.meta or {}).get("source_model"))), None)
        if preferred is None and weekly_candidates:
            preferred = weekly_candidates[0]
        if preferred is not None:
            windows.append(preferred)
        return ProviderResult(provider="minimax", status="ok", source="api", windows=windows)

    def probe_zai(self) -> ProviderResult:
        credential = self.auth.first_credential("custom:zai") or self.auth.first_credential("zai")
        if credential is None:
            raise ProbeError("no Z.ai credential found in dev auth.json")
        token = self._require_access_token(credential, "zai")
        quota_payload = http_json(
            ZAI_LIMIT_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Accept-Language": "en-US,en",
            },
            timeout=self.config.timeout_seconds,
        )
        windows: list[Window] = []
        data = quota_payload.get("data") if isinstance(quota_payload, dict) else None
        items = []
        if isinstance(data, dict):
            items = data.get("limits") or data.get("quotaLimits") or data.get("quota_limits") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                quota_type = str(item.get("type") or "")
                unit = item.get("unit")
                label = self._zai_label(quota_type, unit)
                if label is None:
                    continue
                percentage = as_float(item.get("percentage"))
                reset_at = flexible_zai_date(item.get("nextResetTime") or item.get("next_reset_time"))
                current_value = as_int(item.get("currentValue") or item.get("current_value"))
                remaining = as_int(item.get("remaining"))
                usage = as_int(item.get("usage"))
                number = as_int(item.get("number"))
                used_pct = percentage
                remaining_pct = round(max(0.0, min(100.0, 100.0 - used_pct)), 1) if used_pct is not None else None
                remaining_text = None
                if remaining is not None and usage is not None:
                    remaining_text = f"{remaining} left of {usage}"
                elif remaining_pct is not None:
                    remaining_text = f"{remaining_pct:.1f}% left"
                windows.append(
                    Window(
                        label=label,
                        percent_remaining=remaining_pct,
                        percent_used=round(used_pct, 1) if used_pct is not None else None,
                        remaining_text=remaining_text,
                        reset_at=reset_at,
                        reset_text=relative_reset_text(reset_at),
                        meta={"type": quota_type, "unit": unit, "current_value": current_value, "usage": usage, "remaining": remaining, "number": number},
                    )
                )
        plan = data.get("level") if isinstance(data, dict) else None
        try:
            model_usage = http_json(
                ZAI_MODEL_USAGE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Accept-Language": "en-US,en",
                },
                timeout=self.config.timeout_seconds,
            )
            summary = summarize_zai_model_usage(model_usage)
            if summary:
                windows.append(summary)
        except Exception:
            pass
        if not windows:
            raise ProbeError("Z.ai returned no recognized quota windows")
        return ProviderResult(provider="zai", status="ok", source="api", windows=windows, plan=str(plan).upper() if plan else None)

    def probe_codex(self) -> ProviderResult:
        last_exc: Exception | None = None
        credentials = self.auth.credentials("openai-codex")
        if not credentials:
            raise ProbeError("no credential found for openai-codex")
        for credential in credentials:
            try:
                credential = self._refresh_codex_if_needed(credential)
                payload, headers = http_json_with_headers(
                    CODEX_USAGE_URL,
                    headers={"Authorization": f"Bearer {credential['access_token']}", "Accept": "application/json", "User-Agent": "provider-usage-dashboard"},
                    timeout=self.config.timeout_seconds,
                )
                rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else {}
                windows: list[Window] = []
                for label, body_key, header_key in [
                    ("session", "primary_window", "x-codex-primary-used-percent"),
                    ("weekly", "secondary_window", "x-codex-secondary-used-percent"),
                ]:
                    entry = (rate_limit.get(body_key) if isinstance(rate_limit, dict) else None) or {}
                    used_pct = header_float(headers.get(header_key))
                    if used_pct is None:
                        used_pct = as_float(entry.get("used_percent"))
                    if used_pct is None:
                        continue
                    remaining_pct = round(max(0.0, min(100.0, 100.0 - used_pct)), 1)
                    reset_at = from_epoch_maybe(entry.get("reset_at"))
                    if reset_at is None and entry.get("reset_after_seconds") is not None:
                        reset_at = iso_at_delta(as_float(entry.get("reset_after_seconds")) or 0)
                    windows.append(
                        Window(
                            label=label,
                            percent_remaining=remaining_pct,
                            percent_used=round(used_pct, 1),
                            remaining_text=f"{remaining_pct:.1f}% left",
                            reset_at=reset_at,
                            reset_text=relative_reset_text(reset_at),
                        )
                    )
                credits_balance = header_float(headers.get("x-codex-credits-balance"))
                if credits_balance is None:
                    credits = payload.get("credits") if isinstance(payload, dict) else None
                    if isinstance(credits, dict):
                        credits_balance = as_float(credits.get("balance"))
                if credits_balance is not None:
                    windows.append(
                        Window(
                            label="credits",
                            remaining_text=f"{credits_balance:.2f} credits",
                            percent_remaining=None,
                            percent_used=None,
                        )
                    )
                if not windows:
                    raise ProbeError("Codex returned no recognized usage windows")
                plan = payload.get("plan_type") if isinstance(payload, dict) else None
                email = jwt_claim(credential.get("access_token"), "email") or jwt_claim(credential.get("id_token"), "email")
                return ProviderResult(provider="openai-codex", status="ok", source="api", windows=windows, plan=str(plan).upper() if plan else None, email=email)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise ProbeError("Codex probe failed without a credential-specific error")

    def probe_claude(self) -> ProviderResult:
        credential, source = self._load_claude_credential()
        credential = self._refresh_claude_if_needed(credential, source)
        payload = None
        api_error: Exception | None = None
        try:
            payload = http_json(
                CLAUDE_USAGE_URL,
                headers={
                    "Authorization": f"Bearer {credential['access_token']}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "provider-usage-dashboard",
                    "anthropic-beta": CLAUDE_BETA,
                },
                timeout=self.config.timeout_seconds,
            )
        except Exception as exc:
            api_error = exc

        windows: list[Window] = []
        plan = None
        if isinstance(payload, dict):
            windows, plan = self._claude_windows_from_payload(payload)
        statusline_windows = self._claude_windows_from_statusline_file()
        if statusline_windows:
            if windows:
                windows = self._merge_claude_statusline_windows(windows, statusline_windows)
                source = f"claude-statusline+{source}"
            else:
                email = jwt_claim(credential.get("access_token"), "email") or jwt_claim(credential.get("refresh_token"), "email")
                if plan is None:
                    plan = credential.get("subscription_type") or credential.get("subscriptionType")
                return ProviderResult(provider="anthropic", status="ok", source="claude-statusline", windows=statusline_windows, plan=str(plan).upper() if plan else None, email=email)
        if not windows:
            if api_error is not None:
                raise api_error
            raise ProbeError("Claude returned no recognized usage windows")
        email = jwt_claim(credential.get("access_token"), "email") or jwt_claim(credential.get("refresh_token"), "email")
        if plan is None:
            plan = credential.get("subscription_type") or credential.get("subscriptionType")
        return ProviderResult(provider="anthropic", status="ok", source=source, windows=windows, plan=str(plan).upper() if plan else None, email=email)

    def _merge_claude_statusline_windows(self, api_windows: list[Window], statusline_windows: list[Window]) -> list[Window]:
        """Merge Claude Code statusline data without overriding account weekly usage.

        The Anthropic OAuth usage endpoint is authoritative for the aggregate
        account/subscription weekly quota. Claude Code statusline JSON can report
        a different ``seven_day`` value for the currently active Claude Code
        lane/session (observed live: API weekly 99% used while statusline weekly
        reported 9% used). Therefore statusline may only override the 5h/session
        primary window when current. Its weekly value is kept as a clearly-labeled
        secondary diagnostic window so the UI never leads with the wrong weekly
        quota again.
        """
        session_override = next(
            (window for window in statusline_windows if window.label == "session" and self._window_reset_is_current(window)),
            None,
        )
        statusline_weekly = next(
            (window for window in statusline_windows if window.label == "weekly" and self._window_reset_is_current(window)),
            None,
        )
        merged: list[Window] = []
        seen_labels: set[str] = set()
        for window in api_windows:
            if window.label == "session" and session_override is not None:
                merged.append(session_override)
                seen_labels.add("session")
                continue
            merged.append(window)
            seen_labels.add(window.label)
        if session_override is not None and "session" not in seen_labels:
            merged.append(session_override)
            seen_labels.add("session")
        if statusline_weekly is not None and any(window.label == "weekly" for window in api_windows):
            merged.append(
                Window(
                    label="weekly-statusline",
                    percent_remaining=statusline_weekly.percent_remaining,
                    percent_used=statusline_weekly.percent_used,
                    remaining_text=statusline_weekly.remaining_text,
                    reset_at=statusline_weekly.reset_at,
                    reset_text=statusline_weekly.reset_text,
                    meta={**(statusline_weekly.meta or {}), "source": "claude-code-statusline"},
                )
            )
        elif statusline_weekly is not None and "weekly" not in seen_labels:
            merged.append(statusline_weekly)
        return merged

    def _window_reset_is_current(self, window: Window) -> bool:
        reset_at = parse_iso(window.reset_at)
        if reset_at is None:
            return True
        return reset_at >= datetime.now(timezone.utc) - timedelta(minutes=1)

    def _claude_windows_from_payload(self, payload: dict[str, Any]) -> tuple[list[Window], str | None]:
        windows: list[Window] = []
        for key, label in [
            ("five_hour", "session"),
            ("seven_day", "weekly"),
            ("seven_day_sonnet", "weekly-sonnet"),
            ("seven_day_opus", "weekly-opus"),
        ]:
            entry = payload.get(key)
            if not isinstance(entry, dict):
                continue
            utilization = as_float(entry.get("utilization"))
            if utilization is None:
                continue
            remaining = round(max(0.0, min(100.0, 100.0 - utilization)), 1)
            reset_at = normalize_iso(entry.get("resets_at"))
            windows.append(
                Window(
                    label=label,
                    percent_remaining=remaining,
                    percent_used=round(utilization, 1),
                    remaining_text=f"{remaining:.1f}% left",
                    reset_at=reset_at,
                    reset_text=relative_reset_text(reset_at),
                )
            )
        extra = payload.get("extra_usage") or payload.get("extraUsage")
        plan = None
        if isinstance(extra, dict):
            if as_float(extra.get("monthly_limit")) is not None and as_float(extra.get("monthly_limit")) != 0:
                limit = as_float(extra.get("monthly_limit")) or 0.0
                used = as_float(extra.get("used")) or 0.0
                remaining = max(0.0, limit - used)
                percent = round(remaining / limit * 100, 1) if limit > 0 else None
                windows.append(
                    Window(
                        label="monthly-extra",
                        percent_remaining=percent,
                        percent_used=round(100 - percent, 1) if percent is not None else None,
                        remaining_text=f"${remaining/100:.2f} left of ${limit/100:.2f}",
                    )
                )
            plan = extra.get("subscription_type") or extra.get("subscriptionType")
        return windows, str(plan) if plan else None

    def _claude_windows_from_statusline_file(self) -> list[Window]:
        path_text = (self.config.claude_statusline_path or "").strip()
        if not path_text:
            return []
        path = Path(path_text).expanduser()
        if not path.exists():
            return []
        try:
            age_seconds = time.time() - path.stat().st_mtime
        except Exception:
            return []
        if age_seconds > CLAUDE_STATUSLINE_MAX_AGE_SECONDS:
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if not isinstance(rate_limits, dict):
            return []
        windows: list[Window] = []
        for key, label in (("five_hour", "session"), ("seven_day", "weekly")):
            entry = rate_limits.get(key)
            if not isinstance(entry, dict):
                continue
            used = as_float(entry.get("used_percentage"))
            if used is None:
                continue
            remaining = round(max(0.0, min(100.0, 100.0 - used)), 1)
            reset_at = from_epoch_maybe(entry.get("resets_at")) or normalize_iso(entry.get("resets_at"))
            windows.append(
                Window(
                    label=label,
                    percent_remaining=remaining,
                    percent_used=round(used, 1),
                    remaining_text=f"{remaining:.1f}% left",
                    reset_at=reset_at,
                    reset_text=relative_reset_text(reset_at),
                )
            )
        return windows

    def probe_kimi(self) -> ProviderResult:
        token, source = self._load_kimi_token()
        payload = http_json(
            KIMI_USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=self.config.timeout_seconds,
        )
        windows: list[Window] = []
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            weekly = self._kimi_window("weekly", usage)
            if weekly:
                windows.append(weekly)
        limits = payload.get("limits") if isinstance(payload, dict) else None
        if isinstance(limits, list):
            picked = None
            for limit in limits:
                if not isinstance(limit, dict):
                    continue
                window = limit.get("window") if isinstance(limit.get("window"), dict) else {}
                if window.get("duration") == 300:
                    picked = limit
                    break
            if picked is None and limits:
                picked = limits[0] if isinstance(limits[0], dict) else None
            if isinstance(picked, dict) and isinstance(picked.get("detail"), dict):
                fiveh = self._kimi_window("session", picked["detail"])
                if fiveh:
                    windows.append(fiveh)
        if not windows:
            raise ProbeError("Kimi returned no recognized windows")
        weekly_limit = as_int((usage or {}).get("limit"))
        membership = payload.get("user", {}).get("membership", {}) if isinstance(payload.get("user"), dict) else {}
        plan = membership.get("level") if isinstance(membership, dict) else None
        if not plan:
            plan = {100: "TYPE_PURCHASE", 1024: "ANDANTE", 2048: "MODERATO", 7168: "ALLEGRETTO"}.get(weekly_limit)
        return ProviderResult(provider="kimi-coding", status="ok", source=source, windows=windows, plan=str(plan).upper() if plan else None)

    # -------------------------------------------------------------------------
    # xAI OAuth (Grok subscription account + shared weekly usage pool)
    # -------------------------------------------------------------------------

    def probe_xai_oauth(self) -> ProviderResult:
        """Probe xAI OAuth for weekly usage, identity, and subscription state.

        The legacy /rest/rate-limits endpoint still rejects OAuth bearer
        tokens. Grok's Usage UI now reads the shared subscription pool from a
        gRPC-web billing endpoint that accepts the same OAuth token.
        """
        token = self._xai_oauth_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "provider-usage-dashboard",
        }
        # /v1/me — account validity. Keep this best-effort because usage and
        # subscription endpoints are independently useful.
        try:
            http_json(XAI_ME_URL, headers=headers, timeout=self.config.timeout_seconds)
        except Exception:
            pass

        # grok.com/rest/subscriptions — plan and subscription billing period.
        sub_payload: dict[str, Any] = {}
        try:
            sub_payload = http_json(XAI_SUBSCRIPTIONS_URL, headers=headers, timeout=self.config.timeout_seconds) or {}
        except Exception:
            pass

        # Shared weekly pool shown by grok.com Settings -> Usage. This is an
        # undocumented UI API, so failure must not erase valid plan data.
        weekly_window: Window | None = None
        try:
            weekly_window = self._xai_weekly_window(token)
        except Exception:
            pass

        sub_list = sub_payload.get("subscriptions") if isinstance(sub_payload, dict) else None
        windows: list[Window] = [weekly_window] if weekly_window else []
        plan: str | None = None
        if isinstance(sub_list, list) and sub_list:
            active = next((s for s in sub_list if isinstance(s, dict) and s.get("status") == "SUBSCRIPTION_STATUS_ACTIVE"), None)
            if active:
                tier = str(active.get("tier") or "")
                if tier.startswith("SUBSCRIPTION_TIER_"):
                    tier = tier[len("SUBSCRIPTION_TIER_"):]
                plan = tier.replace("_", " ").title()
                bp_end = active.get("billingPeriodEnd") or active.get("billing_period_end")
                if bp_end:
                    reset_at_str = normalize_iso(str(bp_end))
                    reset_at_dt = parse_iso(str(bp_end))
                    days_remaining = (reset_at_dt - datetime.now(timezone.utc)).days if reset_at_dt else None
                    days_str = f"{days_remaining}d" if days_remaining is not None else ""
                    windows.append(
                        Window(
                            label="billing_period",
                            remaining_text=f"{days_str} remaining" if days_str else "active",
                            percent_remaining=None,
                            percent_used=None,
                            reset_at=reset_at_str,
                            reset_text=relative_reset_text(reset_at_str) if reset_at_str else None,
                            meta={"billing_period_end": str(bp_end)},
                        )
                    )

        if not windows:
            windows.append(Window(label="account", remaining_text="see details"))

        return ProviderResult(
            provider="xai-oauth",
            status="ok",
            source="oauth_usage_api+account_api" if weekly_window else "oauth_account_api",
            windows=windows,
            plan=plan,
            error=None,
        )

    def _xai_weekly_window(self, token: str) -> Window:
        raw, response_headers = http_bytes(
            XAI_CREDITS_URL,
            method="POST",
            body=b"\x00\x00\x00\x00\x00",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://grok.com",
                "Referer": "https://grok.com/?_s=usage",
                "Accept": "*/*",
                "Content-Type": "application/grpc-web+proto",
                "x-grpc-web": "1",
                "x-user-agent": "connect-es/2.1.1",
                "User-Agent": "provider-usage-dashboard",
            },
            timeout=self.config.timeout_seconds,
        )
        grpc_status = next(
            (str(value).strip() for key, value in response_headers.items() if str(key).lower() == "grpc-status"),
            None,
        )
        if grpc_status not in (None, "0"):
            raise ProbeError(f"xAI usage API returned gRPC status {grpc_status}")
        usage = parse_xai_billing_grpc_web(raw)
        used = round(float(usage["percent_used"]), 1)
        remaining = round(max(0.0, 100.0 - used), 1)
        reset_at = usage.get("window_end")
        return Window(
            label="weekly",
            percent_remaining=remaining,
            percent_used=used,
            remaining_text=f"{remaining:.1f}% left",
            reset_at=reset_at,
            reset_text=relative_reset_text(reset_at),
            meta={
                "window_start": usage.get("window_start"),
                "window_end": reset_at,
                "shared_pool": True,
                "surface": "grok_build_billing_grpc_web",
            },
        )

    def _xai_oauth_token(self) -> str:
        # Try Hermes auth store first (xai-runtime creds)
        cred = self.auth.first_credential("xai-oauth")
        if isinstance(cred, dict) and cred.get("access_token"):
            return str(cred["access_token"])
        # Optional fallback: resolve via a locally installed Hermes xai_http helper.
        import sys as _sys

        helper_path = os.environ.get("HERMES_AGENT_PATH")
        if helper_path:
            _sys.path.insert(0, expand_path(helper_path))
        try:
            from tools.xai_http import resolve_xai_http_credentials
        except Exception:
            raise ProbeError("no xAI OAuth token available; set xai-oauth in auth_path or HERMES_AGENT_PATH for helper fallback")
        creds = resolve_xai_http_credentials()
        token = creds.get("access_token") or creds.get("xai_api_key") or ""
        if not token:
            raise ProbeError("xAI OAuth resolution returned no token")
        return token

    def _load_kimi_token(self) -> tuple[str, str]:
        env_token = (os.environ.get("KIMI_AUTH_TOKEN") or "").strip()
        if env_token:
            return env_token, "env"
        credential_path = Path(self.config.kimi_credentials_path).expanduser()
        if credential_path.exists():
            credential = json.loads(credential_path.read_text(encoding="utf-8"))
            expires_at = as_float(credential.get("expires_at")) or 0.0
            access_token = str(credential.get("access_token") or "")
            refresh_token = str(credential.get("refresh_token") or "")
            now = time.time()
            if refresh_token and (not access_token or expires_at - now < 300):
                refreshed = self._refresh_kimi_token(refresh_token)
                credential.update(refreshed)
                credential_path.parent.mkdir(parents=True, exist_ok=True)
                credential_path.write_text(json.dumps(credential, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                access_token = str(credential.get("access_token") or "")
            if access_token:
                return access_token, "kimi-cli"
        credential = self.auth.first_credential("kimi-coding")
        if isinstance(credential, dict) and credential.get("access_token"):
            return str(credential.get("access_token")), "dev-auth"
        raise ProbeError("no Kimi token available")

    def _refresh_kimi_token(self, refresh_token: str) -> dict[str, Any]:
        payload = http_json(
            KIMI_OAUTH_REFRESH_URL,
            method="POST",
            headers={"Accept": "application/json"},
            body=urlencode(
                {
                    "client_id": KIMI_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                }
            ).encode("utf-8"),
            timeout=self.config.timeout_seconds,
        )
        access_token = payload.get("access_token")
        if not access_token:
            raise ProbeError("Kimi token refresh returned no access token")
        expires_in = as_float(payload.get("expires_in")) or 0.0
        return {
            "access_token": access_token,
            "refresh_token": payload.get("refresh_token") or refresh_token,
            "expires_at": time.time() + expires_in,
            "expires_in": expires_in,
            "scope": payload.get("scope"),
            "token_type": payload.get("token_type"),
        }

    def _kimi_window(self, label: str, detail: dict[str, Any]) -> Window | None:
        limit = as_int(detail.get("limit"))
        used = as_int(detail.get("used"))
        remaining = as_int(detail.get("remaining"))
        if remaining is None and limit is not None and used is not None:
            remaining = max(0, limit - used)
        if limit is None or limit <= 0 or remaining is None:
            return None
        percent = round(remaining / limit * 100, 1)
        reset_at = normalize_iso(detail.get("resetTime") or detail.get("reset_time"))
        remaining_text = f"{percent:.1f}% left"
        counts_text = None
        if remaining is not None and limit is not None:
            counts_text = f"{remaining}/{limit} requests"
        return Window(
            label=label,
            percent_remaining=percent,
            percent_used=round(100 - percent, 1),
            remaining_text=remaining_text,
            reset_at=reset_at,
            reset_text=relative_reset_text(reset_at),
            meta={"remaining": remaining, "limit": limit, "counts_text": counts_text},
        )

    def _zai_label(self, quota_type: str, unit: Any) -> str | None:
        if quota_type == "TIME_LIMIT":
            return "mcp"
        if quota_type != "TOKENS_LIMIT":
            return None
        if unit in (None, 3):
            return "session"
        if unit == 6:
            return "weekly"
        if unit == 7:
            return "monthly"
        return f"tokens-unit-{unit}"

    def _require_credential(self, provider: str) -> dict[str, Any]:
        credential = self.auth.first_credential(provider)
        if credential is None:
            raise ProbeError(f"no credential found for {provider}")
        return credential

    def _load_claude_credential(self) -> tuple[dict[str, Any], str]:
        claude_path = Path(self.config.claude_credentials_path)
        if claude_path.exists():
            try:
                raw = json.loads(claude_path.read_text(encoding="utf-8"))
                oauth = raw.get("claudeAiOauth") if isinstance(raw, dict) else None
                if isinstance(oauth, dict) and oauth.get("accessToken"):
                    credential = {
                        "id": "claude-cli",
                        "access_token": oauth.get("accessToken"),
                        "refresh_token": oauth.get("refreshToken"),
                        "expires_at_ms": oauth.get("expiresAt"),
                        "subscriptionType": oauth.get("subscriptionType"),
                    }
                    return credential, "claude-cli"
            except Exception:
                pass
        return self._require_credential("anthropic"), "dev-auth"

    def _require_access_token(self, credential: dict[str, Any], provider: str) -> str:
        token = credential.get("access_token")
        if token:
            return str(token)
        token = self.auth.resolve_env_credential(credential)
        if token:
            return str(token)
        raise ProbeError(f"credential for {provider} has no access token")

    def _refresh_codex_if_needed(self, credential: dict[str, Any]) -> dict[str, Any]:
        access_token = credential.get("access_token")
        token_exp = as_int(jwt_claim(access_token, "exp"))
        if token_exp is not None:
            expires_at = datetime.fromtimestamp(token_exp, tz=timezone.utc)
            if expires_at - datetime.now(timezone.utc) > timedelta(minutes=10):
                return credential

        last_refresh = parse_iso(credential.get("last_refresh"))
        # Only use last_refresh as a fallback freshness hint when the token has no
        # decodable expiry. If the JWT says the access token is expired/near-expiry,
        # refresh now; otherwise stale last_refresh metadata can mask a dead token.
        if token_exp is None and last_refresh and datetime.now(timezone.utc) - last_refresh < timedelta(days=8):
            return credential

        refresh_token = credential.get("refresh_token")
        if not refresh_token:
            return credential
        body = urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": CODEX_CLIENT_ID,
                "refresh_token": str(refresh_token),
            }
        ).encode("utf-8")
        payload = http_json(
            CODEX_REFRESH_URL,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            body=body,
            timeout=self.config.timeout_seconds,
        )
        access_token = payload.get("access_token")
        if not access_token:
            return credential
        updates = {
            "access_token": access_token,
            "refresh_token": payload.get("refresh_token") or refresh_token,
            "last_refresh": iso_now(),
            "last_status": "ok",
            "last_error_code": None,
            "last_error_reason": None,
            "last_error_message": None,
        }
        if str(credential.get("id") or "").startswith("__provider__:"):
            credential.update(updates)
            return credential
        return self.auth.update_credential(
            "openai-codex",
            str(credential["id"]),
            updates,
            auth_path=credential.get("__auth_path"),
        )

    def _refresh_claude_if_needed(self, credential: dict[str, Any], source: str) -> dict[str, Any]:
        expires_at_ms = as_int(credential.get("expires_at_ms"))
        if expires_at_ms is not None:
            expires_at = datetime.fromtimestamp(expires_at_ms / 1000.0, tz=timezone.utc)
            if expires_at - datetime.now(timezone.utc) > timedelta(minutes=5):
                return credential
        refresh_token = credential.get("refresh_token")
        if not refresh_token:
            return credential
        try:
            payload = http_json(
                CLAUDE_REFRESH_URL,
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body={
                    "grant_type": "refresh_token",
                    "refresh_token": str(refresh_token),
                    "client_id": CLAUDE_CLIENT_ID,
                    "scope": CLAUDE_SCOPES,
                },
                timeout=self.config.timeout_seconds,
            )
        except Exception:
            return credential
        access_token = payload.get("access_token")
        if not access_token:
            return credential
        expires_in = as_int(payload.get("expires_in")) or 0
        updates = {
            "access_token": access_token,
            "refresh_token": payload.get("refresh_token") or refresh_token,
            "expires_at_ms": int((time.time() + expires_in) * 1000) if expires_in else credential.get("expires_at_ms"),
            "last_status": "ok",
            "last_error_code": None,
            "last_error_reason": None,
            "last_error_message": None,
        }
        if source == "dev-auth" and credential.get("id") != "claude-cli":
            return self.auth.update_credential(
                "anthropic",
                str(credential["id"]),
                updates,
                auth_path=credential.get("__auth_path"),
            )
        credential.update(updates)
        return credential


HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
  <title>Provider Usage Dashboard</title>
  <style>
    :root {
      --bg-0: #0A0D12;
      --bg-1: #0F141B;
      --bg-2: #141A23;
      --bg-3: #1A212C;
      --bg-inset: #07090D;
      --line-1: #1E2530;
      --line-2: #283040;
      --line-soft: #161C25;
      --fg-0: #E7ECF3;
      --fg-1: #B5BDCB;
      --fg-2: #7A8494;
      --fg-3: #525C6B;
      --accent: #5BB3C2;
      --accent-dim: #3E8995;
      --accent-bg: rgba(91,179,194,0.10);
      --accent-line: rgba(91,179,194,0.28);
      --ok: #6FAE82;
      --ok-bg: rgba(111,174,130,0.10);
      --ok-line: rgba(111,174,130,0.22);
      --warn: #D4A24A;
      --warn-bg: rgba(212,162,74,0.10);
      --warn-line: rgba(212,162,74,0.30);
      --crit: #D86A60;
      --crit-bg: rgba(216,106,96,0.10);
      --crit-line: rgba(216,106,96,0.30);
      --stale: #8B95A8;
      --stale-bg: rgba(139,149,168,0.08);
      --stale-line: rgba(139,149,168,0.22);
      --sans: 'Inter', 'IBM Plex Sans', ui-sans-serif, system-ui, sans-serif;
      --mono: 'JetBrains Mono', 'SFMono-Regular', ui-monospace, monospace;
      --r-sm: 4px;
      --r-md: 6px;
      --r-lg: 8px;
      --shadow: 0 18px 44px rgba(0,0,0,0.26);
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
      background: var(--bg-0);
      color: var(--fg-0);
      font-family: var(--sans);
      font-size: 14px;
      line-height: 1.45;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }
    body::before {
      content: '';
      position: fixed; inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(var(--line-soft) 1px, transparent 1px),
        linear-gradient(90deg, var(--line-soft) 1px, transparent 1px);
      background-size: 48px 48px;
      opacity: 0.28;
      mask-image: radial-gradient(ellipse 1200px 700px at 50% 0%, #000 30%, transparent 82%);
      z-index: 0;
    }
    a { color: inherit; }
    code, .mono { font-family: var(--mono); }
    .topbar {
      position: relative; z-index: 2;
      height: 44px;
      border-bottom: 1px solid var(--line-1);
      background: linear-gradient(180deg, #0C1118 0%, #0A0D12 100%);
      display: flex; align-items: center; gap: 16px;
      padding: 0 20px;
      font-family: var(--mono);
      font-size: 12px;
      color: var(--fg-2);
    }
    .topbar__brand { display: flex; align-items: center; gap: 8px; color: var(--fg-0); font-weight: 500; }
    .topbar__brand svg { color: var(--accent); }
    .topbar__sep { width: 1px; height: 16px; background: var(--line-2); }
    .topbar__right { margin-left: auto; display:flex; align-items:center; gap:18px; }
    .topbar__dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 3px rgba(111,174,130,0.12); }
    .page { position: relative; z-index: 1; max-width: 1240px; margin: 0 auto; padding: 34px 24px 80px; }
    .page-head { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; margin-bottom: 28px; flex-wrap: wrap; }
    .page-head__title { font-size: 22px; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 6px; }
    .page-head__sub { color: var(--fg-2); font-size: 13px; margin: 0; max-width: 64ch; }
    .page-head__actions { display:flex; align-items:center; gap:10px; flex-wrap: wrap; }
    .updated, .pill, .btn, .microchip {
      border: 1px solid var(--line-1);
      border-radius: var(--r-md);
      background: var(--bg-1);
    }
    .updated { display:flex; align-items:center; gap:8px; padding: 6px 10px; font-family: var(--mono); font-size: 11px; color: var(--fg-2); }
    .updated__dot { width: 5px; height: 5px; border-radius: 50%; background: var(--ok); }
    .btn {
      height: 30px; padding: 0 12px; color: var(--fg-1); font-size: 12px; font-weight: 500; cursor:pointer;
      display:inline-flex; align-items:center; gap:7px; transition: all .12s ease;
    }
    .btn:hover { background: var(--bg-2); color: var(--fg-0); border-color: var(--line-2); }
    .btn--primary { background: var(--accent-bg); color: var(--accent); border-color: var(--accent-line); }
    .btn--primary:hover { background: rgba(91,179,194,0.16); color: var(--accent); border-color: var(--accent); }
    .summary { display:flex; gap:10px; flex-wrap: wrap; margin: 0 0 28px; }
    .pill { padding: 8px 12px; font-family: var(--mono); font-size: 11px; color: var(--fg-1); }
    .pill b { color: var(--fg-0); }
    .pill--ok { border-color: var(--ok-line); background: var(--ok-bg); color: #bdd9c6; }
    .pill--warn { border-color: var(--warn-line); background: var(--warn-bg); color: #e7ca95; }
    .pill--crit { border-color: var(--crit-line); background: var(--crit-bg); color: #edb1ab; }
    .section-title {
      font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: 0.14em;
      color: var(--fg-3); margin: 0 0 12px; display:flex; align-items:center; gap:12px;
    }
    .section-title::after { content:''; flex:1; height:1px; background: var(--line-soft); }
    .pace-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; margin-bottom: 34px; }
    .pace-card, .provider-card {
      border: 1px solid var(--line-1);
      background: radial-gradient(900px 200px at 0% 0%, rgba(91,179,194,0.04), transparent 60%), var(--bg-1);
      border-radius: var(--r-lg);
      box-shadow: var(--shadow);
    }
    .pace-card { padding: 18px 18px 16px; }
    .pace-card--ahead { border-color: var(--warn-line); }
    .pace-card--critical { border-color: var(--crit-line); }
    .pace-head { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }
    .pace-name { display:flex; align-items:center; gap:10px; font-weight: 600; }
    .glyph {
      width: 24px; height: 24px; border-radius: 6px; display:inline-flex; align-items:center; justify-content:center;
      background: var(--bg-3); border:1px solid var(--line-2); color: var(--fg-1); font-family: var(--mono); font-size: 11px;
    }
    .pace-left { font-size: 20px; font-weight: 600; letter-spacing: -0.02em; }
    .muted { color: var(--fg-2); }
    .micro { color: var(--fg-3); font-size: 11px; }
    .pace-meta { display:flex; gap:8px; flex-wrap:wrap; margin-top:6px; }
    .snap {
      display:inline-flex; align-items:center; gap:6px; padding: 4px 8px; border-radius: 999px; border:1px solid var(--line-2);
      font-family: var(--mono); font-size: 10px; text-transform: uppercase; letter-spacing: .05em;
    }
    .snap svg { width: 9px; height: 9px; }
    .snap--live { color: #bdd9c6; border-color: var(--ok-line); background: var(--ok-bg); }
    .snap--cached { color: #c5cbd7; border-color: var(--stale-line); background: var(--stale-bg); }
    .snap--probe { color: #e7ca95; border-color: var(--warn-line); background: var(--warn-bg); }
    .snap--error { color: #edb1ab; border-color: var(--crit-line); background: var(--crit-bg); }
    .pacebar, .meter {
      position: relative; width: 100%; overflow:hidden; background: var(--bg-inset); border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.05);
    }
    .pacebar { height: 14px; margin: 12px 0 10px; }
    .pacebar__fill, .meter__fill {
      position:absolute; inset:0 auto 0 0; background: linear-gradient(90deg, var(--accent-dim), var(--accent));
    }
    .pacebar__marker {
      position:absolute; top:-3px; bottom:-3px; width:3px; background:#EEF3F8; border-radius:999px;
      box-shadow: 0 0 0 2px rgba(8,17,31,0.82);
    }
    .pace-card--ahead .pacebar__fill, .provider-card--warn .meter__fill { background: linear-gradient(90deg, #AA7B2B, var(--warn)); }
    .pace-card--critical .pacebar__fill, .provider-card--crit .meter__fill { background: linear-gradient(90deg, #A34A44, var(--crit)); }
    .pace-foot { display:flex; justify-content:space-between; gap:12px; font-size: 11px; color: var(--fg-2); }
    .callout {
      margin-top: 10px; padding: 10px 12px; border-radius: var(--r-md); border:1px solid var(--line-1);
      font-size: 12px; color: var(--fg-2); background: rgba(255,255,255,0.03);
    }
    .callout--ahead { border-color: var(--warn-line); background: var(--warn-bg); color: #ecd6ac; }
    .callout--critical { border-color: var(--crit-line); background: var(--crit-bg); color: #efc0bb; }
    .providers-panel {
      border: 1px solid var(--line-1);
      border-radius: var(--r-lg);
      background: radial-gradient(900px 200px at 0% 0%, rgba(91,179,194,0.04), transparent 60%), var(--bg-1);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .providers__head {
      display:grid;
      grid-template-columns: 240px 1fr 1fr 160px;
      gap: 24px;
      padding: 12px 22px;
      border-bottom: 1px solid var(--line-1);
      font-family: var(--mono);
      font-size: 10px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--fg-3);
      background: rgba(255,255,255,0.015);
    }
    .providers__head .col-right { text-align: right; }
    .prow {
      display:grid;
      grid-template-columns: 240px 1fr 1fr 160px;
      gap: 24px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line-soft);
      align-items: center;
      transition: background 0.12s ease;
      position: relative;
    }
    .prow:last-of-type { border-bottom: none; }
    .prow:hover { background: var(--bg-2); }
    .prow--crit::before,
    .prow--warn::before {
      content: '';
      position: absolute;
      left: 0; top: 0; bottom: 0;
      width: 2px;
    }
    .prow--crit::before { background: var(--crit); }
    .prow--warn::before { background: var(--warn); }
    .prow--stale { background: rgba(255,255,255,0.005); }
    .prow__id { display:flex; flex-direction:column; gap:8px; }
    .prow__name { display:flex; align-items:center; gap:10px; font-size:15px; font-weight:500; color:var(--fg-0); letter-spacing:-0.005em; }
    .prow__name .glyph {
      width:22px; height:22px; display:flex; align-items:center; justify-content:center;
      background: var(--bg-2); border:1px solid var(--line-1); border-radius: var(--r-sm);
      font-family: var(--mono); font-size:11px; font-weight:600; color: var(--fg-1); flex-shrink:0;
    }
    .prow__meta { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
    .badge {
      display:inline-flex; align-items:center; gap:6px; height:20px; padding:0 8px; border-radius:3px;
      font-family: var(--mono); font-size:10.5px; font-weight:500; letter-spacing:0.04em; text-transform:uppercase;
      border:1px solid var(--line-2); background: var(--bg-2); color: var(--fg-1); white-space: nowrap;
    }
    .badge__dot { width:5px; height:5px; border-radius:50%; background: currentColor; }
    .badge--ok { color: var(--ok); border-color: var(--ok-line); background: var(--ok-bg); }
    .badge--warn, .badge--rate_limited, .badge--auth_required { color: var(--warn); border-color: var(--warn-line); background: var(--warn-bg); }
    .badge--error { color: var(--crit); border-color: var(--crit-line); background: var(--crit-bg); }
    .badge--stale { color: var(--stale); border-color: var(--stale-line); background: var(--stale-bg); }
    .snap {
      display:inline-flex; align-items:center; gap:5px; height:18px; padding:0 6px 0 5px; border-radius:3px;
      font-family: var(--mono); font-size:10px; letter-spacing:0.04em; color: var(--fg-2); background: transparent;
      border:1px dashed var(--line-2); white-space: nowrap;
    }
    .snap svg { width: 9px; height: 9px; color: var(--fg-3); }
    .snap--live { color: var(--accent); border:1px solid var(--accent-line); background: var(--accent-bg); }
    .snap--live svg { color: var(--accent); }
    .snap--cached { color: var(--stale); border:1px solid var(--stale-line); background: var(--stale-bg); }
    .snap--cached svg { color: var(--stale); }
    .snap--probe { color: var(--warn); border:1px solid var(--warn-line); background: var(--warn-bg); }
    .snap--error { color: var(--crit); border:1px solid var(--crit-line); background: var(--crit-bg); }
    .plan { font-family: var(--mono); font-size:10.5px; color: var(--fg-2); padding:0 4px; }
    .plan b { color: var(--fg-1); font-weight:500; }
    .pcell { display:flex; flex-direction:column; gap:8px; min-width:0; }
    .pcell__head {
      display:flex; align-items:baseline; justify-content:space-between; gap:12px;
      font-family: var(--mono); font-size:11px; color: var(--fg-2);
    }
    .pcell__label { text-transform:uppercase; letter-spacing:0.1em; font-size:10px; color: var(--fg-3); }
    .pcell__values { color: var(--fg-1); }
    .pcell__values .pct { color: var(--fg-0); font-size:13px; font-weight:500; }
    .pcell__values .pct--warn { color: var(--warn); }
    .pcell__values .pct--crit { color: var(--crit); }
    .pcell__values .sep { color: var(--fg-3); margin: 0 4px; }
    .pcell__values .used { color: var(--fg-2); }
    .pcell__subrow {
      display:flex; align-items:center; justify-content:space-between; gap:12px;
      font-family: var(--mono); font-size:10px; color: var(--fg-3);
      min-height: 18px;
    }
    .pcell__target { color: var(--fg-3); white-space: nowrap; }
    .pace-chip {
      display:inline-flex; align-items:center; gap:6px; height:18px; padding:0 7px; border-radius:999px;
      border:1px solid var(--line-2); background: rgba(255,255,255,0.02); color: var(--fg-2);
      text-transform:uppercase; letter-spacing:0.05em; white-space: nowrap;
    }
    .pace-chip::before { content:''; width:5px; height:5px; border-radius:50%; background: currentColor; opacity:0.9; }
    .pace-chip--ok { color: var(--ok); border-color: var(--ok-line); background: var(--ok-bg); }
    .pace-chip--warn { color: var(--warn); border-color: var(--warn-line); background: var(--warn-bg); }
    .pace-chip--crit { color: var(--crit); border-color: var(--crit-line); background: var(--crit-bg); }
    .pace-chip--stale { color: var(--stale); border-color: var(--stale-line); background: var(--stale-bg); }
    .pacebar {
      position:relative; height:6px; background: var(--bg-inset); border-radius:999px; overflow:visible;
      border:1px solid var(--line-soft);
    }
    .pacebar__fill {
      position:absolute; left:0; top:0; bottom:0; background: linear-gradient(90deg, var(--accent-dim), var(--accent));
      border-radius:999px;
    }
    .pacebar--warn .pacebar__fill { background: linear-gradient(90deg, #B8862F, var(--warn)); }
    .pacebar--crit .pacebar__fill { background: linear-gradient(90deg, #B0524A, var(--crit)); }
    .pacebar--stale .pacebar__fill { background: repeating-linear-gradient(135deg, var(--stale) 0 4px, rgba(139,149,168,0.45) 4px 8px); opacity:0.72; }
    .pacebar__marker {
      position:absolute; top:-4px; bottom:-4px; width:2px; background:#EEF3F8; border-radius:999px;
      box-shadow: 0 0 0 2px rgba(8,17,31,0.78); opacity:0.9; overflow:visible;
    }
    .pacebar__marker span {
      position:absolute; left:50%; bottom:100%; transform:translate(-50%, -3px);
      padding:1px 4px; border-radius:999px; border:1px solid rgba(238,243,248,0.35);
      background:rgba(8,17,31,0.92); color:#EEF3F8; font-family:var(--mono); font-size:8px;
      line-height:1.2; text-transform:uppercase; letter-spacing:0.06em; white-space:nowrap;
    }
    .pcell__resets { font-family: var(--mono); font-size:10.5px; color: var(--fg-3); margin-top:1px; }
    .prow__reset {
      text-align:right; font-family: var(--mono); font-size:11px; color: var(--fg-2); line-height:1.55;
    }
    .prow__reset b { color: var(--fg-1); font-weight:500; }
    .prow__reset .micro { color: var(--fg-3); font-size:10px; }
    .provider-error {
      grid-column: 1 / -1; margin-top: 10px; padding: 10px 12px; border-radius: var(--r-md); border:1px solid var(--crit-line);
      background: rgba(216,106,96,0.08); color: #efc0bb; white-space: pre-wrap;
    }
    details.secondary {
      grid-column: 1 / -1; margin-top: 10px; padding-top: 12px; border-top: 1px dashed var(--line-soft);
    }
    details.secondary > summary {
      cursor:pointer; user-select:none; color: var(--fg-2); font-family: var(--mono); font-size:10.5px;
      letter-spacing:0.06em; text-transform:uppercase; list-style:none;
    }
    details.secondary > summary::-webkit-details-marker { display:none; }
    details.secondary[open] > summary { color: var(--fg-1); }
    .secondary__list { display:grid; gap:8px; margin-top:10px; opacity:0.78; }
    .secondary__item {
      display:grid; grid-template-columns: 240px 1fr 110px; gap:16px; align-items:center; padding:6px 0;
      font-size:12px; color: var(--fg-2);
    }
    .secondary__item .pacebar { height:4px; }
    .secondary__item .pacebar__marker { display:none; }
    .secondary__item__name {
      font-family: var(--mono); font-size:11px; color: var(--fg-2); display:flex; align-items:center; gap:8px;
    }
    .secondary__item__name .dim { color: var(--fg-3); }
    .secondary__item__pct { text-align:right; font-family: var(--mono); color: var(--fg-1); }
    .footer { margin-top: 22px; color: var(--fg-3); font-size: 12px; }
    .empty { color: var(--fg-2); }
    @media (max-width: 980px) {
      .providers__head { display:none; }
      .prow { grid-template-columns: 1fr; gap: 14px; }
      .prow__reset { text-align:left; }
      .secondary__item { grid-template-columns: 1fr; gap:8px; }
      .pace-grid { grid-template-columns: 1fr; }
      .topbar { flex-wrap: wrap; height: auto; padding: 10px 16px; }
    }
  </style>
</head>
<body>
  <div class=\"topbar\">
    <div class=\"topbar__brand\">
      <svg width=\"12\" height=\"12\" viewBox=\"0 0 12 12\" fill=\"none\"><path d=\"M1 6h10M6 1v10\" stroke=\"currentColor\" stroke-width=\"1.2\" stroke-linecap=\"round\"/></svg>
      <span>Provider Usage Dashboard</span>
    </div>
    <div class=\"topbar__sep\"></div>
    <div>lane <span class=\"mono\">dev-profile</span></div>
    <div class=\"topbar__right\">
      <div><span class=\"topbar__dot\"></span> LAN</div>
      <div id=\"topHost\">Loading…</div>
    </div>
  </div>
  <main class=\"page\">
    <header class=\"page-head\">
      <div>
        <h1 class=\"page-head__title\">Provider Usage Dashboard</h1>
        <p class=\"page-head__sub\">Live LAN view of 5h and weekly quotas from the dev-profile credential pool.</p>
      </div>
      <div class=\"page-head__actions\">
        <div class=\"updated\"><span class=\"updated__dot\"></span><span id=\"updatedText\">Loading…</span></div>
        <button class=\"btn btn--primary\" id=\"reloadBtn\" type=\"button\">Reload</button>
      </div>
    </header>
    <div class=\"summary\" id=\"summary\"></div>
    <h2 class=\"section-title\">Providers</h2>
    <section class=\"providers\" id=\"cards\"></section>
    <div class=\"footer\">Endpoint: <code>/api/usage</code> · Health: <code>/health</code> · Browser auto-refresh: disabled</div>
  </main>
<script>
const PROVIDER_META = {
  'minimax': { name: 'MiniMax', glyph: 'Mi' },
  'zai': { name: 'Z.ai', glyph: 'Z' },
  'openai-codex': { name: 'OpenAI Codex', glyph: 'OA' },
  'anthropic': { name: 'Claude', glyph: 'Cl' },
  'kimi-coding': { name: 'Kimi', glyph: 'Ki' },
  'xai-oauth': { name: 'xAI OAuth', glyph: 'xA' }
};
function esc(value) {
  return String(value ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
}
function pct(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${Number(value).toFixed(1)}%`;
}
function providerMeta(provider) {
  return PROVIDER_META[provider.provider] || { name: provider.provider, glyph: provider.provider.slice(0, 2).toUpperCase() };
}
function snapshotInfo(provider) {
  const source = String(provider.source || '');
  if (provider.status === 'stale' || source.includes('cache')) return { cls: 'cached', label: source.includes('statusline') ? 'cached · statusline' : 'cached' };
  if (provider.status === 'ok') return { cls: 'live', label: source.includes('statusline') ? 'live · statusline' : 'live' };
  if (provider.status === 'rate_limited' || provider.status === 'auth_required') return { cls: 'probe', label: provider.status.replace('_', ' ') };
  return { cls: 'error', label: 'probe only' };
}
function countsText(window) {
  const meta = window.meta || {};
  if (meta.counts_text) return meta.counts_text;
  if (meta.remaining !== undefined && meta.limit !== undefined) return `${meta.remaining}/${meta.limit} requests`;
  if (meta.used_count !== undefined && meta.total !== undefined) return `${meta.used_count}/${meta.total} used`;
  return '';
}
function aliasText(window) {
  const aliases = (window.meta || {}).aliases || [];
  return Array.isArray(aliases) && aliases.length ? aliases.join(', ') : '';
}
function windowByLabel(provider, label) {
  const windows = provider.windows || [];
  return windows.find(w => w.label === label) || windows.find(w => String(w.label || '').startsWith(label));
}
function weeklyWindowSpan(window) {
  const meta = window.meta || {};
  const end = meta.window_end || window.reset_at;
  if (!end) return null;
  const endMs = Date.parse(end);
  if (!Number.isFinite(endMs)) return null;
  const start = meta.window_start ? Date.parse(meta.window_start) : endMs - (7 * 24 * 60 * 60 * 1000);
  if (!Number.isFinite(start) || start >= endMs) return null;
  return { start, end: endMs };
}
function weeklySummary(provider) {
  const weekly = windowByLabel(provider, 'weekly');
  if (!weekly || weekly.percent_used === null || weekly.percent_used === undefined) return null;
  const span = weeklyWindowSpan(weekly);
  if (!span) return null;
  const now = Date.now();
  const progress = Math.max(0, Math.min(1, (now - span.start) / (span.end - span.start)));
  const expectedUsed = progress * 100;
  const used = Number(weekly.percent_used);
  const delta = used - expectedUsed;
  return { provider, weekly, used, expectedUsed, delta, remaining: weekly.percent_remaining, ahead: delta > 2.0, critical: delta > 15.0 };
}
function toneForWindow(provider, window) {
  const status = provider.status;
  const used = Number(window.percent_used ?? 0);
  if (status === 'stale') return 'stale';
  if (status === 'error' || status === 'auth_required') return 'crit';
  if (status === 'rate_limited') return 'warn';
  if (window.label === 'weekly' && used >= 95) return 'crit';
  if (window.label === 'weekly' && used >= 75) return 'warn';
  return 'ok';
}
function primaryWindows(provider) {
  const windows = provider.windows || [];
  const picked = [];
  const add = label => {
    const found = windowByLabel(provider, label);
    if (found && !picked.includes(found)) picked.push(found);
  };
  add('session');
  add('weekly');
  for (const window of windows) {
    if (picked.length >= 2) break;
    if (!picked.includes(window)) picked.push(window);
  }
  return picked;
}
function secondaryWindows(provider, primaries) {
  return (provider.windows || []).filter(window => !primaries.includes(window));
}
function badgeClass(status) {
  if (status === 'ok') return 'ok';
  if (status === 'stale') return 'stale';
  if (status === 'rate_limited' || status === 'auth_required') return 'warn';
  return 'error';
}
function providerCardTone(provider) {
  if (provider.status === 'stale') return 'provider-card--stale';
  const weekly = windowByLabel(provider, 'weekly');
  const summary = weeklySummary(provider);
  if (provider.status === 'error' || provider.status === 'auth_required') return 'provider-card--crit';
  if (summary && summary.critical) return 'provider-card--crit';
  if (provider.status === 'rate_limited' || (summary && summary.ahead)) return 'provider-card--warn';
  return '';
}
function renderSummary(data) {
  document.getElementById('summary').innerHTML = [
    `<div class=\"pill pill--ok\">OK <b>${esc(data.summary.ok)}/${esc(data.summary.total)}</b></div>`,
    `<div class=\"pill ${data.summary.degraded ? 'pill--warn' : ''}\">Degraded <b>${esc(data.summary.degraded)}</b></div>`,
    `<div class=\"pill ${data.summary.errors ? 'pill--crit' : ''}\">Errors <b>${esc(data.summary.errors)}</b></div>`,
    `<div class=\"pill\">LAN-local <b>${esc(data.host)}:${esc(data.port)}</b></div>`
  ].join('');
}
function windowExpectedUsed(window) {
  const meta = window.meta || {};
  const end = meta.window_end || window.reset_at;
  if (!end) return null;
  const endMs = Date.parse(end);
  if (!Number.isFinite(endMs)) return null;
  let spanMs = null;
  if (meta.window_start) {
    const startMs = Date.parse(meta.window_start);
    if (Number.isFinite(startMs) && startMs < endMs) spanMs = endMs - startMs;
  }
  const label = String(window.label || '');
  if (!spanMs && label.startsWith('session')) spanMs = 5 * 60 * 60 * 1000;
  if (!spanMs && label.startsWith('weekly')) spanMs = 7 * 24 * 60 * 60 * 1000;
  if (!spanMs) return null;
  const startMs = endMs - spanMs;
  const progress = Math.max(0, Math.min(1, (Date.now() - startMs) / spanMs));
  return progress * 100;
}
function toneClasses(tone) {
  return {
    bar: tone === 'warn' ? 'pacebar--warn' : tone === 'crit' ? 'pacebar--crit' : tone === 'stale' ? 'pacebar--stale' : '',
    pct: tone === 'warn' ? 'pct--warn' : tone === 'crit' ? 'pct--crit' : ''
  };
}
function providerState(provider) {
  const summary = weeklySummary(provider);
  if (provider.status === 'stale') return { badge: 'stale', label: 'cached' };
  if (provider.status === 'auth_required') return { badge: 'auth_required', label: 'auth required' };
  if (provider.status === 'rate_limited') return { badge: 'rate_limited', label: 'rate-limit risk' };
  if (provider.status === 'error') return { badge: 'error', label: 'error' };
  if (summary && summary.critical) return { badge: 'error', label: 'critical burn' };
  if (summary && summary.ahead) return { badge: 'warn', label: 'ahead of pace' };
  return { badge: 'ok', label: 'healthy' };
}
function paceCallout(summary) {
  if (!summary) return '';
  if (summary.critical) return `Critical burn · ${pct(summary.delta)} ahead of even weekly pace`;
  if (summary.ahead) return `Ahead of pace · ${pct(summary.delta)} over target`;
  return `On pace · ${pct(Math.abs(summary.delta))} buffer vs target`;
}
function paceState(summary, provider) {
  if (provider && provider.status === 'stale') return { cls: 'stale', label: 'cached' };
  if (!summary) return null;
  if (summary.critical) return { cls: 'crit', label: `critical ${pct(summary.delta)}` };
  if (summary.ahead) return { cls: 'warn', label: `ahead ${pct(summary.delta)}` };
  return { cls: 'ok', label: `on pace ${pct(Math.abs(summary.delta))}` };
}
function renderPrimaryCell(provider, window, fallbackLabel, summary) {
  if (!window) {
    return `
      <div class=\"pcell\">
        <div class=\"pcell__head\"><span class=\"pcell__label\">${esc(fallbackLabel)}</span><span class=\"pcell__values\">—</span></div>
        <div class=\"pacebar pacebar--stale\"><div class=\"pacebar__fill\" style=\"width:0%\"></div></div>
        <div class=\"pcell__resets\">No window data</div>
      </div>`;
  }
  const tone = toneForWindow(provider, window);
  const cls = toneClasses(tone);
  const used = Math.max(0, Math.min(100, Number(window.percent_used ?? 0)));
  const weekly = window.label === 'weekly';
  const pace = weekly ? paceState(summary, provider) : null;
  const marker = weekly && summary ? summary.expectedUsed : windowExpectedUsed(window);
  const counts = countsText(window);
  return `
    <div class=\"pcell\">
      <div class=\"pcell__head\">
        <span class=\"pcell__label\">${esc(window.label === 'session' ? '5h window' : window.label)}</span>
        <span class=\"pcell__values\"><span class=\"pct ${cls.pct}\">${pct(window.percent_used)} used</span><span class=\"sep\">·</span><span class=\"used\">${pct(window.percent_remaining)} left</span></span>
      </div>
      <div class=\"pcell__subrow\">${weekly ? `${pace ? `<span class=\"pace-chip pace-chip--${esc(pace.cls)}\">${esc(pace.label)}</span>` : '<span></span>'}<span class=\"pcell__target\">target ${esc(pct(summary?.expectedUsed))}</span>` : '<span></span>'}</div>
      <div class=\"pacebar ${cls.bar}\">
        <div class=\"pacebar__fill\" style=\"width:${used.toFixed(1)}%\"></div>
        ${marker !== null ? `<div class=\"pacebar__marker\" style=\"left:calc(${Math.max(0, Math.min(100, marker)).toFixed(1)}% - 1px)\"><span>${weekly ? 'pace' : 'time'}</span></div>` : ''}
      </div>
      <div class=\"pcell__resets\">${esc(counts || window.remaining_text || '')}${window.reset_text ? ` · ${esc(window.reset_text)}` : ''}${weekly && summary ? ` · pace marker = even-burn target` : ''}</div>
    </div>`;
}
function renderSecondaryWindow(provider, window) {
  const tone = toneForWindow(provider, window);
  const cls = toneClasses(tone);
  const aliases = aliasText(window);
  const used = Math.max(0, Math.min(100, Number(window.percent_used ?? 0)));
  return `
    <div class=\"secondary__item\">
      <div class=\"secondary__item__name\">
        <span>${esc(window.label)}</span>
        ${aliases ? `<span class=\"dim\">· ${esc(aliases)}</span>` : ''}
      </div>
      <div class=\"pacebar ${cls.bar}\"><div class=\"pacebar__fill\" style=\"width:${used.toFixed(1)}%\"></div></div>
      <div class=\"secondary__item__pct\">${pct(window.percent_used)} used</div>
    </div>`;
}
function renderResetColumn(provider, sessionWindow, weeklyWindow, secondaries) {
  const resetLabel = window => window.label === 'session' ? '5h' : (window.label === 'weekly' ? 'wk' : window.label);
  return `
    <div class=\"prow__reset\">
      ${sessionWindow ? `<div>${esc(resetLabel(sessionWindow))} · <b>${esc(sessionWindow.reset_text || '—')}</b></div>` : ''}
      ${weeklyWindow ? `<div>${esc(resetLabel(weeklyWindow))} · <b>${esc(weeklyWindow.reset_text || '—')}</b></div>` : ''}
      <div class=\"micro\">${secondaries.length ? `${secondaries.length} secondary window${secondaries.length === 1 ? '' : 's'}` : (provider.checked_at ? `checked ${esc(provider.checked_at)}` : (provider.source || ''))}</div>
    </div>`;
}
function renderProvider(provider) {
  const meta = providerMeta(provider);
  const snap = snapshotInfo(provider);
  const weeklyWindow = windowByLabel(provider, 'weekly');
  const primaryCandidates = primaryWindows(provider);
  const displayOnlyLabels = new Set(['account', 'billing_period']);
  const fallbackPrimaries = primaryCandidates.filter(window => window !== weeklyWindow && !displayOnlyLabels.has(window.label));
  const sessionWindow = windowByLabel(provider, 'session') || fallbackPrimaries.shift() || null;
  const secondWindow = weeklyWindow || fallbackPrimaries.shift() || null;
  const primaries = [sessionWindow, secondWindow].filter(Boolean);
  const secondaries = secondaryWindows(provider, primaries);
  const summary = weeklySummary(provider);
  const state = providerState(provider);
  const rowTone = providerCardTone(provider).replace('provider-card', 'prow');
  return `
    <article class=\"prow ${rowTone}\">
      <div class=\"prow__id\">
        <div class=\"prow__name\"><span class=\"glyph\">${esc(meta.glyph)}</span><span>${esc(meta.name)}</span></div>
        <div class=\"prow__meta\">
          <span class=\"badge badge--${esc(state.badge)}\"><span class=\"badge__dot\"></span>${esc(state.label)}</span>
          <span class=\"snap snap--${esc(snap.cls === 'degraded' ? 'probe' : snap.cls)}\">${esc(snap.label)}</span>
          ${provider.plan ? `<span class=\"plan\"><b>${esc(provider.plan)}</b></span>` : ''}
        </div>
      </div>
      ${renderPrimaryCell(provider, sessionWindow, '5h window', summary)}
      ${renderPrimaryCell(provider, secondWindow, 'weekly', summary)}
      ${renderResetColumn(provider, sessionWindow, secondWindow, secondaries)}
      ${provider.error ? `<div class=\"provider-error\">${esc(provider.error)}</div>` : ''}
      ${secondaries.length ? `<details class=\"secondary\"><summary>Show ${esc(secondaries.length)} secondary window${secondaries.length === 1 ? '' : 's'}</summary><div class=\"secondary__list\">${secondaries.map(window => renderSecondaryWindow(provider, window)).join('')}</div></details>` : ''}
    </article>`;
}
function renderProviders(data) {
  const providers = data.providers || [];
  document.getElementById('cards').innerHTML = providers.length ? `
    <div class=\"providers-panel\">
      <div class=\"providers__head\">
        <div>Provider</div>
        <div>5h / session</div>
        <div>Weekly</div>
        <div class=\"col-right\">Resets</div>
      </div>
      ${providers.map(renderProvider).join('')}
    </div>` : '<div class=\"empty\">No providers returned.</div>';
}
async function load() {

  const res = await fetch('/api/usage', { cache: 'no-store' });
  const data = await res.json();
  document.getElementById('topHost').textContent = `${data.host}:${data.port}`;
  document.getElementById('updatedText').textContent = data.generated_at;
  renderSummary(data);
  renderProviders(data);
}
document.getElementById('reloadBtn').addEventListener('click', () => load().catch(err => {
  document.getElementById('cards').innerHTML = `<div class=\"provider-error\">${esc(String(err))}</div>`;
}));
load().catch(err => {
  document.getElementById('cards').innerHTML = `<div class=\"provider-error\">${esc(String(err))}</div>`;
});
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    service: UsageService
    config: AppConfig

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._send_html()
            return
        if self.path == "/api/usage":
            self._send_json(self.service.collect_all())
            return
        if self.path == "/health":
            self._send_json({"status": "ok", "time": iso_now()})
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_html(self) -> None:
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_xai_billing_grpc_web(raw: bytes, now: datetime | None = None) -> dict[str, Any]:
    """Parse Grok's gRPC-web weekly-credit response without generated protobufs.

    xAI does not publish this UI protobuf schema. We intentionally extract only
    the stable fields needed by the dashboard: usage percent and period bounds.
    Unknown fields are ignored so additive schema changes remain harmless.
    """
    payloads: list[bytes] = []
    grpc_status: str | None = None
    index = 0
    framed = False
    while index + 5 <= len(raw):
        flags = raw[index]
        length = int.from_bytes(raw[index + 1:index + 5], "big")
        end = index + 5 + length
        if end > len(raw):
            break
        framed = True
        frame = raw[index + 5:end]
        if flags & 0x80:
            for line in frame.decode("utf-8", errors="replace").splitlines():
                key, separator, value = line.partition(":")
                if separator and key.strip().lower() == "grpc-status":
                    grpc_status = value.strip()
        else:
            payloads.append(frame)
        index = end
    if framed and index != len(raw):
        raise ProbeError("xAI usage API returned malformed gRPC-web frames")
    if grpc_status not in (None, "0"):
        raise ProbeError(f"xAI usage API returned gRPC status {grpc_status}")
    if not payloads and raw and raw[0] >> 3:
        payloads = [raw]
    if not payloads:
        raise ProbeError("xAI usage API returned no protobuf payload")

    fixed32: list[tuple[tuple[int, ...], float, int]] = []
    varints: list[tuple[tuple[int, ...], int]] = []
    order = 0

    def read_varint(data: bytes, offset: int) -> tuple[int | None, int]:
        value = 0
        shift = 0
        while offset < len(data) and shift < 64:
            byte = data[offset]
            offset += 1
            value |= (byte & 0x7F) << shift
            if byte & 0x80 == 0:
                return value, offset
            shift += 7
        return None, offset

    def scan(data: bytes, path: tuple[int, ...] = (), depth: int = 0) -> None:
        nonlocal order
        offset = 0
        while offset < len(data):
            field_start = offset
            key, offset = read_varint(data, offset)
            if not key:
                offset = field_start + 1
                continue
            field_number, wire_type = key >> 3, key & 0x07
            field_path = path + (field_number,)
            if wire_type == 0:
                value, offset = read_varint(data, offset)
                if value is not None:
                    varints.append((field_path, value))
            elif wire_type == 1:
                if offset + 8 > len(data):
                    break
                offset += 8
            elif wire_type == 2:
                length, offset = read_varint(data, offset)
                if length is None or length > len(data) - offset:
                    offset = field_start + 1
                    continue
                nested = data[offset:offset + length]
                if depth < 4:
                    scan(nested, field_path, depth + 1)
                offset += length
            elif wire_type == 5:
                if offset + 4 > len(data):
                    break
                value = struct.unpack_from("<f", data, offset)[0]
                fixed32.append((field_path, value, order))
                order += 1
                offset += 4
            else:
                offset = field_start + 1

    for payload in payloads:
        scan(payload)

    candidates = [
        item for item in fixed32
        if item[0][-1:] == (1,) and math.isfinite(item[1]) and 0.0 <= item[1] <= 100.0
    ]
    exact = [item for item in candidates if item[0] == (1, 1)]
    picked = min(exact or candidates, key=lambda item: (len(item[0]), item[2])) if (exact or candidates) else None

    epoch_now = int((now or datetime.now(timezone.utc)).timestamp())
    timestamp_fields = {
        path: value for path, value in varints
        if 1_700_000_000 <= value <= 2_100_000_000
    }
    start_epoch = timestamp_fields.get((1, 4, 1)) or timestamp_fields.get((1, 8, 2, 1))
    end_epoch = timestamp_fields.get((1, 5, 1)) or timestamp_fields.get((1, 8, 3, 1))
    if end_epoch is None:
        future = sorted(value for value in timestamp_fields.values() if value > epoch_now)
        end_epoch = future[0] if future else None

    has_usage_period = any(
        (path[:2] == (1, 6)) or (path == (1, 8, 1) and value in (1, 2))
        for path, value in varints
    )
    percent_used = picked[1] if picked else (
        0.0 if not fixed32 and has_usage_period and end_epoch else None
    )
    if percent_used is None:
        raise ProbeError("xAI usage API response contained no recognized usage percent")

    return {
        "percent_used": round(float(percent_used), 3),
        "window_start": from_epoch_maybe(start_epoch),
        "window_end": from_epoch_maybe(end_epoch),
    }


def http_json_with_headers(url: str, *, headers: dict[str, str] | None = None, method: str = "GET", body: Any = None, timeout: int = DEFAULT_TIMEOUT) -> tuple[dict[str, Any], dict[str, str]]:
    raw, response_headers = http_bytes(url, headers=headers, method=method, body=body, timeout=timeout)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"invalid JSON from {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProbeError(f"unexpected JSON root from {url}")
    return payload, response_headers


def http_json(url: str, *, headers: dict[str, str] | None = None, method: str = "GET", body: Any = None, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    payload, _ = http_json_with_headers(url, headers=headers, method=method, body=body, timeout=timeout)
    return payload


def http_bytes(url: str, *, headers: dict[str, str] | None = None, method: str = "GET", body: Any = None, timeout: int = DEFAULT_TIMEOUT) -> tuple[bytes, dict[str, str]]:
    payload = body
    if isinstance(body, dict):
        payload = json.dumps(body).encode("utf-8")
    request = Request(url, data=payload, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read(), dict(response.info())
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise ProbeError(f"HTTP {exc.code} from {url}: {message[:300]}") from exc
    except URLError as exc:
        raise ProbeError(f"network error for {url}: {exc.reason}") from exc


def header_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def normalize_iso(value: Any) -> str | None:
    dt = parse_iso(value)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if dt else None


def from_epoch_ms(value: Any) -> str | None:
    number = as_float(value)
    if number is None:
        return None
    return datetime.fromtimestamp(number / 1000.0, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def from_epoch_maybe(value: Any) -> str | None:
    number = as_float(value)
    if number is None:
        return None
    if number >= 1e12:
        ts = number / 1000.0
    else:
        ts = number
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_at_delta(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def relative_reset_text(reset_at: str | None) -> str | None:
    if not reset_at:
        return None
    dt = parse_iso(reset_at)
    if dt is None:
        return None
    delta = dt - datetime.now(timezone.utc)
    total = int(delta.total_seconds())
    if total <= 0:
        return "reset due now"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append("<1m")
    return "resets in " + " ".join(parts)


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def flexible_zai_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return from_epoch_maybe(value)
    text = str(value).strip()
    if text.isdigit():
        return from_epoch_maybe(int(text))
    return normalize_iso(text)


def summarize_zai_model_usage(payload: dict[str, Any]) -> Window | None:
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    total_prompts = 0
    total_tokens = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        total_prompts += as_int(item.get("requestCount") or item.get("request_count")) or 0
        total_tokens += as_int(item.get("tokenCount") or item.get("token_count")) or 0
    if total_prompts == 0 and total_tokens == 0:
        return None
    return Window(label="7d-activity", remaining_text=f"{total_prompts} prompts · {total_tokens:,} tokens")


def expand_path(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def load_env_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.startswith(prefix):
            continue
        value = stripped.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\"', "'"}:
            value = value[1:-1]
        return value or None
    return None


def jwt_claim(token: Any, claim: str) -> str | None:
    if not token or not isinstance(token, str) or token.count(".") < 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
        value = data.get(claim)
        return str(value) if value else None
    except Exception:
        return None


def friendly_error(provider: str, error: str) -> str:
    if provider == "anthropic" and "429" in error:
        return "Claude usage API is rate limited right now; retry later or keep using the last cached snapshot."
    if provider == "kimi-coding" and "401" in error:
        return "Kimi rejected the token. Export KIMI_AUTH_TOKEN from a browser kimi-auth session token for live quota reads."
    if provider == "kimi-coding" and "no Kimi token available" in error:
        return "No Kimi session token available. Set KIMI_AUTH_TOKEN for live quota reads."
    return error


def status_from_error(provider: str, error: str) -> str:
    if provider == "anthropic" and "429" in error:
        return "rate_limited"
    if provider == "kimi-coding" and "401" in error:
        return "auth_required"
    if provider == "kimi-coding" and "token" in error.lower():
        return "auth_required"
    return "error"


def load_config(path: str | None) -> AppConfig:
    if not path:
        return AppConfig()
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(config_path)
    raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.suffix == ".json" else parse_simple_yaml(config_path.read_text(encoding="utf-8"))
    return AppConfig(
        host=str(raw.get("host", DEFAULT_HOST)),
        port=int(raw.get("port", DEFAULT_PORT)),
        auth_path=expand_path(str(raw.get("auth_path", DEFAULT_AUTH_PATH))),
        timeout_seconds=int(raw.get("timeout_seconds", DEFAULT_TIMEOUT)),
        refresh_seconds=int(raw.get("refresh_seconds", DEFAULT_REFRESH_SECONDS)),
        claude_credentials_path=expand_path(str(raw.get("claude_credentials_path", AppConfig.claude_credentials_path))),
        claude_statusline_path=expand_path(str(raw.get("claude_statusline_path", AppConfig.claude_statusline_path))),
        cache_path=expand_path(str(raw.get("cache_path", AppConfig.cache_path))),
        kimi_credentials_path=expand_path(str(raw.get("kimi_credentials_path", AppConfig.kimi_credentials_path))),
        disabled_providers=parse_disabled_providers(raw.get("disabled_providers")),
    )


def parse_disabled_providers(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, str):
        raise ValueError("disabled_providers must be a comma-separated string")

    candidates = value.split(",")
    disabled = frozenset(str(candidate).strip() for candidate in candidates if str(candidate).strip())
    unknown = sorted(disabled.difference(KNOWN_PROVIDERS))
    if unknown:
        label = "provider" if len(unknown) == 1 else "providers"
        raise ValueError(f"unknown disabled {label}: {', '.join(unknown)}")
    return disabled


def parse_simple_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if value.lower() in {"true", "false"}:
            data[key] = value.lower() == "true"
        else:
            try:
                data[key] = int(value)
            except ValueError:
                data[key] = value
    return data


def run_server(config: AppConfig) -> None:
    service = UsageService(config)

    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.service = service
    BoundHandler.config = config
    server = ThreadingHTTPServer((config.host, config.port), BoundHandler)
    print(f"provider-usage-dashboard listening on http://{config.host}:{config.port}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="LAN dashboard for provider usage quotas")
    parser.add_argument("--config", help="optional simple YAML/JSON config file")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--auth-path", default=None)
    parser.add_argument("--dump-json", action="store_true", help="print one usage snapshot and exit")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    if args.auth_path:
        config.auth_path = args.auth_path

    service = UsageService(config)
    if args.dump_json:
        print(json.dumps(service.collect_all(), indent=2, ensure_ascii=False))
        return
    run_server(config)


if __name__ == "__main__":
    main()
