#!/usr/bin/env zsh
# Deploy the anomaly-detector to the INTERNAL disk and (re)load its launchd job.
#
# Why internal-disk: macOS TCC blocks launchd from reading /Volumes. So detector.py,
# anomaly-detection.yaml, .env, and the venv are copied here. Re-run whenever the
# detector or its config changes (same edit-here-then-deploy rule as drift-sentinel).
set -euo pipefail

SRC="${0:A:h}"                                   # this dir
DEST="$HOME/.local/share/anomaly-detector"

if [[ ! -f "$SRC/.env" ]]; then
  echo "ERROR: $SRC/.env missing (copy .env.example, fill INFLUX_TOKEN with read+ops-write)" >&2
  exit 1
fi

mkdir -p "$DEST"
rsync -a "$SRC/detector.py" "$SRC/anomaly-detection.yaml" "$SRC/pyproject.toml" "$SRC/.env" "$DEST/"
[[ -f "$SRC/uv.lock" ]] && rsync -a "$SRC/uv.lock" "$DEST/"

# Build the internal venv.
( cd "$DEST" && /Users/tommydoerr/.local/bin/uv sync --quiet )

# Install + (re)load the launchd job.
PLIST="$HOME/Library/LaunchAgents/com.tommy.anomaly-detector.plist"
cp "$SRC/com.tommy.anomaly-detector.plist" "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Deployed to $DEST and loaded com.tommy.anomaly-detector (every 10 min)."
echo "Tail: ~/Library/Logs/anomaly-detector.log"
