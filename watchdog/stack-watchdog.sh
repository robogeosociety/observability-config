#!/bin/zsh
# External watchdog for the observability container stack.
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
CONTAINERS=(${=WATCHDOG_CONTAINERS:-influxdb grafana})   # overridable for a down-test

[ -r "$ENVF" ] && { set -a; . "$ENVF"; set +a; }
WEBHOOK="${DISCORD_WEBHOOK_URL:-}"

# ── run the checks → `reason` ("" = healthy) ──────────────────────────────────
reason=""
if ! docker ps >/dev/null 2>&1; then
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
  [ -n "$reason" ] && { print -r -- "DOWN: $reason"; exit 1; } || { print -r -- "OK — engine up, ${(j:, :)CONTAINERS} healthy"; exit 0; }
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
  if [ "$prev_status" = "down" ] && [ $((now - last_alert)) -lt "$REALERT_SECS" ]; then
    print -r -- "$(date '+%F %T') still down ($reason) — already paged, quiet"
  else
    post 15158332 "🚨 Observability stack DOWN" "$reason. Grafana's own alerts can't fire — it's a container in the same outage. Host: ${host}."
    last_alert=$now
    print -r -- "$(date '+%F %T') PAGED: $reason"
  fi
  print -r -- "down|$reason|$last_alert" > "$STATE"
else
  if [ "$prev_status" = "down" ]; then
    post 3066993 "✅ Observability stack recovered" "Engine up and ${(j:, :)CONTAINERS} healthy again (was: ${prev_reason})."
    print -r -- "$(date '+%F %T') RECOVERED (was: $prev_reason)"
  fi
  print -r -- "ok||$now" > "$STATE"
fi
