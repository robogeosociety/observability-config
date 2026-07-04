#!/bin/zsh
# ci_gate.sh <sha> — decide whether a commit is safe to auto-deploy, by its CI status.
#
# The coordinator (worker.sh) poll-deploys origin/main the moment it advances. On a
# private repo on the free plan there is no branch protection, so a red-CI merge would
# otherwise go live within one tick. This gate makes the pull-deploy CI-aware: the
# always-on coordinator becomes the CI-gated-CD "pilot light".
#
# Echoes exactly one word on stdout:
#   deploy          — safe to deploy this SHA
#   skip:ci-failed  — CI ran and did NOT pass → hold (wait for a green SHA)
#   skip:ci-pending — CI hasn't reported success yet → defer to a later tick
#
# Signal: the most recent run of $COORD_CI_WORKFLOW (the hermetic `tests` workflow)
# on this EXACT head SHA —
#   completed + success              → deploy
#   completed + (failure/cancel/…)   → skip:ci-failed
#   queued / in_progress / no run    → skip:ci-pending
#   gh missing OR query errored      → deploy   (degrade OPEN — a broken gate must
#                                                 never freeze deploys; the gate only
#                                                 ever WITHHOLDS on an affirmative
#                                                 not-green signal)
#
# Auth: `gh` uses the host OAuth token (holy-trinity — never a minted PAT). Under
# launchd the keychain may be locked, so the coordinator env (~/.observability/
# coordinator/env, chmod 600) can carry a scoped read-only GH_TOKEN (Actions:read,
# Contents:read) which `gh`/`gh api` pick up automatically — the same documented
# unattended-token pattern as INFLUX_OPS_TOKEN. No token + locked keychain ⇒ the
# query errors ⇒ degrade open (logged by the caller).
#
# Testable in isolation: set COORD_CI_CHECK_CMD to a stub command that takes <sha>
# and echoes a raw "<status>:<conclusion>" (e.g. "completed:success",
# "in_progress:null", "none") — ci_gate.sh maps it the same way it maps gh's output.
set -eu

sha="${1:?usage: ci_gate.sh <sha>}"
: "${COORD_REQUIRE_CI:=1}"
: "${COORD_CI_WORKFLOW:=test.yml}"
: "${COORD_REPO_SLUG:=robogeosociety/observability-config}"

# Gate disabled → always green-light (break-glass: COORD_REQUIRE_CI=0 for a manual deploy).
[ "$COORD_REQUIRE_CI" = "1" ] || { print -- deploy; exit 0; }

# Resolve the raw "<status>:<conclusion>" for this SHA, or "none" if no run exists.
# A non-zero exit from the source (gh error / stub failure) ⇒ degrade OPEN.
if [ -n "${COORD_CI_CHECK_CMD:-}" ]; then
  raw="$(eval "$COORD_CI_CHECK_CMD ${sha}")" || { print -- deploy; exit 0; }
elif command -v gh >/dev/null 2>&1; then
  raw="$(gh api \
    "repos/${COORD_REPO_SLUG}/actions/workflows/${COORD_CI_WORKFLOW}/runs?head_sha=${sha}&per_page=1" \
    --jq 'if (.workflow_runs | length) > 0
          then (.workflow_runs[0].status + ":" + (.workflow_runs[0].conclusion // "null"))
          else "none" end')" || { print -- deploy; exit 0; }
else
  print -- deploy; exit 0   # no gh on PATH → can't check → degrade open
fi

# gh/stub succeeded; map the reported state. Only an affirmative not-green withholds.
case "$raw" in
  completed:success) print -- deploy ;;
  completed:*)       print -- skip:ci-failed ;;   # failure / cancelled / timed_out / …
  *)                 print -- skip:ci-pending ;;  # queued / in_progress / requested / none / …
esac
