#!/bin/zsh
# worker.sh — drain the coordinator queue and run the one serialized deploy step.
#
# Phase 0: every job is a "deploy". The worker refreshes its internal-disk clone
# to origin/main, syncs Grafana provisioning to the internal mount, restarts
# Grafana, VERIFIES health, and ROLLS BACK the provisioning dir if Grafana doesn't
# come back. All of it runs behind the mutex, so launchd ticks and interactive
# deploys never overlap.
#
# Runs under launchd from the INTERNAL-disk clone (TCC blocks launchd from
# /Volumes). It touches only ~/.observability and the docker socket — never
# /Volumes — so it's launchd-legal. See COORDINATION-PLAN.md §5.4.
set -eu

HERE="${0:A:h}"
: "${COORD_HOME:=$HOME/.observability/coordinator}"
: "${COORD_REPO:=$COORD_HOME/repo}"
: "${COORD_PROVISION_DEST:=$HOME/.observability/grafana/provisioning}"
: "${COORD_GIT_REMOTE:=origin}"
: "${COORD_GRAFANA_CONTAINER:=grafana}"
: "${COORD_GRAFANA_HEALTH:=http://localhost:3001/api/health}"
: "${COORD_DEPLOY:=1}"          # set 0 in tests to skip git/docker/curl side effects
: "${COORD_HEALTH_TRIES:=15}"

[ -f "$COORD_HOME/env" ] && source "$COORD_HOME/env"   # optional: INFLUX_OPS_TOKEN, INFLUX_URL
source "$HERE/mutex.sh"

log() { print -r -- "$(/bin/date +%FT%T%z) $*"; }

mkdir -p "$COORD_HOME"/queue "$COORD_HOME"/processing "$COORD_HOME"/done "$COORD_HOME"/failed

coord_acquire_lock || { log "lock held by another run — exiting"; exit 0; }
trap 'coord_release_lock' EXIT

start_ts=$(/bin/date +%s)
setopt local_options null_glob
jobs=("$COORD_HOME"/queue/*.job)
njobs=${#jobs[@]}
if (( njobs == 0 )); then log "queue empty"; exit 0; fi

# Coalesce: all queued deploy jobs collapse into one deploy of current main.
for j in "${jobs[@]}"; do mv "$j" "$COORD_HOME/processing/"; done
log "draining $njobs job(s)"

ok=1
sha="(skipped)"
if [ "$COORD_DEPLOY" = "1" ]; then
  # Refresh the internal clone to main (clone is never hand-edited → hard reset is safe).
  git -C "$COORD_REPO" fetch -q "$COORD_GIT_REMOTE" || ok=0
  git -C "$COORD_REPO" reset --hard -q "$COORD_GIT_REMOTE/main" || ok=0
  sha="$(git -C "$COORD_REPO" rev-parse --short HEAD 2>/dev/null || print unknown)"

  if [ "$ok" = "1" ]; then
    # Snapshot current prod provisioning for rollback.
    if [ -d "$COORD_PROVISION_DEST" ]; then
      rm -rf "$COORD_PROVISION_DEST.prev"
      cp -R "$COORD_PROVISION_DEST" "$COORD_PROVISION_DEST.prev" 2>/dev/null || true
    fi
    mkdir -p "$COORD_PROVISION_DEST"
    rsync -a --delete "$COORD_REPO/grafana/provisioning/" "$COORD_PROVISION_DEST/" || ok=0
    docker restart "$COORD_GRAFANA_CONTAINER" >/dev/null 2>&1 || ok=0

    # Verify the instance comes back healthy.
    healthy=0
    for i in $(seq 1 "$COORD_HEALTH_TRIES"); do
      if curl -sf "$COORD_GRAFANA_HEALTH" >/dev/null 2>&1; then healthy=1; break; fi
      sleep 2
    done
    if [ "$healthy" != "1" ]; then
      ok=0
      log "grafana unhealthy after deploy of main@$sha — rolling back provisioning"
      if [ -d "$COORD_PROVISION_DEST.prev" ]; then
        rsync -a --delete "$COORD_PROVISION_DEST.prev/" "$COORD_PROVISION_DEST/"
        docker restart "$COORD_GRAFANA_CONTAINER" >/dev/null 2>&1 || true
      fi
    fi
  fi
  log "deploy main@$sha ok=$ok"
fi

# File processed jobs by outcome (queue head never blocks: failures are recorded, not retried hot).
dest=$([ "$ok" = "1" ] && print done || print failed)
for j in "$COORD_HOME"/processing/*.job; do mv "$j" "$COORD_HOME/$dest/"; done

# Best-effort heartbeat to the ops bucket (no token → skip).
if [ -n "${INFLUX_OPS_TOKEN:-}" ]; then
  dur=$(( $(/bin/date +%s) - start_ts ))
  host="$(hostname -s 2>/dev/null || print mac)"
  line="coordinator,host=${host} success=${ok}i,jobs=${njobs}i,duration_s=${dur}i $(/bin/date +%s)"
  curl -s -XPOST "${INFLUX_URL:-http://localhost:8086}/api/v2/write?org=home&bucket=ops&precision=s" \
    -H "Authorization: Token ${INFLUX_OPS_TOKEN}" --data-binary "$line" >/dev/null 2>&1 || true
fi

log "done ($dest)"
