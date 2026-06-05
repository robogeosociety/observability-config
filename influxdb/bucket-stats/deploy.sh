#!/usr/bin/env zsh
# Deploy the influx bucket-stats collector to the INTERNAL disk and (re)load its
# LaunchAgent.
#
# Why internal-disk: a launchd-spawned `python3 /Volumes/.../collect.py` lacks
# Full Disk Access, so opening the script off /Volumes hits the TCC block and
# HANGS (FDA is on /bin/zsh, not python). So the runner, the python, and the .env
# all live on the internal disk — same pattern as campsites / dev-status.
# Edit the repo copies here, then run this to deploy + reload.
set -euo pipefail

SRC="${0:A:h}"                                      # bucket-stats/ in the repo (/Volumes)
DEST="$HOME/.local/share/influx-bucket-stats"       # internal-disk runtime copy
PLIST_SRC="$SRC/com.tommydoerr.influx-bucket-stats.plist"
PLIST="$HOME/Library/LaunchAgents/com.tommydoerr.influx-bucket-stats.plist"

if [[ ! -f "$SRC/.env" ]]; then
  echo "ERROR: $SRC/.env missing — copy .env.example and set INFLUX_TOKEN." >&2
  exit 1
fi

mkdir -p "$DEST"
rsync -a "$SRC/collect.py" "$SRC/collect.sh" "$SRC/.env" "$DEST/"
chmod 600 "$DEST/.env"

cp "$PLIST_SRC" "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Deployed to $DEST and loaded com.tommydoerr.influx-bucket-stats (every 5 min)."
echo "Test now:  launchctl start com.tommydoerr.influx-bucket-stats && tail -f ~/Library/Logs/influx-bucket-stats.log"
