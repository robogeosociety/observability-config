# Telegraf — Mac host system metrics + stack endpoint health

Collects CPU, load, memory, swap, disk usage, disk I/O, network throughput, and
OrbStack container stats from `tommys-mac-mini` into the InfluxDB `system`
bucket, feeding the **Mac System** + **OrbStack Containers** dashboards. Also
runs synthetic HTTP probes of the stack's own services and reports Telegraf's
own health.

```
telegraf (brew service, native host)  ──15s──▶  InfluxDB `system` bucket  ──▶  dashboards
  inputs: cpu mem swap disk diskio net system docker  +  internal  http_response
```

Telegraf runs **natively via Homebrew**, not in a container — a container would
report the OrbStack Linux VM's metrics, not macOS.

## Telegraf is the standardized metrics agent

Telegraf is a single static Go binary (**zero runtime deps** — no JVM/Python;
`brew deps telegraf` is empty; only a few plugins need external tools, none of
which we use). It's the **default for any scrape/poll/endpoint metric** — host
stats, container stats, HTTP health checks, Prometheus endpoints — because it's
native to InfluxDB and a new source is a few lines of TOML across 243 built-in
input plugins.

**Custom Python collectors stay custom only when they're real ETL or need an API
Telegraf can't speak** — e.g. `campsites/ingest.py` (paginates R2, aggregates
~1.5M points), `anomaly-detector` (stateful z-scores), `walksheds-uptime` (pulls
GitHub Actions/Pages status via the GitHub API). Those bundle logic no agent
absorbs; folding them in would *fragment* one collector across two places, so we
don't. Rule of thumb: **scrape-shaped → Telegraf; pipeline-shaped → Python.**

## Files

| File | Role |
|------|------|
| `telegraf.conf` | canonical config (source of truth); `${INFLUX_TOKEN}` placeholder |
| `deploy.sh` | bake token from `.env` → `$(brew --prefix)/etc/telegraf.conf`, validate, restart service |
| `.env` | `INFLUX_TOKEN` (write-only, scoped to `system`); gitignored, chmod 600 |

Dashboard: `grafana/provisioning/dashboards/mac-system.json` · datasource
`system` in `grafana/provisioning/datasources/influxdb.yml`.

## Setup

1. **Install**: `brew install telegraf`
2. **Token**: a write-only token scoped to the `system` bucket already exists in
   `.env`. To re-mint:
   ```sh
   source ../influxdb/.env
   BID=$(docker exec influxdb influx bucket list --org home --token "$INFLUX_ADMIN_TOKEN" --hide-headers --name system | awk '{print $1}')
   docker exec influxdb influx auth create --org home --token "$INFLUX_ADMIN_TOKEN" \
     --write-bucket "$BID" --description "telegraf system metrics write"
   ```
3. **Deploy + start**: `./deploy.sh` (substitutes the token, validates with
   `telegraf --test`, runs `brew services restart telegraf`).

## Edit / redeploy

Edit `telegraf.conf` here, then `./deploy.sh`. Check status with
`brew services info telegraf`; logs via `brew services` (stderr to
`$(brew --prefix)/var/log/telegraf.log` if configured, else Console).

## Notes

- No `/Volumes` TCC issue: the deployed config lives on the internal disk, and
  the disk input reads filesystem **stats** (statfs), not file contents.
- `inputs.disk` skips macOS synthetic filesystems (`devfs`, `autofs`, …) so only
  real volumes show — including the 2TB external `/Volumes/dev`.
- CPU is overall-only (`percpu = false`); add `percpu = true` for per-core.
- `inputs.internal` (`collect_memstats = true`) reports Telegraf's own health —
  `internal_agent` / `_write` / `_gather` / `_memstats`. Watch `internal_write`
  `buffer_size` / `errors` to catch the InfluxDB output backing up.
- `inputs.http_response` probes Grafana (`:3001/api/health`), InfluxDB
  (`:8086/health`), and the dev-status collector (`:8077`) every 15s →
  `http_response` measurement (status_code, response_time). These are the stack's
  *own* endpoints (walksheds-uptime covers only the external public site). Data
  lands now; a small uptime panel + an `http_response` "endpoint down" alert are
  the obvious follow-ups. `x509_cert` for the tailnet HTTPS cert was considered
  but dropped — the tailnet TLS port isn't reachable from the host.
