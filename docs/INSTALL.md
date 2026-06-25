# Install

## Requirements

- Python 3.11+
- Local OAuth/API credential files for the providers you want to inspect
- A trusted localhost/LAN deployment target

No third-party Python packages are required.

## Quick start

```bash
git clone git@github.com:<owner>/provider-usage-dashboard.git
cd provider-usage-dashboard
cp config.example.yaml config.yaml
python3 app.py --config config.yaml
```

Open `http://127.0.0.1:8768/`.

By default the app binds to `127.0.0.1`. For trusted-LAN access, explicitly set `host: 0.0.0.0` in your private `config.yaml` or pass `--host 0.0.0.0`.

## Credential source

The default `auth_path` expects a Hermes-style `auth.json` with `credential_pool` entries. See `fixtures/sample-auth.json` for the expected shape. Replace all `REPLACE_ME` values; do not commit real credentials.

For Claude, the dashboard can also read Claude Code's statusline JSON fallback. See `fixtures/claude-statusline-rate-limits.sample.json`.

## systemd user service

```bash
cp provider-usage-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now provider-usage-dashboard.service
systemctl --user status provider-usage-dashboard.service --no-pager
```

Edit paths in the service file first if your checkout is not at `%h/projects/AI-Tools/provider-usage-dashboard`.
