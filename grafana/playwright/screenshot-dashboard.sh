#!/usr/bin/env zsh
# Full-page dashboard screenshot for a PR.
#
# Convention (required for any dashboard PR — see AGENTS.md): a PR that adds or
# changes a dashboard attaches a full-page screenshot of the affected dashboard(s)
# so the reviewer sees the rendered result, not just JSON. This renders the whole
# dashboard (kiosk = no nav chrome) via the grafana-image-renderer sidecar and
# writes it under pr-screenshots/.
#
# Usage:
#   ./screenshot-dashboard.sh <uid> [extra-query]
# e.g.
#   ./screenshot-dashboard.sh campsite-availability 'var-target_date=2026-07-04&var-agency=$__all&var-campsite=$__all'
#
# Then commit grafana/playwright/pr-screenshots/<uid>.png and embed it in the PR
# body via the branch raw URL — it renders inline for repo members on this private
# repo (verified). Pin variables to a selection that has data.
set -euo pipefail

uid=${1:?usage: screenshot-dashboard.sh <uid> [extra-query]}
extra=${2:-}
here=${0:A:h}
out_dir="$here/pr-screenshots"
out="$out_dir/$uid.png"

# Grafana admin creds + URL from the repo .env (chmod 600, gitignored).
source "$here/../.env"
base=${GRAFANA_URL:-http://localhost:3001}

mkdir -p "$out_dir"
url="$base/render/d/$uid/x?from=now-30d&to=now&kiosk=true&theme=dark&width=1400&height=1800"
[[ -n "$extra" ]] && url="$url&$extra"

code=$(curl -sf -o "$out" -w '%{http_code}' \
  -u "$GRAFANA_ADMIN_USER:$GRAFANA_ADMIN_PASSWORD" "$url") || {
  echo "render failed (is the 'renderer' container up?)" >&2; exit 1; }
echo "HTTP $code -> $out"
file "$out"
