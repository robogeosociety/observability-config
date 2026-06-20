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
launchctl bootout "gui/$(id -u)/com.tommydoerr.stack-watchdog" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl kickstart "gui/$(id -u)/com.tommydoerr.stack-watchdog"
echo "loaded com.tommydoerr.stack-watchdog (every 120s) — log: ~/Library/Logs/stack-watchdog.log"
