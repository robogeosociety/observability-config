#!/bin/zsh
# Threshold-gated cleanup of the macOS mediaanalysisd Neural Engine cache leak.
#
# macOS `mediaanalysisd` (Photos/media ML indexer) has a long-standing bug: it
# re-extracts the same ~64MB ANE model into thousands of duplicate
# `*.tmp.NNNNN.bundle` dirs under com.apple.e5rt.e5bundlecache, ballooning to tens
# of GB. Seen at 42GB / 676 dupes on 2026-06-20, which filled the Mac's internal
# disk to 100% and stalled the machine. The cache is fully regenerable — macOS
# rebuilds one clean copy on next use — so deleting it is safe.
#
# Runs via Nomad periodic job `mediaanalysisd-cleanup` (namespace backup), weekly.
# raw_exec runs as tommydoerr in the GUI session, so it can read ~/Library and
# `killall mediaanalysisd`. A clean rebuild settles around ~0.6GB (observed a few
# hours after a purge), so the 3GB threshold leaves headroom for the legit working
# set and only fires once the leak is genuinely running away — this is a no-op on a
# healthy machine. The >90% internal-disk Grafana alert (rule disk-mac-root) is the
# backstop if it ever balloons faster than the weekly cadence.
#
# Emits a heartbeat to InfluxDB (bucket `ops`, measurement `maintenance`) so the
# run is observable; tokens come from the influxdb/.env that backup.sh already uses.
set -uo pipefail

CACHE_DIR="${HOME}/Library/Containers/com.apple.mediaanalysisd/Data/Library/Caches/com.apple.mediaanalysisd/com.apple.e5rt.e5bundlecache"
THRESHOLD_BYTES=$(( 3 * 1024 * 1024 * 1024 ))   # act only above 3GB (clean rebuild ~0.6GB; leak hit 42GB)

# Heartbeat plumbing — mirror influxdb/backup.sh (best-effort, never fatal).
ENV_FILE="/Volumes/dev/observability/influxdb/.env"
OPS_BUCKET="ops"
INFLUX_URL="${INFLUX_URL:-http://localhost:8086}"
HOSTTAG="$(hostname -s 2>/dev/null || echo unknown)"
# shellcheck disable=SC1090
[[ -r "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

emit() {
  curl -s -XPOST \
    "${INFLUX_URL}/api/v2/write?org=${INFLUX_ORG:-home}&bucket=${OPS_BUCKET}&precision=s" \
    -H "Authorization: Token ${INFLUX_OPS_TOKEN:-${INFLUX_ADMIN_TOKEN:-}}" \
    --data-binary "$1" >/dev/null 2>&1 || true
}

# du -sk → KiB; 0 when the dir is absent (cache never built, or already clean).
dir_bytes() {
  [[ -d "$1" ]] || { echo 0; return; }
  local kb; kb="$(/usr/bin/du -sk "$1" 2>/dev/null | /usr/bin/awk '{print $1}')"
  echo $(( ${kb:-0} * 1024 ))
}

start=$SECONDS
before=$(dir_bytes "$CACHE_DIR")
echo "[$(date)] e5bundlecache = ${before} bytes (threshold ${THRESHOLD_BYTES})"

triggered=0
reclaimed=0
if (( before > THRESHOLD_BYTES )); then
  triggered=1
  echo "[$(date)] over threshold — stopping mediaanalysisd and purging cache"
  /usr/bin/killall mediaanalysisd mediaanalysisd-access 2>/dev/null || true
  sleep 1
  /bin/rm -rf "$CACHE_DIR" 2>/dev/null || true
  after=$(dir_bytes "$CACHE_DIR")
  reclaimed=$(( before - after ))
  echo "[$(date)] reclaimed ${reclaimed} bytes (macOS will rebuild a clean copy)"
else
  echo "[$(date)] under threshold — nothing to do"
fi

DUR=$(( SECONDS - start ))
emit "maintenance,task=mediaanalysisd_cleanup,host=${HOSTTAG} success=1i,triggered=${triggered}i,bytes_before=${before}i,bytes_reclaimed=${reclaimed}i,duration_s=${DUR}i"
emit "collector,source=mediaanalysisd_cleanup,kind=batch,host=${HOSTTAG} success=1i,duration_s=${DUR}i,rows=1i,interval_s=604800i"
echo "[$(date)] done (triggered=${triggered}, reclaimed=${reclaimed} bytes, ${DUR}s)"
