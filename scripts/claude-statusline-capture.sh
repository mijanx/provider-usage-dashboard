#!/usr/bin/env bash
set -euo pipefail

out_file="${1:-${CLAUDE_STATUSLINE_OUT:-$HOME/.claude/statusline-rate-limits.json}}"
mkdir -p "$(dirname "$out_file")"
cat > "$out_file"
printf 'cc-status\n'
