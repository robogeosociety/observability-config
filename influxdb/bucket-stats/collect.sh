#!/bin/zsh
# Per-bucket InfluxDB inventory collector. Runs via LaunchAgent
# com.tommydoerr.influx-bucket-stats every 5 min from the INTERNAL disk
# (deploy.sh installs it there — a launchd-spawned python reading /Volumes hits
# the TCC block and hangs; same reason as campsites/dev-status).
#
# Stdlib-only python3 over plain-HTTP localhost:8086 — no uv, no curl needed.
set -uo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
SCRIPT_DIR="${0:A:h}"
[[ -f "${SCRIPT_DIR}/.env" ]] && source "${SCRIPT_DIR}/.env"
echo "[$(date)] influx bucket-stats starting"
python3 -u "${SCRIPT_DIR}/collect.py" "$@"
echo "[$(date)] influx bucket-stats done (exit $?)"
