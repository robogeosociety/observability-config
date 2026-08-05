#!/bin/zsh
# External watchdog: the mini's independent eye on the fleet-bus, the container
# engine, and the /Volumes/dev wedge.
#
# THE GAP IT CLOSES (2026-08 edition): fleet liveness now lives in the fleet-bus
# Worker on Cloudflare (observability-config#174) — the supervisor publishes
# fleet.supervisor.tick and the bus's own DO alarm pages when it goes silent. But
# that alarm runs *in Cloudflare*: it cannot fire when Cloudflare itself, the auth
# token, or the mini→edge path is the broken piece. So this check runs on the HOST
# under launchd, probes the bus's retained tick from the outside, and posts straight
# to Discord via curl (no python-TLS — launchd-spawned python hangs on its own TLS;
# curl is the reliable path). Same independence argument as the original
# Grafana-era version, one layer up.
#
# RETIRED (tombstone): the original premise — "every Grafana alert rule lives inside
# Grafana, a container, so an OrbStack outage silences them all" — is moot since the
# TIG stack (Grafana + InfluxDB + telegraf) was decommissioned (observability-config
# #156). The default influxdb/grafana container checks went first (see CONTAINERS
# below); the framing followed. The engine probe stays: OrbStack still hosts live
# non-observability containers (the discobot fleet, transit-tracker).
#
# Runs every ~2 min. Idempotent + de-duped: pages once on the up→down transition, then
# re-pages every REALERT_SECS while still down, and posts a green recovery on down→up.
# Everything (script, webhook, state, log) is on the internal disk, so it never touches
# /Volumes (launchd TCC) at runtime. Deploy with watchdog/deploy.sh.
set -u

RT="$HOME/.local/share/stack-watchdog"
ENVF="$RT/.env"          # DISCORD_WEBHOOK_URL, staged from grafana/.env by deploy.sh
STATE="$RT/state"        # "ok||<ts>" or "down|<reason>|<last_alert_ts>"
REALERT_SECS="${REALERT_SECS:-21600}"   # re-page every 6h while down
# influxdb + grafana were retired (observability-config#156), so watching them by
# default paged for containers that no longer exist. Opt in explicitly:
# WATCHDOG_CONTAINERS="a b" (also the down-test hook).
CONTAINERS=(${=WATCHDOG_CONTAINERS:-})

# ── fleet-bus tick probe config ───────────────────────────────────────────────
# The supervisor publishes fleet.supervisor.tick every 300s; the bus retains it
# with a 180s TTL (workers/fleet-bus/src/contract.js). We read it back over HTTP
# with the same bearer the publisher uses — the env file already lives on the
# internal disk (~/.config, not /Volumes), so nothing to stage at deploy time.
BUS_ENVF="${WATCHDOG_BUS_ENV:-$HOME/.config/fleet-bus/env}"   # BUS_HTTP_URL + BUS_HTTP_TOKEN
TICK_TOPIC="fleet.supervisor.tick"
TICK_MAX_AGE="${WATCHDOG_TICK_MAX_AGE:-600}"

# ── /Volumes/dev wedge probe ──────────────────────────────────────────────────
# OrbStack shares the whole macOS root into its Linux VM over virtiofs, and that
# sharing layer can wedge the volume: readdir starts returning EINTR while SMART,
# df and the kernel log all stay clean (robogeosociety/infra#25). A second, nearly
# identical-looking failure lives in the USB4/TB controller under the NVMe itself
# (robogeosociety/tommybot#101). Both are invisible until a human runs git — which
# is what this probe fixes.
#
# The deadline is not optional. A wedged readdir blocks in the kernel for minutes,
# so an unbounded check would hang this watchdog under launchd — producing exactly
# the silence it exists to prevent. macOS ships no timeout(1), hence background +
# poll + kill.
DISK_PATH="${WATCHDOG_DISK_PATH:-/Volumes/dev}"
DISK_DEADLINE="${WATCHDOG_DISK_DEADLINE:-15}"

probe_readdir() {   # $1=dir  $2=deadline_secs  → 0 responded, 1 timed out
  local dir="$1" deadline="$2" waited=0 pid
  ls -1 "$dir" >/dev/null 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$deadline" ]; then
      kill -9 "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  # A non-zero exit still means the kernel answered — only blocking is a wedge.
  wait "$pid" 2>/dev/null
  return 0
}

[ -r "$ENVF" ] && { set -a; . "$ENVF"; set +a; }
WEBHOOK="${DISCORD_WEBHOOK_URL:-}"

