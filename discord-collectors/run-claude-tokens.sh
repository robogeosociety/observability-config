#!/bin/zsh
# Wrapper for the Claude output-token milestone notifier (Nomad job
# discord-claude-tokens).
#
# claude_tokens.py reads INFLUXDB_{URL,TOKEN,ORG}; the machine's read creds live
# in ask-dash/.env as INFLUX_{URL,ORG} + INFLUX_READ_TOKEN. Remap them here so no
# secret is baked into the Nomad spec. The Discord webhook is read by
# claude_tokens.py from env -> grafana/.env (DISCORD_WEBHOOK_URL_CLAUDE). Passes
# "$@" through so `run-claude-tokens.sh --dry-run` works for testing.
set -eu
HERE="${0:A:h}"
set -a; source /Volumes/dev/observability/ask-dash/.env; set +a
export INFLUXDB_URL="${INFLUX_URL:-http://localhost:8086}"
export INFLUXDB_TOKEN="${INFLUX_READ_TOKEN:?INFLUX_READ_TOKEN missing in ask-dash/.env}"
export INFLUXDB_ORG="${INFLUX_ORG:-home}"
exec /Users/tommydoerr/.local/bin/uv run --with httpx --with influxdb-client \
  "$HERE/claude_tokens.py" "$@"
