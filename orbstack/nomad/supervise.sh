#!/bin/zsh
# Nomad service-task supervisor for ONE OrbStack container. It makes a Nomad
# `service` job mirror + control the container's lifecycle, so the Nomad UI shows
# the container as a live Running/Dead row with native Stop / Start / Restart,
# logs, and resource graphs — instead of the opaque batch-dispatch records the
# old parameterized job produced.
#
#   job starts  -> docker start  (alloc becomes Running)
#   Stop Job    -> SIGTERM -> docker stop  (alloc ends, container stops)
#   Restart     -> Nomad reruns the task -> docker start again
#   container exits on its own -> the alloc ends too, so status stays honest
#
# raw_exec runs this as the host user, who owns OrbStack's docker.sock, so the
# `docker` CLI reaches the engine with no extra perms. attempts=0 in the job means
# we never auto-restart — the job reflects reality, it doesn't fight a redeploy.
set -uo pipefail
container="${1:?container required}"

if ! docker inspect --type container "$container" >/dev/null 2>&1; then
  print -u2 "no such container: $container"
  exit 3
fi

# Ensure running (no-op if already up); once this returns the alloc is Running.
docker start "$container" >/dev/null

# Stop the Nomad job -> SIGTERM -> stop the container and exit cleanly.
# Background `docker wait` + `wait` so the trap can fire (a foreground wait would
# swallow the signal). When the container exits on its own, `wait` returns and the
# script falls through to exit 0, ending the alloc.
docker wait "$container" >/dev/null 2>&1 &
wpid=$!
trap 'docker stop "$container" >/dev/null 2>&1; kill $wpid 2>/dev/null; exit 0' TERM INT
wait $wpid
