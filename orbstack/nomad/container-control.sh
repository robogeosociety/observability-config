#!/bin/zsh
# Task script for the `container-control` Nomad parameterized batch job: start,
# stop, or restart one OrbStack container via the host Docker CLI.
#
# Args (interpolated from dispatch -meta by container-control.hcl):
#   $1 = action     start | stop | restart
#   $2 = container  container name (or id)
#
# raw_exec runs this as the host user, who owns OrbStack's docker.sock, so `docker`
# reaches the engine with no extra perms. Two guards keep a stray dispatch from
# being a foot-gun: the action is allowlisted, and the container must already exist
# (a typo'd name fails loudly instead of doing nothing).
set -euo pipefail

action="${1:-}"
container="${2:-}"

if [[ -z "$action" || -z "$container" ]]; then
  print -u2 "usage: ${0:t} <start|stop|restart> <container>"
  exit 2
fi

case "$action" in
  start|stop|restart) ;;
  *) print -u2 "invalid action '$action' (want: start|stop|restart)"; exit 2 ;;
esac

if ! docker inspect --type container "$container" >/dev/null 2>&1; then
  print -u2 "no such container: '$container'"
  exit 3
fi

print "container-control: docker ${action} ${container}"
exec docker "$action" "$container"
