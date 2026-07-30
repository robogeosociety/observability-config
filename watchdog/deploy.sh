#!/bin/zsh
# Deploy the stack-watchdog: copy the script + stage the Discord webhook to the internal
# disk, then (re)load the launchd agent. Run from the repo (reads /Volumes interactively,
# which is fine — only the launchd *runtime* must avoid /Volumes). Idempotent.
set -eu

REPO="${0:A:h}"                                  # watchdog/ in the repo
RT="$HOME/.local/share/stack-watchdog"
PLIST_SRC="$REPO/com.tommydoerr.stack-watchdog.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.tommydoerr.stack-watchdog.plist"
GRAFANA_ENV="/Volumes/dev/observability/grafana/.env"

mkdir -p "$RT"
install -m 0755 "$REPO/stack-watchdog.sh" "$RT/stack-watchdog.sh"
install -m 0755 "$REPO/capture-wedge.sh" "$RT/capture-wedge.sh"

# Stage the webhook (the ops channel) into an internal .env so the launchd runtime never
# reads /Volumes. chmod 600 — it's the secret.
if [ -r "$GRAFANA_ENV" ]; then
  hook=$(grep -m1 '^DISCORD_WEBHOOK_URL=' "$GRAFANA_ENV" | cut -d= -f2- | tr -d '"'\''')
  umask 177
  print -r -- "DISCORD_WEBHOOK_URL=$hook" > "$RT/.env"
  umask 022
  echo "staged webhook -> $RT/.env"
else
  echo "WARN: $GRAFANA_ENV unreadable — watchdog will run but can't page until $RT/.env has DISCORD_WEBHOOK_URL"
fi

install -m 0644 "$PLIST_SRC" "$PLIST_DST"

LABEL="com.tommydoerr.stack-watchdog"
DOMAIN="gui/$(id -u)"

# `bootstrap` into gui/UID fails with "5: Input/output error" when deploy runs over
# ssh rather than from the desktop session. The old sequence booted the agent OUT
# first and then failed to put it back — which is how this watchdog sat unloaded
# from 2026-07-21 through both /Volumes/dev wedges, paging for neither. So: try
# bootstrap, fall back to the legacy loader (which does work over ssh), and then
# *verify*, because a deploy that silently leaves the watchdog down is worse than
# no deploy at all.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_DST" 2>/dev/null \
  || launchctl load -w "$PLIST_DST" 2>/dev/null \
  || true
launchctl kickstart "$DOMAIN/$LABEL" 2>/dev/null || true

if launchctl list 2>/dev/null | grep -q "$LABEL"; then
  echo "loaded $LABEL (every 120s) — log: ~/Library/Logs/stack-watchdog.log"
else
  echo "ERROR: $LABEL is NOT loaded — the watchdog is dark." >&2
  echo "  Load it from the mini's desktop session, or: launchctl load -w $PLIST_DST" >&2
  exit 1
fi
