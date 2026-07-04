#!/usr/bin/env bash
# Bare-metal macOS self-hosted GitHub Actions runner on the mini — the counterpart to the
# containerized `github-runner/` (Linux). This one is **CD-only**: it exists because a deploy
# has to touch the host — pull the serve clone, kickstart launchd services, and run the on-box
# `tommybot doctor --deploy-check` post-deploy gate. A Linux container can't reach the host's
# launchd; this runner can.
#
# NO model-bound CI runs here (no eval, no qwengen verify): local LLM generation on the mini
# was deprecated 2026-07-04 — the bot rebackends to the Claude API; eval gates Air-tier
# artifacts in its own lane. Hence no `metal` label.
#
# Service model: a **system LaunchDaemon** (/Library/LaunchDaemons), NOT the runner's stock
# `svc.sh` (which installs a gui-domain LaunchAgent). This box runs FileVault with no
# auto-login: gui agents die on GUI logout and don't return after a reboot until someone logs
# in at the console — the exact Nomad failure mode. The daemon survives logout and starts at
# boot, right after the FileVault pre-boot unlock. Install prompts for ONE interactive sudo.
#
# Idempotent: re-run to re-register or bump the version. The PAT mints a short-lived
# registration token; it is not stored in the runner config.
#
# Two scopes:
#   • ORG-level  (set GH_ORG)             — ONE runner serves every repo in the org
#                                           (robogeosociety — the standing plan).
#   • REPO-level (set GH_OWNER + GH_REPO) — serves a single repo.
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
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,macos,arm64,mini}"
RUNNER_NAME="${RUNNER_NAME:-mini-macos-$(hostname -s)}"
DAEMON_LABEL="${DAEMON_LABEL:-com.github.actions-runner.mini}"
RUN_AS_USER="$(id -un)"
plist_src="${RUNNER_DIR}/${DAEMON_LABEL}.plist"
plist_dst="/Library/LaunchDaemons/${DAEMON_LABEL}.plist"

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"
if [ ! -x ./run.sh ]; then
  echo "→ downloading actions-runner v${RUNNER_VERSION} (osx-arm64)"
  tarball="actions-runner-osx-arm64-${RUNNER_VERSION}.tar.gz"
  curl -fsSLo "$tarball" \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${tarball}"
  tar xzf "$tarball" && rm -f "$tarball"
fi

# Re-runs: config.sh refuses to re-register while the runner is running — stop the daemon first.
if [ -f "$plist_dst" ]; then
  echo "→ stopping the existing runner daemon for re-registration (sudo)"
  sudo launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true
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

# Same wrapper the stock service uses (reads .path, runs RunnerService.js, which survives
# runner self-updates) — only the launchd domain differs from svc.sh.
[ -f ./runsvc.sh ] || cp ./bin/runsvc.sh ./runsvc.sh
chmod +x ./runsvc.sh

# HOME/USER are set explicitly: launchd does not reliably populate them for UserName daemons,
# and the deploy jobs' git/ssh need them.
cat > "$plist_src" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${DAEMON_LABEL}</string>
  <key>UserName</key><string>${RUN_AS_USER}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${RUNNER_DIR}/runsvc.sh</string>
  </array>
  <key>WorkingDirectory</key><string>${RUNNER_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>${HOME}</string>
    <key>USER</key><string>${RUN_AS_USER}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${RUNNER_DIR}/daemon.out.log</string>
  <key>StandardErrorPath</key><string>${RUNNER_DIR}/daemon.err.log</string>
</dict>
</plist>
PLIST
plutil -lint "$plist_src" >/dev/null

echo "→ installing + starting the system LaunchDaemon (ONE interactive sudo)"
sudo /bin/sh -c "
  launchctl bootout 'system/${DAEMON_LABEL}' 2>/dev/null || true
  install -o root -g wheel -m 644 '${plist_src}' '${plist_dst}'
  launchctl bootstrap system '${plist_dst}'
  launchctl print 'system/${DAEMON_LABEL}' | grep -E '(state|pid) =' || true
"
echo "✓ runner up (system daemon ${DAEMON_LABEL}, survives logout)."
echo "  Target it with: runs-on: [self-hosted, macos, arm64, mini]"
