#!/bin/zsh
# Daily InfluxDB backup. Run via LaunchAgent.
# Strategy: dump inside the container to /tmp, docker cp to host, prune.

set -eu

ENV_FILE="/Volumes/dev/influxdb/.env"
BACKUP_ROOT="/Volumes/dev/influxdb/backups"
RETENTION_DAYS=30
DOCKER="/usr/local/bin/docker"
[[ -x "$DOCKER" ]] || DOCKER="/opt/homebrew/bin/docker"
[[ -x "$DOCKER" ]] || DOCKER="$(/usr/bin/env which docker)"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CONTAINER_PATH="/tmp/backup-${STAMP}"
HOST_PATH="${BACKUP_ROOT}/${STAMP}"

mkdir -p "$BACKUP_ROOT"

echo "[$(date)] starting backup ${STAMP}"
"$DOCKER" exec influxdb influx backup "$CONTAINER_PATH" \
  --org "$INFLUX_ORG" \
  --token "$INFLUX_ADMIN_TOKEN"

"$DOCKER" cp "influxdb:${CONTAINER_PATH}" "$HOST_PATH"
"$DOCKER" exec influxdb rm -rf "$CONTAINER_PATH"

echo "[$(date)] backup written to $HOST_PATH ($(du -sh "$HOST_PATH" | awk '{print $1}'))"

# Prune backups older than RETENTION_DAYS
find "$BACKUP_ROOT" -maxdepth 1 -type d -name '20*' -mtime "+${RETENTION_DAYS}" -print -exec rm -rf {} +

echo "[$(date)] done"
