#!/usr/bin/env zsh
# Deploy the campsite ingest to the INTERNAL disk and (re)load its LaunchAgent.
#
# Why internal-disk: a launchd-spawned `python3 /Volumes/.../ingest.py` lacks
# Full Disk Access, so opening the script off /Volumes hits the TCC block and
# HANGS (FDA is on /bin/zsh, not python). So the runner, the python, and the .env
# all live on the internal disk — same pattern as dev-status / claude-usage.
# Edit the repo copies here, then run this to deploy + reload.
set -euo pipefail

SRC="${0:A:h}"                                   # campsites/ in the repo (/Volumes)
DEST="$HOME/.local/share/campsite-ingest"        # internal-disk runtime copy
PLIST_SRC="$SRC/com.tommydoerr.campsite-ingest.plist"
PLIST="$HOME/Library/LaunchAgents/com.tommydoerr.campsite-ingest.plist"

if [[ ! -f "$SRC/.env" ]]; then
  echo "ERROR: $SRC/.env missing — copy .env.example and set INFLUX_CAMPSITES_TOKEN." >&2
  exit 1
fi

mkdir -p "$DEST"
rsync -a "$SRC/ingest.py" "$SRC/ingest.sh" "$SRC/.env" "$DEST/"
chmod 600 "$DEST/.env"

cp "$PLIST_SRC" "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Deployed to $DEST and loaded com.tommydoerr.campsite-ingest (daily 07:30)."
echo "Test now:  launchctl start com.tommydoerr.campsite-ingest && tail -f ~/Library/Logs/campsite-ingest.log"
