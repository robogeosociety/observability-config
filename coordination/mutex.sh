# mutex.sh — atomic mkdir mutex for the observability coordinator.
#
# Source this (don't exec it). It defines the coordinator paths from $COORD_HOME
# and three helpers: coord_acquire_lock / coord_release_lock / coord_lock_held.
#
# Why mkdir: it's atomic on APFS, needs no flock (absent on macOS) and no deps.
# A single holder at a time across launchd ticks AND a manual deploy. Stale locks
# (dead owner PID + age past TTL) are reclaimed so a crashed run can't wedge it.

: "${COORD_HOME:=$HOME/.observability/coordinator}"
: "${COORD_LOCK:=$COORD_HOME/LOCK}"
: "${COORD_LOCK_TTL:=900}"   # seconds; a held lock older than this with a dead PID is stale

coord_now() { /bin/date +%s; }

# 0 = acquired (caller now owns it), 1 = held by a live owner (try again later).
coord_acquire_lock() {
  mkdir -p "$COORD_HOME"
  if mkdir "$COORD_LOCK" 2>/dev/null; then
    printf '%s %s\n' "$$" "$(coord_now)" > "$COORD_LOCK/owner"
    return 0
  fi
  # Lock dir exists — reclaim only if the owner is gone AND it's old.
  local pid ts age
  if read -r pid ts < "$COORD_LOCK/owner" 2>/dev/null; then
    age=$(( $(coord_now) - ts ))
    if ! kill -0 "$pid" 2>/dev/null && [ "$age" -gt "$COORD_LOCK_TTL" ]; then
      rm -rf "$COORD_LOCK"
      if mkdir "$COORD_LOCK" 2>/dev/null; then
        printf '%s %s\n' "$$" "$(coord_now)" > "$COORD_LOCK/owner"
        return 0
      fi
    fi
  fi
  return 1
}

# Release only if we hold it (don't yank someone else's lock).
coord_release_lock() {
  local pid
  if read -r pid _ < "$COORD_LOCK/owner" 2>/dev/null && [ "$pid" = "$$" ]; then
    rm -rf "$COORD_LOCK"
  fi
}

coord_lock_held() { [ -d "$COORD_LOCK" ]; }
