#!/bin/zsh
# runtime-versions/collect.sh — emit current-vs-latest versions for the three
# language runtimes to InfluxDB `ops` (measurement `toolchain_version`), feeding the
# "Runtime Versions" dashboard. Read-only checks; writes only the metric points.
#
#   node  → volta   (default node; latest in the active major from nodejs.org)
#   python→ uv      (uv-managed default; newest downloadable patch in the series)
#   rust  → rustup  (active stable toolchain; `rustup check` for the latest)
#
# Run by the Nomad periodic job `runtime-versions` (nomad/runtime-versions.hcl),
# daily. Unlike the older launchd collectors in this repo, it runs under the host
# Nomad agent (the batch-on-Nomad standard) and reads /Volumes directly via the
# agent's Full Disk Access — no internal-disk deploy needed.
set -uo pipefail
emulate -L zsh
cd "$HOME"   # keep Python -c startup CWD off /Volumes (dyld getcwd hang under daemons)

INFLUX_URL="${INFLUX_URL:-http://localhost:8086}"
INFLUX_ORG="${INFLUX_ORG:-home}"
OPS_BUCKET="${INFLUX_OPS_BUCKET:-ops}"
ENV_FILE="/Volumes/dev/observability/influxdb/.env"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
TOKEN="${INFLUX_OPS_TOKEN:-${INFLUX_ADMIN_TOKEN:-}}"
HOSTTAG="$(hostname -s 2>/dev/null || echo unknown)"
PY="$HOME/.local/bin/python3"; [[ -x "$PY" ]] || PY=python3
ts() { print -r -- "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }
# gt A B → true when B is strictly newer than A (semver-ish via sort -V)
gt() { [[ "$1" != "$2" && "$(print -l -- "$1" "$2" | sort -V | tail -1)" == "$2" ]] }

lines=()
add() {  # runtime manager current latest
  local rt=$1 mgr=$2 cur=$3 lat=$4 upd=0
  [[ -z "$cur" ]] && { ts "skip ${rt}: no current version"; return }
  [[ -z "$lat" ]] && lat=$cur
  # "latest" can never be older than what's installed — registries (e.g. uv's
  # python-build-standalone) sometimes lag the installed patch. Clamp to max:
  # if current is newer than the reported latest, latest := current.
  gt "$lat" "$cur" && lat=$cur
  gt "$cur" "$lat" && upd=1
  lines+=("toolchain_version,runtime=${rt},manager=${mgr},host=${HOSTTAG} current=\"${cur}\",latest=\"${lat}\",update_available=${upd}i")
  ts "${rt} (${mgr}): current=${cur} latest=${lat} update_available=${upd}"
}

# --- python / uv ---
if command -v uv >/dev/null 2>&1; then
  cur=$("$PY" -V 2>/dev/null | awk '{print $2}'); series=${cur%.*}
  lat=$(uv python list --all-versions 2>/dev/null \
    | grep -E "cpython-${series}\.[0-9]+-" \
    | grep -E "<download available>|/\.local/share/uv/" \
    | sed -E 's/^cpython-([0-9.]+)-.*/\1/' | sort -V | tail -1)
  add python uv "$cur" "${lat:-$cur}"
fi

# --- node / volta ---
if command -v volta >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
  cur=$(volta list node 2>/dev/null | sed -nE 's/.*node@([0-9][0-9.]*).*/\1/p' | head -1)
  major=${cur%%.*}
  if [[ -n "$major" ]]; then
    lat=$(curl -s --max-time 10 https://nodejs.org/dist/index.json 2>/dev/null \
      | "$PY" -c "import sys,json
d=json.load(sys.stdin)
v=[x['version'][1:] for x in d if x['version'].startswith('v${major}.')]
print(v[0] if v else '')" 2>/dev/null)
  fi
  add node volta "$cur" "${lat:-$cur}"
fi

# --- rust / rustup ---
if command -v rustup >/dev/null 2>&1; then
  rc=$(rustup check 2>/dev/null | grep -i "stable" | head -1)
  if print -r -- "$rc" | grep -qi "Update available"; then
    cur=$(print -r -- "$rc" | sed -nE 's/.*: ([0-9.]+) -> ([0-9.]+).*/\1/p')
    lat=$(print -r -- "$rc" | sed -nE 's/.*: ([0-9.]+) -> ([0-9.]+).*/\2/p')
  else
    cur=$(print -r -- "$rc" | sed -nE 's/.*: ([0-9.]+).*/\1/p'); lat=$cur
  fi
  [[ -z "$cur" ]] && { cur=$(rustc --version 2>/dev/null | awk '{print $2}'); lat=$cur; }
  add rust rustup "$cur" "${lat:-$cur}"
fi

if (( ${#lines} == 0 )); then ts "no runtimes detected — nothing to write"; exit 1; fi
lines+=("collector,source=runtime-versions,kind=batch,host=${HOSTTAG} success=1i,rows=${#lines}i,interval_s=86400i")

if [[ -z "$TOKEN" ]]; then ts "no Influx token (INFLUX_OPS_TOKEN) — cannot write"; exit 1; fi
body=$(print -l -- "${lines[@]}")
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -XPOST \
  "${INFLUX_URL}/api/v2/write?org=${INFLUX_ORG}&bucket=${OPS_BUCKET}&precision=s" \
  -H "Authorization: Token ${TOKEN}" -H "Content-Type: text/plain; charset=utf-8" \
  --data-binary "$body")
ts "influx write http=$code (${#lines} lines)"
[[ "$code" == 2* ]] || exit 1