# ── fleet-bus tick probe ──────────────────────────────────────────────────────
# Three outcomes, deliberately distinct because they implicate different layers:
#   healthy      — 200 with ageSec <= TICK_MAX_AGE. Silent.
#   "bus tick…"  — 200-but-stale or 404 (TTL lapsed): the supervisor OR its publish
#                  path is down. The bus's own DO alarm catches this too; we check
#                  it independently so one bug can't silence both.
#   "bus unreachable…" — curl failure / 5xx / 401: Cloudflare or auth trouble seen
#                  FROM THE MINI. The bus's own alarm cannot fire for this case —
#                  catching it is the reason this on-box watchdog still exists.
# Missing/incomplete config never pages (a false page from an absent key would
# train the human to ignore the red ones) — it logs the skip and the other checks
# still run.  Reason strings carry no double-quotes (see post()).
bus_skip=""
probe_bus() {   # → sets `bus_reason` ("" = healthy) / `bus_skip` (non-empty = not configured)
  bus_reason=""
  local url="" token="" out code body age
  if [ -r "$BUS_ENVF" ]; then
    url=$(grep -m1 '^BUS_HTTP_URL=' "$BUS_ENVF" | cut -d= -f2- | tr -d '"'\''')
    token=$(grep -m1 '^BUS_HTTP_TOKEN=' "$BUS_ENVF" | cut -d= -f2- | tr -d '"'\''')
  fi
  if [ -z "$url" ] || [ -z "$token" ]; then
    bus_skip="fleet-bus check skipped — $BUS_ENVF absent or missing BUS_HTTP_URL/BUS_HTTP_TOKEN"
    return 0
  fi
  out=$(curl -sS -m 10 -w $'\n%{http_code}' -H "Authorization: Bearer $token" \
        "${url%/}/retained/${TICK_TOPIC}" 2>/dev/null)
  code="${out##*$'\n'}"
  body="${out%$'\n'*}"
  case "$code" in
    200)
      age=$(print -r -- "$body" | sed -n 's/.*"ageSec":\([0-9][0-9]*\).*/\1/p')
      if [ -n "$age" ] && [ "$age" -le "$TICK_MAX_AGE" ]; then
        bus_age="$age"        # for --check
      elif [ -n "$age" ]; then
        bus_reason="bus tick stale — retained ${TICK_TOPIC} is ${age}s old (limit ${TICK_MAX_AGE}s)"
      else
        bus_reason="bus unreachable — 200 from the bus but no ageSec in the body (contract drift?)"
      fi ;;
    404) bus_reason="bus tick absent — no live retained ${TICK_TOPIC} (TTL lapsed)" ;;
    ""|000) bus_reason="bus unreachable — no HTTP response from the fleet-bus within 10s" ;;
    401|403) bus_reason="bus unreachable — fleet-bus rejected our token (HTTP $code)" ;;
    *) bus_reason="bus unreachable — fleet-bus answered HTTP $code" ;;
  esac
}

# Cheapest-test-first recovery. The two wedge layers look identical from userspace
# (readdir EINTR, stat fine, SMART clean), so ordering IS the diagnostic: the
# OrbStack case clears in seconds, and only its failure to clear implicates the
# device. Running the 20-minute device ceremony first would cost a reboot for a
# problem an 'orb' restart fixes.
remedy_for() {
  case "$1" in
    readdir*) print -r -- "Run 'orb stop && orb start' first — it clears the OrbStack virtiofs case in seconds (infra#25). If readdir still blocks after that, the fault is below APFS in the enclosure (tommybot#101): force-unmount, detach the enclosure, reboot the mini, reattach. Do not start with the reboot." ;;
    "bus tick"*) print -r -- "The supervisor or its publish path is down — the same silence the bus's own alarm pages for, verified here independently. On the mini: curl 127.0.0.1:8787/health, then launchctl kickstart gui/\$UID/com.tommyroar.obsidian-supervisor and tail its log." ;;
    "bus unreachable"*) print -r -- "Cloudflare, DNS, or the bearer token is the broken piece — the one failure the bus's own alarm can never report. Check ~/.config/fleet-bus/env, curl the bus /stat by hand, and check the Cloudflare status page before touching the supervisor." ;;
    # Tombstone: the old default remedy — 'Grafana's own alerts cannot fire — it is a
    # container in the same outage.' — died with the TIG stack (observability-config#156).
    *)        print -r -- "OrbStack hosts the live container fleet (discobots, transit-tracker) — run 'orb start', then 'docker ps' on the mini." ;;
  esac
}

title_for() {
  case "$1" in
    readdir*) print -r -- "🚨 ${DISK_PATH} is wedged" ;;
    "bus tick"*) print -r -- "🔴 supervisor tick silent on the bus" ;;
    "bus unreachable"*) print -r -- "⚠️ fleet-bus unreachable from the mini" ;;
    # Tombstone: '🚨 Observability stack DOWN' — the stack it named was the TIG stack,
    # retired in observability-config#156; the engine check itself lives on.
    *)        print -r -- "🚨 Container engine DOWN on the mini" ;;
  esac
}

color_for() {  # red for down, amber for the reachability-only case
  case "$1" in
    "bus unreachable"*) print -r -- 15105570 ;;
    *)                  print -r -- 15158332 ;;
  esac
}

# ── run the checks → `reason` ("" = healthy) ──────────────────────────────────
# First reason wins, cheapest/most-fundamental first: a wedged disk or dead engine
# explains everything downstream of it; the bus probe runs last (it costs a WAN
# round-trip and its failure implicates a different layer entirely).
reason=""; bus_age=""
if ! probe_readdir "$DISK_PATH" "$DISK_DEADLINE"; then
  reason="readdir on $DISK_PATH blocked >${DISK_DEADLINE}s — the volume is wedged"
elif ! docker ps >/dev/null 2>&1; then
  reason="container engine unreachable — OrbStack/Docker is down"
else
  for c in $CONTAINERS; do
    st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null)
    if [ -z "$st" ]; then reason="container '$c' is missing (not created)"; break
    elif [ "$st" != "healthy" ] && [ "$st" != "running" ]; then reason="container '$c' is '$st'"; break
    fi
  done
fi
if [ -z "$reason" ]; then
  probe_bus
  [ -n "$bus_skip" ] && print -r -- "$(date '+%F %T') $bus_skip"
  reason="${bus_reason:-}"
fi

# `--check` — print status and exit, no alert (for tests / manual runs).
if [ "${1:-}" = "--check" ]; then
  if [ -n "$reason" ]; then
    print -r -- "DOWN: $reason"; print -r -- "  -> $(remedy_for "$reason")"; exit 1
  fi
  bus_note="bus tick ${bus_age:-?}s old"
  [ -n "$bus_skip" ] && bus_note="bus check skipped (no config)"
  print -r -- "OK — ${DISK_PATH} readable, engine up${CONTAINERS:+, ${(j:, :)CONTAINERS} healthy}, ${bus_note}"
  exit 0
fi

now=$(date +%s)
host=$(hostname -s)
prev_status="ok"; prev_reason=""; last_alert=0
[ -r "$STATE" ] && IFS='|' read -r prev_status prev_reason last_alert < "$STATE"

post() {  # $1=color(int)  $2=title  $3=description   (reason strings carry no double-quotes)
  [ -n "$WEBHOOK" ] || { print -r -- "$(date '+%F %T') no webhook configured"; return 0; }
  curl -sS -m 10 -H 'Content-Type: application/json' "$WEBHOOK" \
    --data "{\"embeds\":[{\"title\":\"$2\",\"description\":\"$3\",\"color\":$1,\"footer\":{\"text\":\"stack-watchdog · ${host}\"}}]}" >/dev/null 2>&1
}

if [ -n "$reason" ]; then
  # Capture evidence on the healthy->wedged edge only, and only for a disk wedge.
  # Detached, because the collector deliberately waits on calls that block —
  # letting it run inline would stall the 120 s cycle it is reporting from.
  #
  # The edge is the only moment worth capturing: `orb stop && orb start` is the
  # documented fix AND it rotates away OrbStack's log, so once a human responds
  # the evidence is gone. Every prior incident was diagnosed without it.
  if [ "$prev_status" != "down" ]; then
    case "$reason" in
      readdir*)
        if [ -x "$RT/capture-wedge.sh" ]; then
          # nohup, NOT setsid — setsid is Linux-only and absent on macOS. The
          # first version of this used it behind a `||` fallback that could never
          # fire (backgrounding returns 0 immediately), so it logged "started"
          # and ran nothing. An evidence collector that reports success while
          # collecting nothing is worse than one that is obviously missing, so
          # the claim below is now checked rather than assumed.
          nohup "$RT/capture-wedge.sh" "$reason" >>"$RT/capture.log" 2>&1 &
          cap_pid=$!
          disown 2>/dev/null
          sleep 1
          if kill -0 "$cap_pid" 2>/dev/null; then
            print -r -- "$(date '+%F %T') capture-wedge.sh running (pid $cap_pid)"
          else
            print -r -- "$(date '+%F %T') WARNING: capture-wedge.sh exited immediately — see $RT/capture.log"
          fi
        else
          print -r -- "$(date '+%F %T') WARNING: $RT/capture-wedge.sh missing — wedge going unrecorded"
        fi
        ;;
    esac
  fi

  if [ "$prev_status" = "down" ] && [ $((now - last_alert)) -lt "$REALERT_SECS" ]; then
    print -r -- "$(date '+%F %T') still down ($reason) — already paged, quiet"
  else
    post "$(color_for "$reason")" "$(title_for "$reason")" "$reason. $(remedy_for "$reason") Host: ${host}."
    last_alert=$now
    print -r -- "$(date '+%F %T') PAGED: $reason"
  fi
  print -r -- "down|$reason|$last_alert" > "$STATE"
else
  if [ "$prev_status" = "down" ]; then
    bus_state="bus tick fresh"
    [ -n "$bus_skip" ] && bus_state="bus check unconfigured"
    post 3066993 "✅ Recovered" "All watchdog checks green — ${DISK_PATH} readable, engine up${CONTAINERS:+, ${(j:, :)CONTAINERS} healthy}, ${bus_state} (was: ${prev_reason})."
    print -r -- "$(date '+%F %T') RECOVERED (was: $prev_reason)"
  fi
  print -r -- "ok||$now" > "$STATE"
fi
