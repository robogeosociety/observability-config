#!/bin/zsh
# Capture evidence at the moment /Volumes/dev wedges — before anyone fixes it.
#
# THE GAP IT CLOSES: every investigation of this failure so far has been
# archaeology, and every one of them ran out of evidence.
#   - rgs#172 (07-22) and tommybot#101 (07-23) — kernel logs long gone. Retention
#     on this host is ~22 h at its message volume, not the assumed several days.
#   - infra#25 (07-28) — OrbStack rotates vmgr.log on EVERY VM start, so the
#     `orb stop && orb start` that fixes the wedge also destroys the log that
#     would explain it. The 07-28 window was overwritten this way.
# The fix and the evidence are the same action, so evidence has to be taken
# first, automatically, by something that is already watching.
#
# Called by stack-watchdog.sh on the healthy->wedged transition. Runs DETACHED so
# it can never delay the watchdog's 120 s cycle.
#
# Ordering is deliberate: OrbStack logs first (a fix destroys them), then cheap
# host state, then the things that touch /Volumes/dev last — because those are
# the calls that will block, and a blocked capture must not cost us the rest.
#
# Every command is deadline-guarded. macOS has no timeout(1), and on a wedged
# volume lsof/diskutil/du block in the kernel for minutes.
#
# Usage:
#   capture-wedge.sh [reason]      # normally invoked by stack-watchdog.sh
#   capture-wedge.sh --dry-run     # capture now against a healthy volume, to
#                                  # prove the collector works before it matters
set -u

DEST_ROOT="${WEDGE_CAPTURE_DIR:-$HOME/.local/share/stack-watchdog/captures}"
DISK_PATH="${WATCHDOG_DISK_PATH:-/Volumes/dev}"
REASON="${1:-unspecified}"
[ "$REASON" = "--dry-run" ] && REASON="dry-run (volume presumed healthy)"

