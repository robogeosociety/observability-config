#!/bin/zsh
# Deploy the dev-status collector: copy this repo's server.py to the internal-disk
# runtime location and restart the launchd job.
#
# Why copy instead of run in place: macOS TCC blocks launchd-spawned processes
# from reading the external /Volumes disk (exit 78, "Operation not permitted"),
# so the running copy must live on the internal disk. This repo file is the
# source of truth; ~/.local/share/dev-status/server.py is the deployed copy.
set -eu

SRC="${0:A:h}/server.py"
DEST="$HOME/.local/share/dev-status/server.py"

mkdir -p "${DEST:h}"
cp "$SRC" "$DEST"
echo "deployed $SRC -> $DEST"

launchctl kickstart -k "gui/$(id -u)/com.tommy.dev-status"
echo "restarted com.tommy.dev-status"
