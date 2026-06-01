#!/bin/zsh
# enqueue.sh — atomically submit a job to the coordinator queue.
#
#   enqueue.sh deploy "reason"
#
# Atomic by construction: write to a hidden temp file in the queue dir, then
# rename it into place (rename within a dir is atomic), so the draining worker
# never sees a half-written job. Filenames are timestamp-prefixed for FIFO order.
set -eu

: "${COORD_HOME:=$HOME/.observability/coordinator}"
QUEUE="$COORD_HOME/queue"

type="${1:?usage: enqueue.sh <type> [reason]}"
reason="${2:-unspecified}"

mkdir -p "$QUEUE"
tmp="$(mktemp "$QUEUE/.tmp.XXXXXX")"
{
  print -r -- "type: $type"
  print -r -- "reason: $reason"
  print -r -- "requested_by: ${USER:-unknown}"
  print -r -- "ts: $(/bin/date +%s)"
} > "$tmp"

dest="$QUEUE/$(/bin/date +%s)-${type}-$$.job"
mv "$tmp" "$dest"
print -r -- "enqueued ${dest:t}"
