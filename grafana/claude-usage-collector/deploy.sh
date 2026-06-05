#!/bin/zsh
# Deploy the Claude Code usage collector: copy this repo's collector.py to the
# internal-disk runtime location and (re)load the launchd job.
#
# Why copy instead of run in place: macOS TCC blocks launchd-spawned processes
# from reading the external /Volumes disk (exit 78, "Operation not permitted"),
# so the running copy must live on the internal disk. This repo file is the
# source of truth; ~/.local/share/claude-usage-collector/collector.py is the
# deployed copy. (The collector itself only reads ~/.claude and writes to
# localhost:8086 — no /Volumes access at runtime.)
set -eu

SRC="${0:A:h}/collector.py"
DEST="$HOME/.local/share/claude-usage-collector/collector.py"
PLIST_SRC="${0:A:h}/com.tommy.claude-usage.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.tommy.claude-usage.plist"

mkdir -p "${DEST:h}"
cp "$SRC" "$DEST"
echo "deployed $SRC -> $DEST"

# Pull the write token from influxdb/.env (this script runs from the interactive
# shell, which CAN read /Volumes; the launchd job that follows cannot). The token
# goes into a chmod-600 .env next to the deployed collector — NOT into the plist —
# so it stays out of `launchctl print` env dumps and plist backups. collector.py
# loads this file itself (see load_env_file()).
ENV_FILE="${0:A:h:h:h}/influxdb/.env"
[ -f "$ENV_FILE" ] && source "$ENV_FILE"
: "${INFLUX_CLAUDE_USAGE_TOKEN:?set INFLUX_CLAUDE_USAGE_TOKEN in influxdb/.env first}"

DEPLOYED_ENV="${DEST:h}/.env"
umask 077
printf 'INFLUX_CLAUDE_USAGE_TOKEN=%s\n' "$INFLUX_CLAUDE_USAGE_TOKEN" > "$DEPLOYED_ENV"
chmod 600 "$DEPLOYED_ENV"
echo "wrote token -> $DEPLOYED_ENV (chmod 600)"

# Install/refresh the launchd job (idempotent). The plist is now secret-free, so
# it's copied verbatim.
mkdir -p "${PLIST_DEST:h}"
cp "$PLIST_SRC" "$PLIST_DEST"
chmod 600 "$PLIST_DEST"
launchctl bootout "gui/$(id -u)/com.tommy.claude-usage" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl kickstart -k "gui/$(id -u)/com.tommy.claude-usage"
echo "loaded + kicked com.tommy.claude-usage"
