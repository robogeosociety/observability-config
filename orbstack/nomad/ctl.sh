#!/bin/zsh
# Terminal companion to the Nomad UI: start / stop / restart one supervised
# container by driving its `ctl-<container>` service job. The UI (Jobs list →
# Running/Dead row → Stop/Start/Restart) is the primary surface; this is for when
# you're already in a shell. Run from this dir, after deploy-jobs.sh up.
#
#   ./ctl.sh start   grafana
#   ./ctl.sh stop    transit-tracker
#   ./ctl.sh restart influxdb
set -euo pipefail
cd "${0:A:h}"

# All ctl-* jobs live in the orbstack namespace (the "parent" grouping).
export NOMAD_NAMESPACE="orbstack"

action="${1:-}"
container="${2:-}"
if [[ -z "$action" || -z "$container" ]]; then
  print -u2 "usage: ${0:t} <start|stop|restart> <container>"
  print -u2 "containers: ${${(f)$(ls ctl-*.hcl)}//ctl-/}"
  exit 2
fi

job="ctl-${container}"
hcl="${job}.hcl"

# SUPERVISE overrides the supervisor path (default: canonical /Volumes checkout) —
# matches deploy-jobs.sh, for registering from a worktree before merge.
var_args=()
[[ -n "${SUPERVISE:-}" ]] && var_args=(-var "supervise=${SUPERVISE}")

case "$action" in
  start)   [[ -f "$hcl" ]] || { print -u2 "no job file: $hcl"; exit 3; }
           nomad job run $var_args "$hcl" ;;
  stop)    nomad job stop "$job" ;;          # SIGTERM -> supervisor docker-stops it
  restart) nomad job restart -yes "$job" ;;  # -yes: non-interactive; restarts the
                                             # task in place -> SIGTERM (docker stop)
                                             # then start -> docker restart
  *) print -u2 "invalid action '$action' (want: start|stop|restart)"; exit 2 ;;
esac
