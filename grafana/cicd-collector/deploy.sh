#!/bin/zsh
# Deploy the GitHub Actions CI/CD collector: copy this repo's collector.py (and
# its .env) to the internal-disk runtime location and (re)load the launchd job.
#
# Why copy instead of run in place: macOS TCC blocks launchd-spawned processes
# from reading the external /Volumes disk (exit 78, "Operation not permitted"),
# so the running copy must live on the internal disk. This repo dir is the
# source of truth; ~/.local/share/cicd-collector/ is the deployed copy. (At
# runtime the collector only talks to api.github.com and localhost:8086 — no
# /Volumes access.)
set -eu

SRC_DIR="${0:A:h}"
DEST_DIR="$HOME/.local/share/cicd-collector"
PLIST_SRC="$SRC_DIR/com.tommy.cicd-collector.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.tommy.cicd-collector.plist"

[ -f "$SRC_DIR/.env" ] || { echo "error: $SRC_DIR/.env missing — cp .env.example .env and fill it"; exit 1; }

mkdir -p "$DEST_DIR"
cp "$SRC_DIR/collector.py" "$DEST_DIR/collector.py"
echo "deployed collector.py -> $DEST_DIR"

# Secrets travel via a chmod-600 .env next to the deployed collector — NOT the
# plist — so they stay out of `launchctl print` env dumps and plist backups.
umask 077
cp "$SRC_DIR/.env" "$DEST_DIR/.env"
chmod 600 "$DEST_DIR/.env"
echo "wrote .env -> $DEST_DIR/.env (chmod 600)"

# Install/refresh the launchd job (idempotent). The plist is secret-free.
mkdir -p "${PLIST_DEST:h}"
cp "$PLIST_SRC" "$PLIST_DEST"
chmod 600 "$PLIST_DEST"
launchctl bootout "gui/$(id -u)/com.tommy.cicd-collector" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl kickstart -k "gui/$(id -u)/com.tommy.cicd-collector"
echo "loaded + kicked com.tommy.cicd-collector"
