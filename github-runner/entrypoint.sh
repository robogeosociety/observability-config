#!/usr/bin/env bash
# Register an ephemeral self-hosted runner from a PAT-minted registration token, run one
# job, then unregister on exit. Repo-scoped by default (tommyroar is a user account); set
# GH_SCOPE=org for an org account.
set -euo pipefail

: "${GH_OWNER:?set GH_OWNER (e.g. tommyroar)}"
: "${RUNNER_PAT:?set RUNNER_PAT — a fine-grained PAT with Administration:write on the target repo(s), used only to mint short-lived registration tokens}"
GH_SCOPE="${GH_SCOPE:-repo}" # repo | org
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,linux,arm64,orbstack,mini}"
RUNNER_NAME="${RUNNER_NAME:-mini-orbstack-$(cut -c1-8 /proc/sys/kernel/random/uuid)}"

if [ "$GH_SCOPE" = "org" ]; then
  api="orgs/${GH_OWNER}"
  url="https://github.com/${GH_OWNER}"
else
  : "${GH_REPO:?set GH_REPO (the repo name) for repo-scoped runners}"
  api="repos/${GH_OWNER}/${GH_REPO}"
  url="https://github.com/${GH_OWNER}/${GH_REPO}"
fi

# Mint a short-lived token via the PAT (the PAT itself never touches the runner config).
gh_token() { # $1 = registration-token | remove-token
  curl -fsSL -X POST \
    -H "Authorization: Bearer ${RUNNER_PAT}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/${api}/actions/runners/${1}" | jq -r .token
}

cleanup() { ./config.sh remove --token "$(gh_token remove-token)" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

./config.sh --url "${url}" --token "$(gh_token registration-token)" \
  --name "${RUNNER_NAME}" --labels "${RUNNER_LABELS}" \
  --work _work --unattended --replace --ephemeral

exec ./run.sh
