#!/bin/zsh
# R2 → InfluxDB ingest of is-the-mountain-out inference results.
# Runs via LaunchAgent com.tommydoerr.mountain-ingest every 15 min, mirroring
# the public history.jsonl into the `mountain` bucket. Stdlib-only (no boto3 —
# the source bucket is public over HTTPS). Requires Full Disk Access on /bin/zsh
# (reads /Volumes + mountain/.env). Mirrors campsites/ingest.sh.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
SCRIPT_DIR="${0:A:h}"
[ -f "${SCRIPT_DIR}/.env" ] && { set -a; source "${SCRIPT_DIR}/.env"; set +a; }
echo "[$(date)] mountain ingest starting"
uv run --no-project python "${SCRIPT_DIR}/ingest.py" "$@"
echo "[$(date)] mountain ingest done (exit $?)"
