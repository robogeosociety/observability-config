#!/bin/zsh
# Wrapper for the weekly Discord digest (Nomad job discord-digest).
#
# digest.py expects INFLUXDB_{URL,TOKEN,ORG}; the machine's read creds live in
# ask-dash/.env as INFLUX_{URL,ORG} + INFLUX_READ_TOKEN (read-all, perfect for a
# digest). Remap them here so no secret is baked into the Nomad spec. The Discord
# webhook is read by digest.py itself from env -> grafana/.env. Passes "$@"
# through so `run-digest.sh --dry-run` works for testing.
set -eu
HERE="${0:A:h}"
set -a; source /Volumes/dev/observability/ask-dash/.env; set +a
export INFLUXDB_URL="${INFLUX_URL:-http://localhost:8086}"
export INFLUXDB_TOKEN="${INFLUX_READ_TOKEN:?INFLUX_READ_TOKEN missing in ask-dash/.env}"
export INFLUXDB_ORG="${INFLUX_ORG:-home}"
exec /Users/tommydoerr/.local/bin/uv run --with httpx --with influxdb-client \
  "$HERE/digest.py" "$@"
