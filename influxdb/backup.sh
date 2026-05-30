#!/bin/zsh
# Daily InfluxDB backup → external 2TB disk (/Volumes/dev/observability/influxdb/backups).
#
# Runs via LaunchAgent dev.tommydoerr.influxdb-backup (ProgramArguments: /bin/zsh
# <this script>). launchd-spawned processes are TCC-gated from /Volumes, so this
# REQUIRES Full Disk Access granted to /bin/zsh
# (System Settings → Privacy & Security → Full Disk Access → add /bin/zsh).
#
# Design: every /Volumes touch (read .env, write the tarball, prune) is done by
# THIS zsh process, so the single /bin/zsh FDA grant is sufficient. The dump is
# streamed out of the container via a zsh redirect instead of `docker cp`, so the
# docker CLI never writes to /Volumes (no separate grant needed).
set -euo pipefail

ENV_FILE="/Volumes/dev/observability/influxdb/.env"
BACKUP_ROOT="/Volumes/dev/observability/influxdb/backups"
RETENTION_DAYS=30
DOCKER="/usr/local/bin/docker"
[[ -x "$DOCKER" ]] || DOCKER="/opt/homebrew/bin/docker"
[[ -x "$DOCKER" ]] || DOCKER="$(/usr/bin/env which docker)"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CONTAINER_DIR="/tmp/backup-${STAMP}"
HOST_TARBALL="${BACKUP_ROOT}/influx-${STAMP}.tar.gz"

mkdir -p "$BACKUP_ROOT"
echo "[$(date)] starting backup ${STAMP}"

# Dump inside the container, then stream it out via a zsh redirect (zsh, which has
# FDA, writes the /Volumes file — docker only talks to the daemon).
"$DOCKER" exec influxdb influx backup "$CONTAINER_DIR" \
  --org "$INFLUX_ORG" --token "$INFLUX_ADMIN_TOKEN"
"$DOCKER" exec influxdb tar -C /tmp -czf - "backup-${STAMP}" > "$HOST_TARBALL"
"$DOCKER" exec influxdb rm -rf "$CONTAINER_DIR"

echo "[$(date)] wrote $HOST_TARBALL ($(du -h "$HOST_TARBALL" | awk '{print $1}'))"

# Prune tarballs older than RETENTION_DAYS (zsh performs the /Volumes delete).
find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'influx-*.tar.gz' \
  -mtime "+${RETENTION_DAYS}" -print -delete

echo "[$(date)] done"
