# Mountain Out — observability

Observability for [`tommyroar/is-the-mountain-out`](https://github.com/tommyroar/is-the-mountain-out):
a Cloudflare Worker runs a `*/15` cron that classifies whether Mount Rainier is
visible from a UW webcam (ConvNeXt + LoRA, fused with KSEA METAR) and appends one
record per tick to `history.jsonl` in the public `is-the-mountain-out-public` R2
bucket. This directory mirrors that into InfluxDB and ships two Grafana dashboards.

- **Mountain Out — Status** (`mountain-status`): is it out now, class-confidence
  over time, out/not-out timeline, class distribution, METAR visibility & ceiling.
- **Mountain Out — Inference Infrastructure** (`mountain-infra`): tick freshness,
  count, error count, success rate, tick duration, outcomes, recent-error table.

## Data store: proposal

**Recommendation: InfluxDB (`mountain` bucket), mirrored from R2 by a local ingest job.**

The decision mirrors the split this repo already uses for Cloudflare-Worker
projects, and is driven by what the data is *for*.

| | **InfluxDB `mountain`** (chosen) | Cloudflare Analytics Engine |
|---|---|---|
| Already wired to Grafana | ✅ 7 buckets, Flux | ✅ `cloudflare-ae` (campsite collector) |
| Write path | local ingest pulls public R2 `history.jsonl` (the `campsites` pattern) | Worker writes data points inline |
| Retention | unlimited / configurable | ~90 days, sampled (`_sample_interval`) |
| Query | Flux (rich: joins, math, downsample) | ClickHouse SQL, sampled |
| Cross-correlation | ✅ co-located with `tempest_archive` weather, `system` host | ✗ separate store |
| Fit for **model fine-tuning** | ✅ full-fidelity, long-horizon, joinable | ✗ sampled + short retention |

Why InfluxDB here:

1. **Fine-tuning is the goal.** You want to study model behaviour over time —
   confidence drift, false-positive review (the model is tuned for *precision*),
   and correlating predictions against actual weather. That needs **full-fidelity,
   long-retention, joinable** data. Analytics Engine samples and expires (~90d);
   InfluxDB keeps every tick and lets you `join` against the existing
   `tempest_archive` weather bucket already in this org.
2. **No new moving parts on the Worker.** The Worker keeps writing only
   `history.jsonl` to R2; ingestion lives here, exactly like `campsites`
   (the repo "owns ingest"). InfluxDB isn't publicly reachable, so a local
   pull-from-R2 job is the natural path — the Worker never needs an outbound
   InfluxDB credential.
3. **Idempotent + simple.** A point's identity is (measurement, tags, timestamp),
   so re-ingesting the small `history.jsonl` overwrites instead of duplicating.

Analytics Engine remains the better choice for *high-volume, fire-and-forget*
Worker telemetry (as with the campsite collector). If the inference Worker later
emits per-request operational metrics at volume, those could go to AE while the
prediction time-series stays in InfluxDB.

## Schema (`mountain` bucket)

Written by [`ingest.py`](./ingest.py), one ingest run per 15 min:

```
prediction   # ok ticks only, @ state.timestamp_utc
  tags    class_name (not_out|full|partial), model_version, station
  fields  is_out (0|1), class_index, conf_not_out, conf_full, conf_partial,
          visibility_sm?, ceiling_ft?

tick         # every tick (ok or error), @ finished_at
  tags    status (ok|error), error_type?
  fields  ok (0|1), duration_seconds
```

## Setup (one-time)

```sh
source /Volumes/dev/observability/influxdb/.env

# 1. Create the bucket (unlimited retention)
docker exec influxdb influx bucket create --org home \
  --token "$INFLUX_ADMIN_TOKEN" --name mountain --retention 0

# 2. Mint a write-only token scoped to it
docker exec influxdb influx auth create --org home --token "$INFLUX_ADMIN_TOKEN" \
  --write-bucket "$(docker exec influxdb influx bucket list --org home \
     --token "$INFLUX_ADMIN_TOKEN" --hide-headers --name mountain | awk '{print $1}')" \
  --description "mountain ingest write"

# 3. Configure + smoke-test the ingest
cp mountain/.env.example mountain/.env && chmod 600 mountain/.env   # paste the token
uv run --no-project python mountain/ingest.py --dry-run             # inspect line protocol
uv run --no-project python mountain/ingest.py                       # first real write

# 4. Schedule it (every 15 min)
cp mountain/com.tommydoerr.mountain-ingest.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tommydoerr.mountain-ingest.plist
```

> The Grafana `mountain` datasource reuses `${INFLUX_GRAFANA_TOKEN}`; if that
> read token is bucket-scoped rather than org-wide, add `mountain` to its scope.

## Dashboards

File-provisioned via `grafana/provisioning/dashboards/` — they appear
automatically. Both read the `mountain` InfluxDB datasource and are empty until
the first ingest run writes points.
