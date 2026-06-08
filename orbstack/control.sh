#!/bin/zsh
# Convenience wrapper around the `container-control` Nomad job: dispatch a
# start / stop / restart for one OrbStack container, or for every managed stack
# container at once. The wrapper just dispatches — Nomad runs the actual docker
# command (in nomad/container-control.sh) so the action shows up in
# `nomad job status` with retries/dead-job visibility.
#
#   ./control.sh <start|stop|restart> <container>
#   ./control.sh restart grafana
#   ./control.sh restart --all          # every container in MANAGED below
#
# Register the job once (maintainer step, after merge):
#   nomad job run nomad/container-control.hcl
set -euo pipefail

# Long-lived stack containers worth one-touch control. Pass an explicit name to
# act on anything the engine knows (e.g. an ad-hoc mcp/grafana helper); --all is
# deliberately scoped to these so it can't sweep ephemeral containers.
MANAGED=(grafana influxdb transit-tracker grafana-renderer)

action="${1:-}"
target="${2:-}"

if [[ -z "$action" || -z "$target" ]]; then
  print -u2 "usage: ${0:t} <start|stop|restart> <container|--all>"
  print -u2 "managed: ${MANAGED[*]}"
  exit 2
fi

case "$action" in
  start|stop|restart) ;;
  *) print -u2 "invalid action '$action' (want: start|stop|restart)"; exit 2 ;;
esac

dispatch() {
  local c="$1"
  print ">> dispatch ${action} ${c}"
  nomad job dispatch -meta "action=${action}" -meta "container=${c}" container-control
}

if [[ "$target" == "--all" ]]; then
  for c in "${MANAGED[@]}"; do dispatch "$c"; done
else
  dispatch "$target"
fi
