# xAI usage surface investigation

Date: 2026-07-10

## Finding

xAI now exposes the shared Grok subscription weekly pool to its own Usage UI. The dashboard can read the same value with the existing xAI OAuth bearer token.

Working read-only request:

- `POST https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig`
- Content type: `application/grpc-web+proto`
- Body: one empty gRPC-web message (`00 00000000`)
- Authentication: existing xAI OAuth bearer token
- Returned fields observed and implemented: percent used, weekly start, weekly reset/end

Live verification on 2026-07-10 returned a seven-day window and a valid usage percentage. The normalized dashboard output labels this `weekly` and marks it as a shared pool.

## What did not work

`POST https://grok.com/rest/rate-limits` still rejects OAuth tokens with:

`Action cannot be performed by OAuth2 token users`

That older endpoint is for browser-session/cookie-authenticated Grok limits and is not the right surface for Hermes xAI OAuth.

The installed Grok CLI 0.2.14 ACP server also returns `Method not found` for `x.ai/billing`, so the CLI RPC is not currently usable as a fallback.

## Official vs undocumented APIs

- Official xAI documentation (Grok FAQ, updated 2026-07-06) confirms one shared weekly pool across Chat, Imagine, Voice, Build, and related Grok products, with usage and reset time shown under Settings → Usage.
- The documented xAI Management API exposes team/API billing analytics (`POST /v1/billing/teams/{team_id}/usage`). That is API-team spend, not the personal Grok/SuperGrok shared weekly subscription meter.
- `GetGrokCreditsConfig` is an undocumented grok.com UI gRPC-web endpoint. It is therefore treated as best-effort. If it changes, the provider remains available with subscription plan and billing-period data rather than failing the whole card.

## Current limits

The implemented parser intentionally extracts only the overall shared-pool percentage and weekly period bounds. Product-level breakdown values shown by the UI were not present as usable values in the observed response and are not inferred. No request counts or monetary values are fabricated.

## Sources

- https://docs.x.ai/grok/faq
- https://docs.x.ai/developers/rest-api-reference/management/billing
- https://docs.x.ai/console/usage
- Independent implementation comparison: CodexBar `GrokWebBillingFetcher` and `docs/grok.md`
