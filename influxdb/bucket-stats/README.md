# influx bucket-stats collector

Records **per-bucket storage, cardinality, and write throughput** over time so the
**InfluxDB Buckets** Grafana dashboard can graph them. InfluxDB exposes these live
at `/metrics` (Prometheus text, keyed by *hex bucket id*) but never persists them;
this collector samples that endpoint every 5 minutes, resolves ids → names, and
writes one `bucket_stats` point per bucket to the `ops` bucket.

## What it writes

Measurement `bucket_stats`, tag `bucket=<name>` (internal `_*` buckets skipped):

| field | type | source |
|---|---|---|
| `disk_bytes` | int | `storage_tsm_files_disk_bytes` + `storage_cache_disk_bytes` (compacted TSM + WAL/cache) |
| `series` | int | `storage_bucket_series_num` (series cardinality) |
| `measurements` | int | `storage_bucket_measurement_num` |
| `points_per_sec` | float | points written in the trailing 5-min window ÷ window |

**Why throughput is derived:** `/metrics` only exposes write counts globally
(`storage_writer_ok_points`), not per bucket — so the collector counts each
bucket's points over the trailing window via Flux and divides. `series` is
*cardinality*, not row count; the dashboard labels it accordingly.

## Run / deploy

- **Locally:** `INFLUX_TOKEN=… python3 collect.py` (reads `.env` if present via
  `collect.sh`).
- **Scheduled:** LaunchAgent `com.tommydoerr.influx-bucket-stats`, every 5 min.
  Per repo convention you don't deploy — a human runs `deploy.sh`, which rsyncs
  `collect.py`/`collect.sh`/`.env` to `~/.local/share/influx-bucket-stats/`
  (internal disk; launchd can't read `/Volumes`) and `launchctl load`s the plist.

## Secrets

`.env` (gitignored, chmod 600) from `.env.example`. `INFLUX_TOKEN` needs **read on
all buckets** (to count points) **+ write on `ops`** — the admin token works.
