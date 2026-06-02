#!/bin/zsh
# Daily R2 → InfluxDB ingest of campsite availability summaries.
# Runs via LaunchAgent com.tommydoerr.campsite-ingest, after the raw-collection
# Worker cron (13:00 UTC). Requires Full Disk Access on /bin/zsh (reads /Volumes
# + campsites/.env). Mirrors influxdb/backup.sh (runs in place on /Volumes).
set -uo pipefail
# Include ~/.local/bin — that's where uv lives, and launchd's minimal PATH omits it
# (without this, the scheduled run dies on `command not found: uv`).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
SCRIPT_DIR="${0:A:h}"
echo "[$(date)] campsite ingest starting"
uv run --no-project --with boto3 python "${SCRIPT_DIR}/ingest.py" "$@"
echo "[$(date)] campsite ingest done (exit $?)"
