#!/usr/bin/env zsh
# Deploy the walksheds-uptime collector to the internal disk and (re)load its
# launchd job. It runs from the internal disk because macOS TCC blocks
# launchd-spawned processes from reading the external /Volumes disk (same reason
# as dev-status). This repo dir is the source of truth; the deployed copy lives
# at ~/.local/share/walksheds-uptime/.
set -eu

SRC="${0:A:h}"
DEST="$HOME/.local/share/walksheds-uptime"
PLIST="com.tommy.walksheds-uptime.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$DEST"
cp "$SRC/collector.py" "$DEST/collector.py"
cp "$SRC/.env" "$DEST/.env"
chmod 600 "$DEST/.env"
cp "$SRC/$PLIST" "$HOME/Library/LaunchAgents/$PLIST"
echo "deployed $SRC -> $DEST"

launchctl bootout "$DOMAIN/com.tommy.walksheds-uptime" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$PLIST"
launchctl kickstart -k "$DOMAIN/com.tommy.walksheds-uptime"
echo "loaded + kicked com.tommy.walksheds-uptime (StartInterval 120s)"
