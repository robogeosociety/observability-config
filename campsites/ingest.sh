#!/bin/zsh
# Daily R2 → InfluxDB ingest of campsite availability summaries.
# Runs via LaunchAgent com.tommydoerr.campsite-ingest, after the raw-collection
# Worker cron (13:00 UTC). Requires Full Disk Access on /bin/zsh (reads /Volumes
# + campsites/.env). Reads R2 via the wrangler OAuth session (no S3 key) — needs
# uv, and node/npx on PATH for wrangler token refresh.
set -uo pipefail
# Include ~/.local/bin (uv) + /opt/homebrew/bin (node/npx) — launchd's minimal
# PATH omits both (the scheduled run would die on `command not found`).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
SCRIPT_DIR="${0:A:h}"
echo "[$(date)] campsite ingest starting"
uv run --no-project python "${SCRIPT_DIR}/ingest.py" "$@"
echo "[$(date)] campsite ingest done (exit $?)"
