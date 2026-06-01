#!/bin/zsh
# install.sh — stand up the observability coordinator on the INTERNAL disk and
# load its launchd job. Run this from the /Volumes repo (interactive shell, which
# can read /Volumes); everything it creates lives on the internal disk so the
# launchd worker never has to touch /Volumes.
#
#   ./install.sh [git-remote-url]
#
# Idempotent: re-running fast-forwards the clone and reloads the job. Run it
# AFTER this PR merges to main (the worker it loads lives in the clone of main).
set -eu

HERE="${0:A:h}"
REPO_ROOT="${HERE:h}"
COORD_HOME="$HOME/.observability/coordinator"
REPO="$COORD_HOME/repo"
REMOTE_URL="${1:-git@github.com:tommyroar/observability-config.git}"
PLIST_DEST="$HOME/Library/LaunchAgents/com.tommy.observability-coordinator.plist"

mkdir -p "$COORD_HOME"/queue "$COORD_HOME"/processing "$COORD_HOME"/done "$COORD_HOME"/failed

# Internal-disk clone (launchd-safe; never hand-edited — the worker hard-resets it to main).
if [ -d "$REPO/.git" ]; then
  git -C "$REPO" fetch -q origin
  git -C "$REPO" reset --hard -q origin/main
else
  git clone -q "$REMOTE_URL" "$REPO"
fi
echo "clone at $REPO ($(git -C "$REPO" rev-parse --short HEAD))"

# Coordinator env: just the token the heartbeat needs. Copied from /Volumes here
# (interactive shell), kept on the internal disk so launchd doesn't read /Volumes.
if [ -f "$REPO_ROOT/influxdb/.env" ]; then
  grep -E '^(INFLUX_OPS_TOKEN|INFLUX_ADMIN_TOKEN|INFLUX_URL)=' "$REPO_ROOT/influxdb/.env" \
    > "$COORD_HOME/env" 2>/dev/null || true
  [ -s "$COORD_HOME/env" ] && chmod 600 "$COORD_HOME/env"
fi

# launchd job, pointed at the worker INSIDE the clone.
WORKER="$REPO/coordination/worker.sh"
sed "s|__COORD_WORKER__|$WORKER|g" "$HERE/com.tommy.observability-coordinator.plist" > "$PLIST_DEST"
chmod 644 "$PLIST_DEST"
launchctl bootout "gui/$(id -u)/com.tommy.observability-coordinator" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
echo "loaded com.tommy.observability-coordinator (worker: $WORKER)"
echo "enqueue a deploy with:  $REPO_ROOT/coordination/enqueue.sh deploy \"<reason>\""
