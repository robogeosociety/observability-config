#!/usr/bin/env zsh
# Deploy the drift-sentinel to the INTERNAL disk and (re)load its launchd job.
#
# Why internal-disk: macOS TCC blocks launchd from reading /Volumes (the documented
# gotcha). So everything the scheduled run touches — sentinel.py, .env, the venv,
# and a SNAPSHOT of dashboards.index.yaml — is copied here. Re-run this whenever the
# sentinel or the index changes (same edit-here-then-deploy rule as dev-status).
set -euo pipefail

SRC="${0:A:h}"                                   # this dir
REPO="${SRC:h:h}"                                # observability-config root
DEST="$HOME/.local/share/drift-sentinel"

if [[ ! -f "$SRC/.env" ]]; then
  echo "ERROR: $SRC/.env missing (copy .env.example, fill INFLUX_READ_TOKEN + DISCORD_WEBHOOK_URL)" >&2
  exit 1
fi

mkdir -p "$DEST"
rsync -a "$SRC/sentinel.py" "$SRC/pyproject.toml" "$SRC/.env" "$DEST/"
[[ -f "$SRC/uv.lock" ]] && rsync -a "$SRC/uv.lock" "$DEST/"
# Snapshot the index so the runtime never reads /Volumes.
rsync -a "$REPO/grafana/dashboards.index.yaml" "$DEST/dashboards.index.yaml"

# Build the internal venv.
( cd "$DEST" && /Users/tommydoerr/.local/bin/uv sync --quiet )

# Install + (re)load the launchd job.
PLIST="$HOME/Library/LaunchAgents/com.tommy.drift-sentinel.plist"
cp "$SRC/com.tommy.drift-sentinel.plist" "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Deployed to $DEST and loaded com.tommy.drift-sentinel (every 10 min)."
echo "Tail: ~/Library/Logs/drift-sentinel.log"
