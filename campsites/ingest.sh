#!/bin/zsh
# Daily R2 → InfluxDB ingest of campsite availability summaries.
# Runs via LaunchAgent com.tommydoerr.campsite-ingest, after the raw-collection
# Worker cron (13:00 UTC). Requires Full Disk Access on /bin/zsh (reads /Volumes
# + campsites/.env). Reads R2 via the wrangler OAuth session (no S3 key).
#
# Stdlib-only python3 (NOT `uv run` — uv's own TLS hangs under launchd's
# background session); HTTPS to Cloudflare goes through curl (CPython's TLS hangs
# there too), InfluxDB writes are plain-HTTP localhost. node/npx is only needed if
# the OAuth token has expired (wrangler refresh).
set -uo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
SCRIPT_DIR="${0:A:h}"
echo "[$(date)] campsite ingest starting"
python3 -u "${SCRIPT_DIR}/ingest.py" "$@"
echo "[$(date)] campsite ingest done (exit $?)"
