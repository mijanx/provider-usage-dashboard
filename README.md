# Provider Usage Dashboard

LAN-local dashboard for AI provider quota and usage windows.

It normalizes usage snapshots from provider APIs and OAuth credential stores into a small HTML dashboard and `/api/usage` JSON endpoint. It is designed for local/private deployment, not public internet exposure.

## Providers currently supported

- MiniMax coding plan remains
- Z.ai quota/model usage
- OpenAI Codex OAuth usage
- Anthropic Claude OAuth / Claude Code statusline fallback
- Kimi Code OAuth/session usage
- xAI OAuth / Grok subscription metadata

## Run

```bash
python3 app.py --config config.example.yaml
# open http://127.0.0.1:8768/
```

One-shot JSON:

```bash
python3 app.py --config config.example.yaml --dump-json
```

## Configuration

Copy `config.example.yaml` to `config.yaml`. Paths support `~` and environment variables.

The default auth source assumes a Hermes-style `auth.json` with `credential_pool` entries. If you do not use Hermes, either adapt `AuthStore` or provide an auth JSON with compatible provider entries.

Supported keys:

- `host`, `port`
- `auth_path`
- `claude_credentials_path`
- `claude_statusline_path`
- `kimi_credentials_path`
- `cache_path`
- `timeout_seconds`
- `refresh_seconds`

Environment overrides for defaults:

- `PROVIDER_USAGE_AUTH_PATH`
- `PROVIDER_USAGE_CACHE_PATH`
- `CLAUDE_CREDENTIALS_PATH`
- `CLAUDE_STATUSLINE_PATH`
- `KIMI_CREDENTIALS_PATH`

## Endpoints

- `/` HTML dashboard
- `/api/usage` normalized JSON payload
- `/health` basic health response

## systemd user service

Use `provider-usage-dashboard.service` as a template. Edit `WorkingDirectory` and `ExecStart` paths before installing.

## Security model

This is LAN-trust tooling. It reads/refreshes local OAuth credentials and should stay behind localhost, VPN, or a trusted LAN.
