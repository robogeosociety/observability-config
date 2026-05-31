# Cloudflare Worker — collector history dashboard (exploration)

Goal: a Grafana dashboard for the **campsite-availability collector** Cloudflare
Worker (robot-geographical-society
[PR #42](https://github.com/tommyroar/robot-geographical-society/pull/42)) — its
run history and health over time.

## What the worker does (context)

- `scheduled()` cron `0 13 * * *` (06:00 PT) enqueues one job per reservable
  campsite (61: 39 rec.gov + 22 WA) onto the `campsite-availability` Queue.
- `queue()` consumer drains in batches of 10 (`max_concurrency: 2`), launches
  **Browser Rendering** per site, fetches ~6 months availability, and writes
  `raw/<date>/…` + `summary/<date>/<id>.json` to the `campsite-raw` R2 bucket.
  Failures retry ×3 → DLQ.
- `POST /collect/run?limit=N` triggers an on-demand run.

## What "collector history" should show

- Run cadence: did the daily cron fire? when, how long?
- Per-run coverage: sites attempted / succeeded / failed (of 61).
- Queue health: enqueued vs consumed, retries, DLQ depth.
- Browser Rendering usage (concurrency, durations, errors).
- Freshness: age of the latest `summary/<date>/` set in R2.

## Candidate data sources (the thing to decide)

1. **Cloudflare GraphQL Analytics** (`workersInvocationsAdaptive`,
   `queueConsumerMetricsAdaptiveGroups`, Browser Rendering usage) via the
   Cloudflare Grafana plugin or an Infinity/JSON datasource hitting the GraphQL
   API. Pro: zero worker changes, real invocation/queue/error data. Con: new
   datasource + API token to provision; retention is limited (Workers analytics
   ~ days/weeks).
2. **Worker → InfluxDB heartbeat** — the worker (or the `campsites/ingest.py`
   side) writes a `collector_run` measurement (sites_ok/sites_failed/duration_s/
   dlq_depth, tag `target_date`) to a bucket, mirroring `influxdb/backup.sh`'s
   `backup` heartbeat pattern. Pro: fits the existing InfluxDB+Grafana stack,
   long retention, full control of fields. Con: requires emitting the heartbeat
   (worker egress to InfluxDB, or derive it during ingest).
3. **R2 object inventory as proxy** — count/age `summary/<date>/*` objects in
   `campsite-raw` via the Cloudflare MCP / Infinity. Pro: no new plumbing, proves
   end-to-end success. Con: coarse (success-only, no per-run timing/failures).

Likely answer: a blend — **(2)** for durable run history (best fit for this
stack) plus **(1)** for live queue/invocation detail. TBD in this branch.

## Open questions

- Does the worker have egress to push to InfluxDB, or should the ingest job
  derive run history from what landed in R2?
- Provision a Cloudflare datasource (plugin vs Infinity+GraphQL), or InfluxDB-only?
- Which bucket — reuse `campsites`, or a dedicated `ops`-style heartbeat bucket?

---

Scaffold only — dashboard JSON + datasource land here as the approach is decided.
Deployed Mac-system work is unaffected (separate worktree/branch).
