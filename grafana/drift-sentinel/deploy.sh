#!/usr/bin/env zsh
# RETIRED 2026-07-25 — this deploy is disabled on purpose. Do not re-enable it
# without reading the note below.
#
# The sentinel reads InfluxDB, and the mini no longer runs InfluxDB or Grafana:
# both containers are gone from `docker ps -a` (absent, not stopped) and nothing
# listens on :8086 or :3000. The launchd job is already unloaded, its last
# successful run wrote state.json on 2026-07-02, and every invocation after that
# died with `HTTPError: 404` out of flux_count(). Running this script would
# reinstall a job that cannot work.
#
# This is the decommission #156 calls for ("At parity, decommission on the mini:
# Grafana + InfluxDB containers, launchd coordinator, com.tommy.* collectors"),
# not a port. sentinel.py and its tests are KEPT in-tree so the commit-aware
# freshness check can be re-emitted against Analytics Engine as part of that
# migration — same treatment as the parked collectors (#151/#152/#153).
#
# Refs: robogeosociety/robot-geographical-society#175, #156.
#
# --- original header -------------------------------------------------------
# Deploy the drift-sentinel to the INTERNAL disk and (re)load its launchd job.
#
# Why internal-disk: macOS TCC blocks launchd from reading /Volumes (the documented
# gotcha). So everything the scheduled run touches — sentinel.py, .env, the venv,
# and a SNAPSHOT of the dashboards.index.d/ dir — is copied here. Re-run this whenever
# the sentinel or the index changes (same edit-here-then-deploy rule as dev-status).
set -euo pipefail

cat >&2 <<'RETIRED'
drift-sentinel is RETIRED (2026-07-25) — refusing to deploy.

Its data source is gone: no influxdb/grafana containers on this host, nothing
listening on :8086. Re-emit it against Analytics Engine per observability-config#156
instead of reinstalling the launchd job.

Override only if you have deliberately restored InfluxDB:
    DRIFT_SENTINEL_FORCE_DEPLOY=1 ./deploy.sh
RETIRED
[[ "${DRIFT_SENTINEL_FORCE_DEPLOY:-0}" == "1" ]] || exit 1

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
# Snapshot the conf.d index dir so the runtime never reads /Volumes (--delete so a
# removed dashboard's sidecar drops from the snapshot too).
rsync -a --delete "$REPO/grafana/dashboards.index.d/" "$DEST/dashboards.index.d/"

# Build the internal venv.
( cd "$DEST" && /Users/tommydoerr/.local/bin/uv sync --quiet )

# Install + (re)load the launchd job.
PLIST="$HOME/Library/LaunchAgents/com.tommy.drift-sentinel.plist"
cp "$SRC/com.tommy.drift-sentinel.plist" "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Deployed to $DEST and loaded com.tommy.drift-sentinel (every 10 min)."
echo "Tail: ~/Library/Logs/drift-sentinel.log"
