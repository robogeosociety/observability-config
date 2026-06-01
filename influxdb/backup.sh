#!/bin/zsh
# Daily InfluxDB backup → external 2TB disk + (optional) Cloudflare R2 offsite.
#
# Runs via LaunchAgent dev.tommydoerr.influxdb-backup (ProgramArguments:
# /bin/zsh <this script>). launchd-spawned processes are TCC-gated from /Volumes,
# so this REQUIRES Full Disk Access granted to /bin/zsh (System Settings →
# Privacy & Security → Full Disk Access). All /Volumes access is done by this zsh
# process; the dump is streamed out of the container via a redirect (not docker
# cp) so a single grant suffices.
#
# Emits a heartbeat to InfluxDB (bucket `ops`, measurement `backup`) after each
# run — the "Backups" Grafana dashboard reads it. R2 upload happens only when
# R2_* creds are present in .env (see .env.example); until then it's local-only.
set -uo pipefail

SCRIPT_DIR="${0:A:h}"
ENV_FILE="${SCRIPT_DIR}/.env"
BACKUP_ROOT="/Volumes/dev/observability/influxdb/backups"
OPS_BUCKET="ops"
INFLUX_URL="${INFLUX_URL:-http://localhost:8086}"
RETENTION_DAYS=30
DOCKER="/usr/local/bin/docker"
[[ -x "$DOCKER" ]] || DOCKER="/opt/homebrew/bin/docker"
[[ -x "$DOCKER" ]] || DOCKER="$(/usr/bin/env which docker)"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CONTAINER_DIR="/tmp/backup-${STAMP}"
HOST_TARBALL="${BACKUP_ROOT}/influx-${STAMP}.tar.gz"

# Write one line of InfluxDB line protocol to the ops bucket (best-effort).
emit() {
  curl -s -XPOST \
    "${INFLUX_URL}/api/v2/write?org=${INFLUX_ORG}&bucket=${OPS_BUCKET}&precision=s" \
    -H "Authorization: Token ${INFLUX_OPS_TOKEN:-$INFLUX_ADMIN_TOKEN}" \
    --data-binary "$1" >/dev/null 2>&1 || true
}
HOSTTAG="$(hostname -s 2>/dev/null || echo unknown)"
fail() {
  emit "backup,target=local success=0i"
  # Generic collector heartbeat for the Collectors dashboard (see COLLECTORS.md).
  emit "collector,source=backup,kind=batch,host=${HOSTTAG} success=0i,interval_s=86400i"
  echo "[$(date)] backup FAILED: $1"
  exit 1
}

mkdir -p "$BACKUP_ROOT"
echo "[$(date)] starting backup ${STAMP}"
start=$SECONDS

# Dump inside the container, stream it out via a zsh redirect (zsh writes /Volumes).
"$DOCKER" exec influxdb influx backup "$CONTAINER_DIR" \
  --org "$INFLUX_ORG" --token "$INFLUX_ADMIN_TOKEN" || fail "influx backup"
"$DOCKER" exec influxdb tar -C /tmp -czf - "backup-${STAMP}" > "$HOST_TARBALL" || fail "stream dump"
"$DOCKER" exec influxdb rm -rf "$CONTAINER_DIR" || true
[[ -s "$HOST_TARBALL" ]] || fail "empty tarball"

SIZE=$(stat -f%z "$HOST_TARBALL")
DUR=$(( SECONDS - start ))
emit "backup,target=local success=1i,bytes=${SIZE}i,duration_s=${DUR}i"
emit "collector,source=backup,kind=batch,host=${HOSTTAG} success=1i,duration_s=${DUR}i,rows=1i,interval_s=86400i"
echo "[$(date)] local backup ok: $HOST_TARBALL (${SIZE} bytes, ${DUR}s)"

# Optional offsite copy to Cloudflare R2 (activated by adding R2 creds to .env).
# Auth is a single CLOUDFLARE_API_TOKEN (Workers R2 Storage:Edit) — r2_upload.py
# shells out to wrangler, so no S3 access-key pair and no boto3 are needed.
if [[ -n "${CLOUDFLARE_API_TOKEN:-}" && -n "${R2_BUCKET:-}" ]]; then
  if python3 "${SCRIPT_DIR}/r2_upload.py" put "$HOST_TARBALL" "influx-${STAMP}.tar.gz"; then
    emit "backup,target=r2 success=1i,bytes=${SIZE}i"
    echo "[$(date)] uploaded to r2://${R2_BUCKET}/influx-${STAMP}.tar.gz"
  else
    emit "backup,target=r2 success=0i"
    echo "[$(date)] R2 upload FAILED"
  fi
else
  echo "[$(date)] R2 creds not set — skipping offsite upload (local-only)"
fi

# Prune local tarballs older than RETENTION_DAYS.
find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'influx-*.tar.gz' \
  -mtime "+${RETENTION_DAYS}" -print -delete

echo "[$(date)] done"
