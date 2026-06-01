#!/bin/zsh
# enable-merge-queue.sh — turn on a GitHub merge queue + required checks on `main`
# via a repository ruleset.
#
#   ./enable-merge-queue.sh            # dry run: print the ruleset payload, change nothing
#   ./enable-merge-queue.sh --apply    # create the ruleset (governance change!)
#
# After --apply, ALL changes to `main` go through a PR and the merge queue — no
# direct pushes. Squash is the queue merge method (matches this repo's history).
# To undo: gh api repos/$REPO/rulesets  → find the id → gh api -X DELETE repos/$REPO/rulesets/<id>
set -eu

REPO="${COORD_REPO_SLUG:-tommyroar/observability-config}"
CHECK="${COORD_REQUIRED_CHECK:-hermetic}"

read -r -d '' PAYLOAD <<JSON || true
{
  "name": "main-merge-queue",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/main"], "exclude": [] } },
  "rules": [
    { "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false
      } },
    { "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "do_not_enforce_on_create": false,
        "required_status_checks": [ { "context": "$CHECK" } ]
      } },
    { "type": "merge_queue",
      "parameters": {
        "merge_method": "SQUASH",
        "grouping_strategy": "ALLGREEN",
        "check_response_timeout_minutes": 60,
        "min_entries_to_merge": 1,
        "min_entries_to_merge_wait_minutes": 0,
        "max_entries_to_merge": 5,
        "max_entries_to_build": 5
      } }
  ]
}
JSON

if [ "${1:-}" != "--apply" ]; then
  print -r -- "DRY RUN — would create this ruleset on $REPO:"
  print -r -- "$PAYLOAD"
  print -r -- "\nre-run with --apply to create it (this is a governance change)."
  exit 0
fi

print -r -- "$PAYLOAD" | gh api -X POST "repos/$REPO/rulesets" --input -
print -r -- "merge queue enabled on $REPO main (required check: $CHECK)."
