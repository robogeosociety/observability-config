#!/usr/bin/env bash
# Bare-metal macOS self-hosted GitHub Actions runner on the mini — the counterpart to the
# containerized `github-runner/` (Linux). This one runs the **Apple-Silicon / MLX gates** the
# container can't: tommybot `eval`, `qwengen verify`, and the `doctor --deploy-check` post-deploy
# gate — they need Metal + MLX + the resident model cache, which only the macOS host provides.
#
# Runs directly on the host (no OrbStack), managed by the runner's own launchd service (`svc.sh`).
# Idempotent: re-run to re-register or bump the version. The PAT mints a short-lived registration
# token; it is not stored in the runner config.
#
# Two scopes:
#   • ORG-level  (set GH_ORG)            — ONE runner serves every repo in the org. The clean
#                                          multi-repo path once tommyroar is an org.
#   • REPO-level (set GH_OWNER + GH_REPO) — serves a single repo (the only option on a user account).
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; source .env; set +a; }

: "${RUNNER_PAT:?set RUNNER_PAT in .env (fine-grained PAT · Administration: read+write)}"

if [ -n "${GH_ORG:-}" ]; then
  scope_url="https://github.com/${GH_ORG}"
  token_api="https://api.github.com/orgs/${GH_ORG}/actions/runners/registration-token"
  scope_desc="org ${GH_ORG} (all repos)"
else
  : "${GH_OWNER:?set GH_OWNER (repo mode) or GH_ORG (org mode) in .env}"
  : "${GH_REPO:?set GH_REPO (repo mode) or GH_ORG (org mode) in .env}"
  scope_url="https://github.com/${GH_OWNER}/${GH_REPO}"
  token_api="https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/actions/runners/registration-token"
  scope_desc="${GH_OWNER}/${GH_REPO}"
fi

RUNNER_VERSION="${RUNNER_VERSION:-2.321.0}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-mini}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,macos,arm64,metal,mini}"
RUNNER_NAME="${RUNNER_NAME:-mini-macos-$(hostname -s)}"

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"
if [ ! -x ./run.sh ]; then
  echo "→ downloading actions-runner v${RUNNER_VERSION} (osx-arm64)"
  tarball="actions-runner-osx-arm64-${RUNNER_VERSION}.tar.gz"
  curl -fsSLo "$tarball" \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${tarball}"
  tar xzf "$tarball" && rm -f "$tarball"
fi

echo "→ minting a registration token for ${scope_desc}"
reg_token="$(curl -fsSL -X POST \
  -H "Authorization: Bearer ${RUNNER_PAT}" \
  -H "Accept: application/vnd.github+json" \
  "$token_api" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"

echo "→ registering ${RUNNER_NAME} [${RUNNER_LABELS}] on ${scope_desc}"
./config.sh --url "$scope_url" --token "${reg_token}" \
  --name "${RUNNER_NAME}" --labels "${RUNNER_LABELS}" --work _work --unattended --replace

echo "→ installing + starting the launchd service"
./svc.sh install
./svc.sh start
./svc.sh status || true
echo "✓ runner up. Target it with: runs-on: [self-hosted, macos, arm64, metal, mini]"
