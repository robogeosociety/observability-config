#!/usr/bin/env zsh
# Sync Grafana provisioning from the version-controlled repo copy (on the
# external /Volumes/dev disk) to the INTERNAL-disk path that the grafana
# container actually bind-mounts. This mirrors telegraf/deploy.sh: edit the
# canonical files in ./provisioning, then run this to deploy + reload.
#
# Why: the container must NOT read config off the external disk. The external
# disk's unmount/TCC behavior broke containers on 2026-05-31 (see
# transit_tracker/CRASH_REPORT.md). Internal copy = robust across reboots.
set -euo pipefail

SRC="${0:A:h}/provisioning"                      # repo copy (this dir)/provisioning
DEST="$HOME/.observability/grafana/provisioning" # internal copy the container mounts

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: source provisioning dir not found: $SRC" >&2
  exit 1
fi

mkdir -p "$DEST"
echo "Syncing $SRC -> $DEST"
rsync -a --delete "$SRC"/ "$DEST"/

# Reload grafana so it re-reads provisioning (no-op if not running).
if docker ps --format '{{.Names}}' | grep -qx grafana; then
  echo "Restarting grafana to apply provisioning…"
  docker restart grafana >/dev/null
  echo "Done. Grafana reloaded."
else
  echo "Done. (grafana container not running — will pick up on next start.)"
fi
