#!/bin/zsh
# External watchdog for the observability container stack + the /Volumes/dev wedge.
#
# THE GAP IT CLOSES: every Grafana alert rule lives inside Grafana — a *container*.
# When OrbStack/Docker stops, Grafana stops too, so none of its rules can fire
# (InfluxDB-availability, container-health, all of it goes silent). The stack can be
# dark for days and never page. So this check runs on the HOST under launchd, OUTSIDE
# the containers, and posts straight to Discord via curl (no Grafana, no python-TLS —
# launchd-spawned python hangs on its own TLS; curl is the reliable path).
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
# influxdb + grafana were retired, so watching them by default paged for containers
# that no longer exist. Opt in explicitly: WATCHDOG_CONTAINERS="a b" (also the down-test hook).
CONTAINERS=(${=WATCHDOG_CONTAINERS:-})

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

# Cheapest-test-first recovery. The two wedge layers look identical from userspace
# (readdir EINTR, stat fine, SMART clean), so ordering IS the diagnostic: the
# OrbStack case clears in seconds, and only its failure to clear implicates the
# device. Running the 20-minute device ceremony first would cost a reboot for a
# problem an 'orb' restart fixes.
remedy_for() {
  case "$1" in
    readdir*) print -r -- "Run 'orb stop && orb start' first — it clears the OrbStack virtiofs case in seconds (infra#25). If readdir still blocks after that, the fault is below APFS in the enclosure (tommybot#101): force-unmount, detach the enclosure, reboot the mini, reattach. Do not start with the reboot." ;;
    *)        print -r -- "Grafana's own alerts cannot fire — it is a container in the same outage." ;;
  esac
}

title_for() {
  case "$1" in
    readdir*) print -r -- "🚨 ${DISK_PATH} is wedged" ;;
    *)        print -r -- "🚨 Observability stack DOWN" ;;
  esac
}

# ── run the checks → `reason` ("" = healthy) ──────────────────────────────────
reason=""
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

# `--check` — print status and exit, no alert (for tests / manual runs).
if [ "${1:-}" = "--check" ]; then
  [ -n "$reason" ] && { print -r -- "DOWN: $reason"; print -r -- "  -> $(remedy_for "$reason")"; exit 1; } \
                   || { print -r -- "OK — ${DISK_PATH} readable, engine up${CONTAINERS:+, ${(j:, :)CONTAINERS} healthy}"; exit 0; }
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
    post 15158332 "$(title_for "$reason")" "$reason. $(remedy_for "$reason") Host: ${host}."
    last_alert=$now
    print -r -- "$(date '+%F %T') PAGED: $reason"
  fi
  print -r -- "down|$reason|$last_alert" > "$STATE"
else
  if [ "$prev_status" = "down" ]; then
    post 3066993 "✅ Recovered" "${DISK_PATH} readable and engine up again${CONTAINERS:+, ${(j:, :)CONTAINERS} healthy} (was: ${prev_reason})."
    print -r -- "$(date '+%F %T') RECOVERED (was: $prev_reason)"
  fi
  print -r -- "ok||$now" > "$STATE"
fi
