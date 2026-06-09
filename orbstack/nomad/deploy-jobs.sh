#!/bin/zsh
# Register / tear down / inspect the per-container supervisor service jobs.
# These make the Nomad UI a real container console (one Running/Dead row per
# container, native Stop/Start/Restart). Run from this dir.
#
#   ./deploy-jobs.sh up        # register every ctl-*.hcl in this dir
#   ./deploy-jobs.sh down      # stop + purge them
#   ./deploy-jobs.sh status    # nomad status for each
#
# SUPERVISE overrides the supervisor script path (default: the canonical
# /Volumes checkout). Used to register from a worktree before merge:
#   SUPERVISE="$PWD/supervise.sh" ./deploy-jobs.sh up
set -euo pipefail
cd "${0:A:h}"

cmd="${1:-status}"
files=(ctl-*.hcl)
var_args=()
[[ -n "${SUPERVISE:-}" ]] && var_args=(-var "supervise=${SUPERVISE}")

case "$cmd" in
  up)
    for f in $files; do
      print ">> run $f"
      nomad job run $var_args "$f"
    done ;;
  down)
    for f in $files; do
      job="${${f:t:r}}"          # ctl-grafana.hcl -> ctl-grafana
      print ">> stop -purge $job"
      nomad job stop -purge "$job" || true
    done ;;
  status)
    for f in $files; do
      job="${${f:t:r}}"
      print "── $job ──"
      nomad job status -short "$job" 2>/dev/null || print "   (not registered)"
    done ;;
  *)
    print -u2 "usage: ${0:t} <up|down|status>"; exit 2 ;;
esac
