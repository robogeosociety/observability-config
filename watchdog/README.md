# stack-watchdog — external "the stack is down" alarm

Closes a self-monitoring blind spot: **every Grafana alert rule lives inside Grafana, a
container.** When OrbStack/Docker stops, Grafana stops with it, so *none* of the rules
(InfluxDB-availability, container-health, …) can fire — the stack goes dark and never
pages. On 2026-06-19→20 the stack was down ~36h (OrbStack stopped) and nothing alerted —
the InfluxDB write gap confirms 06-19T03:49Z → 06-20T16:09Z.

This watchdog runs on the **host** under launchd (not a container), every 2 min, and posts
**straight to Discord via curl** — so it works precisely when the rest doesn't.

## What it checks
1. The container engine is reachable (`docker ps`) — catches **OrbStack/Docker down**.
2. `influxdb` and `grafana` are `healthy`/`running` — catches a dead/missing container.

Down → one red Discord embed; re-pages every `REALERT_SECS` (6h) while still down; posts a
green recovery on the way back up. De-duped via `~/.local/share/stack-watchdog/state`.

## Why host + curl (the gotchas)
- **Host, not a container** — the whole point is to survive the container stack being down.
- **curl, not Grafana** — the alerter must be independent of the thing it watches.
- **Internal disk, no /Volumes at runtime** — launchd jobs hit TCC on `/Volumes`; the
  script, webhook, state, and log all live under `~`. `deploy.sh` stages the webhook from
  `grafana/.env` into `~/.local/share/stack-watchdog/.env` (chmod 600).
- **curl over python httpx** — launchd-spawned python hangs on its own TLS (same reason
  campsite-ingest uses curl); the Discord POST is curl.

## Operate
```sh
watchdog/deploy.sh                              # stage + (re)load the launchd agent
~/.local/share/stack-watchdog/stack-watchdog.sh --check        # print status, no alert
WATCHDOG_CONTAINERS=nonexistent ~/.local/share/stack-watchdog/stack-watchdog.sh   # force a down-page (test)
tail ~/Library/Logs/stack-watchdog.log
launchctl bootout gui/$(id -u)/com.tommydoerr.stack-watchdog   # disable
```

It runs on launchd `StartInterval` (120s), host-native — see the JOBS tier model
(continuous host daemons → launchd, not Nomad/containers).
