#!/bin/zsh
# proc-mem-collector/run.sh — sample per-process memory into InfluxDB `ops`.
#
# Run by the Nomad periodic job `proc-mem-collector` (nomad/proc-mem-collector.hcl)
# every minute. Mirrors runtime-versions/collect.sh: runs under the host Nomad agent,
# reads INFLUX_OPS_TOKEN from observability/influxdb/.env directly via the agent's
# Full Disk Access (no internal-disk deploy). Passes "$@" through so
# `run.sh --dry-run` works for testing.
set -uo pipefail
emulate -L zsh
HERE="${0:A:h}"   # resolve the script dir BEFORE cd, while $0 is still relative to CWD
cd "$HOME"        # keep Python startup CWD off /Volumes (dyld getcwd hang under daemons)

export INFLUX_URL="${INFLUX_URL:-http://localhost:8086}"
export INFLUX_ORG="${INFLUX_ORG:-home}"
export INFLUX_OPS_BUCKET="${INFLUX_OPS_BUCKET:-ops}"
ENV_FILE="/Volumes/dev/observability/influxdb/.env"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
export INFLUX_OPS_TOKEN="${INFLUX_OPS_TOKEN:-${INFLUX_ADMIN_TOKEN:-}}"

exec /Users/tommydoerr/.local/bin/uv run --no-project python "$HERE/proc_mem.py" "$@"
