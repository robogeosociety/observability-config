#!/bin/zsh
# Wrapper for the #ops Live-Dashboards thread keepalive (Nomad job thread-keepalive).
# Sources the OpsBot bot token from the Discord channel's state .env (NOT a repo .env —
# the bot token lives in ~/.claude/channels/discord-ops/.env, len 72; ask-dash/.env's
# DISCORD_BOT_TOKEN is empty). Passes "$@" through so `--dry-run` works for testing.
set -eu
HERE="${0:A:h}"
BOT_ENV="$HOME/.claude/channels/discord-ops/.env"
[[ -f "$BOT_ENV" ]] && { set -a; source "$BOT_ENV"; set +a; }
export DISCORD_BOT_TOKEN="${DISCORD_BOT_TOKEN:?DISCORD_BOT_TOKEN missing in $BOT_ENV}"
export THREAD_ID="${THREAD_ID:-1521534944827150366}"   # #ops "Live Dashboards" thread
exec /usr/bin/python3 "$HERE/thread_keepalive.py" "$@"
