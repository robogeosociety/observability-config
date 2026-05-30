#!/bin/zsh
# Auto-backup the tbd Obsidian vault. Git database lives OUTSIDE iCloud
# (~/git-repos/obsidian-tbd.git) with the work-tree pointed at the iCloud vault,
# so nothing git-related lives in the synced folder (no iCloud conflict copies).
#
# Emits a heartbeat to InfluxDB (bucket `ops`, measurement `backup`,
# target=obsidian) after each run — the "Backups" Grafana dashboard reads it.
# Reading the influxdb .env + vault both require Full Disk Access on /bin/zsh.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

GIT=/usr/bin/git
GD="$HOME/git-repos/obsidian-tbd.git"
WT="/Users/tommydoerr/Library/Mobile Documents/iCloud~md~obsidian/Documents/tbd"
INFLUX_ENV="/Volumes/dev/observability/influxdb/.env"
INFLUX_URL="${INFLUX_URL:-http://localhost:8086}"
g() { "$GIT" --git-dir="$GD" --work-tree="$WT" "$@"; }
ts() { date '+%F %T'; }

# Heartbeat to the ops bucket (best-effort; silent if influx/.env unavailable).
emit() {  # $1 = line-protocol fields, e.g. 'success=1i,bytes=123i'
  [ -r "$INFLUX_ENV" ] || return 0
  ( set -a; . "$INFLUX_ENV"; set +a
    curl -s -XPOST \
      "${INFLUX_URL}/api/v2/write?org=${INFLUX_ORG}&bucket=ops&precision=s" \
      -H "Authorization: Token ${INFLUX_ADMIN_TOKEN}" \
      --data-binary "backup,target=obsidian $1" >/dev/null 2>&1 ) || true
}

start=$SECONDS
# Bail if the vault isn't mounted/available
[ -d "$WT" ] || { echo "$(ts) vault not found at $WT"; exit 0; }

SIZE=$(( $(du -sk "$WT" 2>/dev/null | awk '{print $1}') * 1024 ))

g add -A
if g diff --cached --quiet; then
  echo "$(ts) no changes (vault ${SIZE} bytes)"
  emit "success=1i,bytes=${SIZE}i,duration_s=$(( SECONDS - start ))i"
  exit 0
fi

n=$(g diff --cached --name-only | wc -l | tr -d ' ')
if ! g commit -q -m "vault backup: $(ts) (${n} files)"; then
  echo "$(ts) commit failed"; emit "success=0i,bytes=${SIZE}i"; exit 1
fi
if g push -q origin main; then
  echo "$(ts) committed ${n} files, pushed"
  emit "success=1i,bytes=${SIZE}i,duration_s=$(( SECONDS - start ))i"
else
  echo "$(ts) committed ${n} files, push FAILED (will retry next run)"
  emit "success=0i,bytes=${SIZE}i,duration_s=$(( SECONDS - start ))i"
fi
