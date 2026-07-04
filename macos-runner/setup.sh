#!/usr/bin/env bash
# Bare-metal macOS self-hosted GitHub Actions runner on the mini — the counterpart to the
# containerized `github-runner/` (Linux). This one runs the **Apple-Silicon / MLX gates** the
# container can't: tommybot `eval`, `qwengen verify`, and the `doctor --deploy-check` post-deploy
# gate — they need Metal + MLX + the resident model cache, which only the macOS host provides.
#
# Runs directly on the host (no OrbStack), managed by the runner's own launchd service (`svc.sh`).
# Idempotent: re-run to re-register or bump the version. Registers repo-level (tommyroar is a user,
# not an org). The PAT mints a short-lived registration token; it is not stored in the runner config.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; source .env; set +a; }

: "${GH_OWNER:?set GH_OWNER in .env}"
: "${GH_REPO:?set GH_REPO in .env}"
: "${RUNNER_PAT:?set RUNNER_PAT in .env (fine-grained PAT · Administration:write on the repo)}"
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

echo "→ minting a registration token for ${GH_OWNER}/${GH_REPO}"
reg_token="$(curl -fsSL -X POST \
  -H "Authorization: Bearer ${RUNNER_PAT}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/actions/runners/registration-token" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"

echo "→ registering ${RUNNER_NAME} [${RUNNER_LABELS}]"
./config.sh --url "https://github.com/${GH_OWNER}/${GH_REPO}" --token "${reg_token}" \
  --name "${RUNNER_NAME}" --labels "${RUNNER_LABELS}" --work _work --unattended --replace

echo "→ installing + starting the launchd service"
./svc.sh install
./svc.sh start
./svc.sh status || true
echo "✓ runner up. Target it with: runs-on: [self-hosted, macos, arm64, metal, mini]"
