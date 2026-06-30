# mutex.sh — atomic mkdir mutex for the observability coordinator.
#
# Source this (don't exec it). It defines the coordinator paths from $COORD_HOME
# and three helpers: coord_acquire_lock / coord_release_lock / coord_lock_held.
#
# Why mkdir: it's atomic on APFS, needs no flock (absent on macOS) and no deps.
# A single holder at a time across launchd ticks AND a manual deploy. Stale locks
# are reclaimed so a crashed run can't wedge it, in two cases: (a) the owner file
# names a dead PID older than the TTL, or (b) the owner file is missing entirely —
# a run died between `mkdir LOCK` and writing `owner` (see the orphan branch).

: "${COORD_HOME:=$HOME/.observability/coordinator}"
: "${COORD_LOCK:=$COORD_HOME/LOCK}"
: "${COORD_LOCK_TTL:=900}"           # seconds; a held lock with a dead PID older than this is stale
: "${COORD_LOCK_ORPHAN_GRACE:=30}"   # seconds; an owner-LESS lock dir older than this is stale

coord_now() { /bin/date +%s; }

# Epoch mtime of $1. Portable across GNU stat (Linux — the hermetic CI runner) and
# BSD stat (macOS — the prod mini): try `-c %Y` first, fall back to `-f %m`.
coord_mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || print 0; }

# 0 = acquired (caller now owns it), 1 = held by a live owner (try again later).
coord_acquire_lock() {
  mkdir -p "$COORD_HOME"
  if mkdir "$COORD_LOCK" 2>/dev/null; then
    printf '%s %s\n' "$$" "$(coord_now)" > "$COORD_LOCK/owner"
    return 0
  fi
  # Lock dir exists — reclaim only if it's provably stale.
  local pid ts age reclaim=0
  if [[ -r "$COORD_LOCK/owner" ]] && read -r pid ts < "$COORD_LOCK/owner"; then
    # Owner recorded: stale iff its PID is gone AND it's older than the TTL.
    age=$(( $(coord_now) - ts ))
    if ! kill -0 "$pid" 2>/dev/null && [ "$age" -gt "$COORD_LOCK_TTL" ]; then
      reclaim=1
    fi
  else
    # No readable owner file: a run died between `mkdir LOCK` and writing `owner`
    # (or is in the sub-second window before it writes). Without this branch the
    # lock is UNRECLAIMABLE — the TTL check above needs an owner to read, so an
    # owner-less LOCK wedges every future deploy forever (2026-06-29: ~12h of
    # missed deploys). Reclaim once the lock DIR's own mtime is past the orphan
    # grace — long enough that any live holder has surely written `owner` by now.
    local mtime
    mtime=$(coord_mtime "$COORD_LOCK")
    if [ "$mtime" -gt 0 ] && [ "$(( $(coord_now) - mtime ))" -gt "$COORD_LOCK_ORPHAN_GRACE" ]; then
      reclaim=1
    fi
  fi
  if [ "$reclaim" -eq 1 ]; then
    rm -rf "$COORD_LOCK"
    if mkdir "$COORD_LOCK" 2>/dev/null; then
      printf '%s %s\n' "$$" "$(coord_now)" > "$COORD_LOCK/owner"
      return 0
    fi
  fi
  return 1
}

# Release only if we hold it (don't yank someone else's lock).
coord_release_lock() {
  local pid
  if [[ -r "$COORD_LOCK/owner" ]] && read -r pid _ < "$COORD_LOCK/owner" && [ "$pid" = "$$" ]; then
    rm -rf "$COORD_LOCK"
  fi
}

coord_lock_held() { [ -d "$COORD_LOCK" ]; }
