# Telegraf — Mac host system metrics + stack endpoint health

Collects CPU, load, memory, swap, disk usage, disk I/O, network throughput, and
OrbStack container stats from `tommys-mac-mini` into the InfluxDB `system`
bucket, feeding the **Mac System** + **OrbStack Containers** dashboards. Also
runs synthetic HTTP probes of the stack's own services and reports Telegraf's
own health.

```
telegraf (brew service, native host)  ──15s──▶  InfluxDB `system` bucket  ──▶  dashboards
  inputs: cpu mem swap disk diskio net system docker  +  internal  http_response  prometheus
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
~1.5M points) and `anomaly-detector` (stateful z-scores). Those bundle logic no
agent absorbs; folding them in would *fragment* one collector across two places,
so we don't. Rule of thumb: **scrape-shaped → Telegraf; pipeline-shaped → Python.**

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

- **Missing `telegraf.d` crash-loops the service.** Homebrew's plist runs
  telegraf with `-config-directory $(brew --prefix)/etc/telegraf.d`; since
  v1.38 a *missing* dir is fatal, so the service crash-loops, writes nothing,
  and the InfluxDB-availability alert fires even though InfluxDB is healthy
  (bit us 2026-06-23). We use no drop-ins, but the dir must exist — `deploy.sh`
  now `mkdir -p`s it. Symptom in `var/log/telegraf.log`:
  `E! reading config directory failed: ... telegraf.d: no such file or directory`.
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
  *own* loopback endpoints; public-site uptime is not measured here (walksheds.xyz
  is probed from GitHub Actions — `robogeosociety/walksheds` `uptime.yml`). Data
  lands now; a small uptime panel + an `http_response` "endpoint down" alert are
  the obvious follow-ups. `x509_cert` for the tailnet HTTPS cert was considered
  but dropped — the tailnet TLS port isn't reachable from the host.
- `inputs.prometheus` scrapes InfluxDB's own `/metrics` (`:8086`) at 60s for
  engine/operational internals — write throughput/errors (`storage_writer_*`),
  query-control state (`qc_*`), HTTP request/query/write counts, task scheduler,
  instance inventory (`influxdb_*_total`), uptime, boltdb. This is the **hybrid**
  "migrate to InfluxDB Prometheus": Telegraf owns the internals; `bucket-stats`
  still owns named per-bucket storage + throughput (which `/metrics` can't label
  by name or derive). `/metrics` has ~2,600 series; `namedrop` cuts the
  cardinality bombs (`*_duration_seconds` histograms, per-shard `storage_*`,
  `go_*`/`promhttp_*`/`process_*`) down to **~50 bounded series**. **Do NOT set
  `metric_version = 2`** — in 1.38 it flips to a single `prometheus` measurement
  and defeats the measurement-name `namedrop`. A future "InfluxDB Internals"
  dashboard reads these from the `system` bucket.
