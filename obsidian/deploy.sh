#!/bin/zsh
# Deploy the Obsidian-vault backup from this repo (source of truth) to the
# internal disk, where launchd runs it. Mirrors the dev-status deploy pattern:
# edit backup.sh / the plist HERE, then run this to sync + reload the agent.
#
# The job needs Full Disk Access on /bin/zsh (iCloud vault + influxdb/.env).
set -euo pipefail
HERE="${0:A:h}"
PLIST="com.tommydoerr.obsidian-tbd-backup.plist"

mkdir -p "$HOME/git-repos"
install -m 755 "$HERE/backup.sh" "$HOME/git-repos/obsidian-tbd-backup.sh"
install -m 644 "$HERE/$PLIST" "$HOME/Library/LaunchAgents/$PLIST"

launchctl unload "$HOME/Library/LaunchAgents/$PLIST" 2>/dev/null || true
launchctl load -w "$HOME/Library/LaunchAgents/$PLIST"
echo "deployed obsidian backup → ~/git-repos/obsidian-tbd-backup.sh; (re)loaded $PLIST"