# The capture must land on the INTERNAL disk. Writing evidence about a wedged
# volume onto that same volume would block forever and produce nothing.
case "$DEST_ROOT" in
  /Volumes/*)
    print -u2 "refusing to write captures to $DEST_ROOT — that is the volume under investigation"
    exit 2 ;;
esac

stamp=$(date +%Y%m%dT%H%M%S)
DEST="$DEST_ROOT/$stamp"
mkdir -p "$DEST" || exit 2

export PATH="/opt/homebrew/bin:$HOME/.orbstack/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# run_guarded <deadline_secs> <label> <command...>
# Never lets one blocked call cost us the rest of the capture.
run_guarded() {
  local deadline="$1" label="$2"; shift 2
  local out="$DEST/$label.txt" waited=0 pid
  ( "$@" >"$out" 2>&1 ) &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$deadline" ]; then
      kill -9 "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      print -r -- "" >>"$out"
      print -r -- "*** TIMED OUT after ${deadline}s — the call itself blocking IS a finding ***" >>"$out"
      print -r -- "  timeout: $label (${deadline}s)" >>"$DEST/00-INDEX.txt"
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid" 2>/dev/null
  # Record the byte count, not just "ok". A collector that runs cleanly and
  # writes nothing looks identical to a working one otherwise — which is exactly
  # how the kernel-log collector shipped broken (zsh's `log` builtin shadowing
  # /usr/bin/log, failing with "too many arguments" into an unread file).
  local sz=$(wc -c <"$out" 2>/dev/null | tr -d ' ')
  : "${sz:=0}"
  # Only a genuinely empty file is an error. Some collectors are legitimately
  # terse — `orb status` prints "Running", and rawdev-read prints a permission
  # error unless run as root. Sizes are recorded either way so a human or an
  # agent reading this index can judge.
  if [ "$sz" -eq 0 ]; then
    print -r -- "  EMPTY:   $label (0B) — this collector produced nothing" >>"$DEST/00-INDEX.txt"
  else
    print -r -- "  ok:      $label (${sz}B)" >>"$DEST/00-INDEX.txt"
  fi
  return 0
}

{
  print -r -- "wedge capture $stamp"
  print -r -- "host:    $(hostname -s)"
  print -r -- "reason:  $REASON"
  print -r -- "uptime:  $(uptime)"
  print -r -- "volume:  $DISK_PATH"
  print -r -- ""
  print -r -- "collected:"
} > "$DEST/00-INDEX.txt"

# ── 1. OrbStack logs — FIRST. `orb stop && orb start` rotates these away, and
#       that restart is the documented fix, so this is the only chance to get them.
#       These are small (KBs) and are the ones that vanish.
for f in "$HOME/.orbstack/log/vmgr.log" "$HOME/.orbstack/log/vmgr.1.log" "$HOME/.orbstack/log/gui.log"; do
  [ -r "$f" ] && cp "$f" "$DEST/orbstack-$(basename "$f")" 2>/dev/null
done
# Today's daily vmgr archive too — also small.
cp "$HOME/Library/Logs/OrbStack/vmgr-$(date +%Y-%m-%d).log" "$DEST/" 2>/dev/null
#
# NOT the unified-*.log archives. They are 100-280 MB EACH and the whole set is
# ~2.4 GB — copying them would fill the disk in a handful of captures, and it is
# redundant: orbstack-log-capture already retains them 30 days. Take a bounded
# extract instead, and record where the originals are.
{
  print -r -- "Full unified logs are NOT copied (~2.4 GB). Originals, retained 30 days:"
  ls -lh "$HOME/Library/Logs/OrbStack/"unified-*.log 2>/dev/null | tail -4
} > "$DEST/orbstack-unified-pointer.txt"
# Today's archive may not exist yet — orbstack-log-capture writes it once daily,
# mid-morning. Fall back to the newest one that does.
unified_latest=$(ls -1t "$HOME/Library/Logs/OrbStack/"unified-*.log 2>/dev/null | head -1)
[ -n "$unified_latest" ] && run_guarded 60 orbstack-unified-tail tail -n 4000 "$unified_latest"
run_guarded 10 orbstack-status orb status
run_guarded 10 orbstack-config orb config show

# ── 2. The drive. Cheap, no /Volumes access, and the discriminator that needs no
#       root: if these counters moved off zero it is the drive; if they are still
#       zero while the volume is unreadable, the fault is above it.
#       Baseline as of 2026-07-30: 0 error-log entries, 0 media errors, 0% used.
whole=$(diskutil info "$DISK_PATH" 2>/dev/null | awk -F': *' '/Part of Whole/{print $2}' | tr -d ' ')
: "${whole:=disk5}"
print -r -- "whole disk: $whole" >> "$DEST/00-INDEX.txt"
run_guarded 20 smart-health   smartctl -a "/dev/$whole"
run_guarded 20 smart-errorlog smartctl -l error "/dev/$whole"

# ── 3. Host state. None of this touches the volume.
run_guarded 10 swap        sysctl vm.swapusage vm.compressor_bytes_used
run_guarded 10 vmstat      vm_stat
run_guarded 15 processes   ps aux
run_guarded 10 virtualization pgrep -fl Virtualization.VirtualMachine
run_guarded 15 docker-ps   docker ps -a
run_guarded 30 thunderbolt system_profiler SPThunderboltDataType
run_guarded 30 nvme        system_profiler SPNVMeDataType

# ── 4. Kernel log. Bounded window: retention is ~22 h here, so a wedge older than
#       that has already lost this. 30 min is enough for onset and cheap to write.
#
#       /usr/bin/log by absolute path, NOT `log`: zsh has a `log` BUILTIN that
#       shadows it and fails with "too many arguments" — silently collecting
#       nothing, which is the worst possible failure for an evidence collector.
run_guarded 90 kernel-30m /usr/bin/log show --last 30m --style compact \
  --predicate 'process == "kernel" OR process == "launchd" OR eventMessage CONTAINS[c] "virtiofs" OR eventMessage CONTAINS[c] "Virtualization" OR eventMessage CONTAINS[c] "NVMe" OR eventMessage CONTAINS[c] "Thunderbolt" OR eventMessage CONTAINS[c] "USB4" OR eventMessage CONTAINS[c] "apfs" OR eventMessage CONTAINS[c] "memorystatus"'

# ── 5. Anything touching the volume goes LAST. These are the calls that block on
#       a wedge — and a timeout here is itself the measurement, so short deadlines.
run_guarded 20 diskutil-info  diskutil info "$DISK_PATH"
run_guarded 20 diskutil-list  diskutil list
run_guarded 25 lsof-volume    lsof -n "$DISK_PATH"
run_guarded 15 readdir-retry  ls -1 "$DISK_PATH"
run_guarded 15 mount          mount

# The raw-device read is the true discriminator between the two failure layers:
# virtiofs sits above APFS which sits above the block device, so a userspace
# sharing layer cannot make this block. Needs root, so it usually records a
# permission error rather than a result — that is expected, not a bug. If it is
# ever run as root and BLOCKS, the fault is the device.
run_guarded 15 rawdev-read dd "if=/dev/r$whole" of=/dev/null bs=4096 count=1

# The kernel slice is ~15 MB uncompressed and is by far the bulk of a capture.
# It is also the artefact every prior investigation lacked, so it is kept in full
# and compressed rather than truncated.
[ -f "$DEST/kernel-30m.txt" ] && gzip -f "$DEST/kernel-30m.txt" 2>/dev/null

print -r -- "" >> "$DEST/00-INDEX.txt"
print -r -- "finished: $(date '+%F %T')" >> "$DEST/00-INDEX.txt"
print -r -- "size:     $(du -sh "$DEST" 2>/dev/null | cut -f1)" >> "$DEST/00-INDEX.txt"

# Keep the last 20 captures; they are small and the volume they describe is not.
ls -1dt "$DEST_ROOT"/*(/N) 2>/dev/null | tail -n +21 | while read -r old; do
  rm -rf "$old"
done

print -r -- "$DEST"
