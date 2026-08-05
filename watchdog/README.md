# stack-watchdog — the mini's independent "is anyone watching?" alarm

Closes a self-monitoring blind spot — the *current* one. Fleet liveness lives in the
**fleet-bus Worker on Cloudflare** (#174): the supervisor publishes
`fleet.supervisor.tick`, and the bus's own DO alarm pages when the tick goes silent.
But that alarm runs **in Cloudflare** — it cannot fire when Cloudflare itself, the
bearer token, or the mini→edge path is what broke. This watchdog is the other half
of the pair: it runs on the **host** under launchd, every 2 min, probes the bus's
retained tick **from the mini**, and posts straight to Discord via curl — so it works
precisely when the edge-side alarm can't.

> **Tombstone (2026-08):** the original premise — "every Grafana alert rule lives
> inside Grafana, a container, so an OrbStack outage silences them all" (the
> 2026-06-19→20 ~36h dark-stack incident) — retired with the TIG stack (#156).
> The default `influxdb`/`grafana` container checks were dropped then; the framing
> and remedies followed in the bus repoint. The disk-wedge and engine probes were
> never Grafana-specific and live on unchanged.

## What it checks

1. **`/Volumes/dev` readdir probe** (deadline-bounded) — catches the OrbStack-virtiofs
   wedge (infra#25) and the enclosure fault below it (tommybot#101).
2. **Container engine reachable** (`docker ps`) — OrbStack still hosts the live
   non-observability fleet (discobots, transit-tracker).
3. **Opt-in container health** — `WATCHDOG_CONTAINERS="a b"` (also the down-test hook).
4. **Fleet-bus supervisor tick** — `GET {BUS_HTTP_URL}/retained/fleet.supervisor.tick`
   with the bearer from `~/.config/fleet-bus/env`. Three outcomes:

   | Outcome | Signal | Meaning |
   | --- | --- | --- |
   | 200, `ageSec` ≤ 600 | healthy, silent | supervisor publishing, bus retaining, path clear |
   | 200-but-stale or 404 | 🔴 `supervisor tick silent on the bus` | supervisor or its publish path down — the bus's own alarm sees this too; checked independently so one bug can't silence both |
   | curl fail / 5xx / 401 | ⚠️ `fleet-bus unreachable from the mini` (amber) | Cloudflare/DNS/auth trouble — the case **only** this on-box watchdog can catch |

   Missing/incomplete config **never pages** — it logs the skip and the other checks
   still run (a false page from an absent key trains the human to ignore red ones).

Down → one Discord embed; re-pages every `REALERT_SECS` (6h) while still down; posts a
green recovery on the way back up. De-duped via `~/.local/share/stack-watchdog/state`.

## Why host + curl (the gotchas)

- **Host, not the edge** — the alerter must be independent of the thing it watches;
  the bus's DO alarm and this probe fail for disjoint reasons.
- **Internal disk, no /Volumes at runtime** — launchd jobs hit TCC on `/Volumes`; the
  script, webhook, state, and log live under `~`. `deploy.sh` stages the webhook into
  `~/.local/share/stack-watchdog/.env` (chmod 600). The bus credentials are read in
  place from `~/.config/fleet-bus/env` — already internal, nothing to stage.
- **curl over python httpx** — launchd-spawned python hangs on its own TLS (same reason
  campsite-ingest uses curl); both the probe and the Discord POST are curl.

## Operate

```sh
watchdog/deploy.sh                              # stage + (re)load the launchd agent
~/.local/share/stack-watchdog/stack-watchdog.sh --check        # print status, no alert
WATCHDOG_CONTAINERS=nonexistent ~/.local/share/stack-watchdog/stack-watchdog.sh   # force a down-page (test)
WATCHDOG_TICK_MAX_AGE=1 ~/.local/share/stack-watchdog/stack-watchdog.sh --check   # force the tick-stale path
tail ~/Library/Logs/stack-watchdog.log
launchctl bootout gui/$(id -u)/com.tommydoerr.stack-watchdog   # disable
```

It runs on launchd `StartInterval` (120s), host-native — see the JOBS tier model
(continuous host daemons → launchd, not Nomad/containers). Deploying over ssh:
`deploy.sh` falls back to `launchctl load -w` and **verifies** the agent is loaded
(#172; the fleet-wide audit of that failure pattern is rgs#181).
