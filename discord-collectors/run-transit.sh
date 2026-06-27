#!/bin/zsh
# Wrapper for the transit Discord collector (Nomad job discord-transit).
#
# transit_discord.py reads OBA_API_KEY from env (defaults to the dud "TEST").
# Rather than duplicate the secret, pull the real key straight from the existing
# transit_tracker app config — single source of truth, nothing new committed.
# The Discord webhook is read by the script itself from env -> grafana/.env.
# Passes "$@" through so `run-transit.sh --dry` works for testing.
set -eu
HERE="${0:A:h}"
SVC="/Volumes/dev/transit_tracker/.local/service.yaml"
OBA_API_KEY="$(grep -E '^[[:space:]]*oba_api_key:' "$SVC" | head -1 \
  | sed -E 's/^[[:space:]]*oba_api_key:[[:space:]]*//; s/[[:space:]]*(#.*)?$//; s/^["'\'']//; s/["'\'']$//')"
export OBA_API_KEY
[[ -n "$OBA_API_KEY" && "$OBA_API_KEY" != "TEST" ]] || { print -u2 "ERROR: no real oba_api_key in $SVC"; exit 1; }
exec /Users/tommydoerr/.local/bin/uv run --with httpx \
  "$HERE/transit_discord.py" "$@"
