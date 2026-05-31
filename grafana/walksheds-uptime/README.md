# walksheds-uptime

Host-side collector feeding the **Walksheds Uptime** Grafana dashboard
(`grafana/provisioning/dashboards/walksheds-uptime.json`, datasource `ops`).

GitHub-hosted CI can't reach the home InfluxDB (localhost/LAN/tailnet only), so
the live writes happen here on the host — same pattern as `dev-status`. Three
signals land in the `ops` bucket, tagged `service=walksheds`:

| measurement | source | fields |
|---|---|---|
| `uptime` | synthetic HTTP probe of `walksheds.xyz` | `up`, `latency_ms`, `status` |
| `ci_smoke` | latest **Live smoke** workflow run (`smoke.yml`) | `ok`, `duration_s`, `run_id` |
| `deploy` | github-pages `deployment_status` events | `ok`, `deploy_id` (tag `state`) |

The CI smoke + deploy signals come from the `walksheds` repo:
`.github/workflows/smoke.yml` runs `e2e/uptime.spec.js` against the live site on
a schedule and after each deploy; `ci_smoke` populates once that workflow is on
the default branch.

## Setup

1. `cp .env.example .env` and fill `INFLUX_TOKEN` (ops write token from
   `influxdb/.env` → `INFLUX_OPS_TOKEN`) and `GH_TOKEN` (`gh auth token`).
   `chmod 600 .env`.
2. `./deploy.sh` — copies `collector.py` + `.env` to
   `~/.local/share/walksheds-uptime/` (internal disk; macOS TCC blocks launchd
   from `/Volumes`) and (re)loads the launchd job
   `com.tommy.walksheds-uptime` (runs every 120 s).

Run `python3 collector.py --dry-run` to print line protocol without writing.

## Write path

Prefers the published InfluxDB HTTP port (`http://localhost:8086`, like the
backup/campsites collectors); if host→container port-forwarding is unavailable
it falls back to `docker exec influxdb influx write`. Logs:
`~/Library/Logs/com.tommy.walksheds-uptime.log`.
