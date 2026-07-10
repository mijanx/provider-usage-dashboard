from __future__ import annotations

import struct
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import importlib.util
import sys
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("provider_xai_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
sys.modules["provider_xai_app"] = app
spec.loader.exec_module(app)

AppConfig = app.AppConfig
ProbeError = app.ProbeError
UsageService = app.UsageService
Window = app.Window
parse_xai_billing_grpc_web = app.parse_xai_billing_grpc_web


def varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def length_field(number: int, payload: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(payload)) + payload


def timestamp(seconds: int) -> bytes:
    return varint(1 << 3) + varint(seconds)


def billing_response(
    percent: float | None,
    start: int,
    end: int,
    *,
    grpc_status: int = 0,
    unrelated_fixed32: float | None = None,
) -> bytes:
    payload = bytearray()
    if percent is not None:
        payload.extend(varint((1 << 3) | 5))
        payload.extend(struct.pack("<f", percent))
    if unrelated_fixed32 is not None:
        payload.extend(varint((2 << 3) | 5))
        payload.extend(struct.pack("<f", unrelated_fixed32))
    payload.extend(length_field(4, timestamp(start)))
    payload.extend(length_field(5, timestamp(end)))
    payload.extend(length_field(8, varint(1 << 3) + varint(2)))
    message = length_field(1, bytes(payload))
    data_frame = b"\x00" + len(message).to_bytes(4, "big") + message
    trailer = f"grpc-status:{grpc_status}\r\n".encode()
    trailer_frame = b"\x80" + len(trailer).to_bytes(4, "big") + trailer
    return data_frame + trailer_frame


class XaiBillingParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = 1_783_464_413
        self.end = self.start + 7 * 24 * 60 * 60
        self.now = datetime.fromtimestamp(self.start + 60, timezone.utc)

    def test_parses_weekly_percent_and_period(self) -> None:
        parsed = parse_xai_billing_grpc_web(billing_response(13.0, self.start, self.end), now=self.now)
        self.assertEqual(parsed["percent_used"], 13.0)
        self.assertEqual(parsed["window_start"], "2026-07-07T22:46:53Z")
        self.assertEqual(parsed["window_end"], "2026-07-14T22:46:53Z")

    def test_omitted_proto3_zero_is_zero_during_current_period(self) -> None:
        parsed = parse_xai_billing_grpc_web(billing_response(None, self.start, self.end), now=self.now)
        self.assertEqual(parsed["percent_used"], 0.0)

    def test_unrecognized_fixed32_degrades_instead_of_assuming_zero(self) -> None:
        raw = billing_response(None, self.start, self.end, unrelated_fixed32=42.0)
        with self.assertRaisesRegex(ProbeError, "no recognized usage percent"):
            parse_xai_billing_grpc_web(raw, now=self.now)

    def test_rejects_nonzero_grpc_status(self) -> None:
        with self.assertRaisesRegex(ProbeError, "gRPC status 7"):
            parse_xai_billing_grpc_web(billing_response(13.0, self.start, self.end, grpc_status=7), now=self.now)

    def test_weekly_window_maps_to_dashboard_contract(self) -> None:
        service = UsageService(AppConfig())
        raw = billing_response(13.0, self.start, self.end)
        with patch("provider_xai_app.http_bytes", return_value=(raw, {})) as request:
            window = service._xai_weekly_window("secret-token")
        self.assertEqual(window.label, "weekly")
        self.assertEqual(window.percent_used, 13.0)
        self.assertEqual(window.percent_remaining, 87.0)
        self.assertEqual(window.reset_at, "2026-07-14T22:46:53Z")
        self.assertTrue(window.meta["shared_pool"])
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertEqual(request.call_args.kwargs["body"], b"\x00\x00\x00\x00\x00")

    def test_weekly_window_rejects_nonzero_grpc_header(self) -> None:
        service = UsageService(AppConfig())
        raw = billing_response(13.0, self.start, self.end)
        with patch("provider_xai_app.http_bytes", return_value=(raw, {"Grpc-Status": "7"})):
            with self.assertRaisesRegex(ProbeError, "gRPC status 7"):
                service._xai_weekly_window("secret-token")

    def test_full_xai_probe_combines_weekly_and_subscription(self) -> None:
        service = UsageService(AppConfig())
        weekly = Window(label="weekly", percent_used=14.0, percent_remaining=86.0)
        subscription = {
            "subscriptions": [{
                "status": "SUBSCRIPTION_STATUS_ACTIVE",
                "tier": "SUBSCRIPTION_TIER_GROK_PRO",
                "billingPeriodEnd": "2027-05-22T21:56:35Z",
            }]
        }

        def account_response(url: str, **_kwargs):
            return subscription if url.endswith("/rest/subscriptions") else {"id": "account"}

        with (
            patch.object(service, "_xai_oauth_token", return_value="secret-token"),
            patch.object(service, "_xai_weekly_window", return_value=weekly),
            patch("provider_xai_app.http_json", side_effect=account_response),
        ):
            result = service.probe_xai_oauth()

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source, "oauth_usage_api+account_api")
        self.assertEqual(result.plan, "Grok Pro")
        self.assertEqual([window.label for window in result.windows], ["weekly", "billing_period"])

    def test_full_xai_probe_truthfully_degrades_when_weekly_api_fails(self) -> None:
        service = UsageService(AppConfig())
        subscription = {
            "subscriptions": [{
                "status": "SUBSCRIPTION_STATUS_ACTIVE",
                "tier": "SUBSCRIPTION_TIER_GROK_PRO",
                "billingPeriodEnd": "2027-05-22T21:56:35Z",
            }]
        }

        def account_response(url: str, **_kwargs):
            return subscription if url.endswith("/rest/subscriptions") else {"id": "account"}

        with (
            patch.object(service, "_xai_oauth_token", return_value="secret-token"),
            patch.object(service, "_xai_weekly_window", side_effect=ProbeError("schema changed")),
            patch("provider_xai_app.http_json", side_effect=account_response),
        ):
            result = service.probe_xai_oauth()

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source, "oauth_account_api")
        self.assertEqual(result.plan, "Grok Pro")
        self.assertEqual([window.label for window in result.windows], ["billing_period"])


if __name__ == "__main__":
    unittest.main()
