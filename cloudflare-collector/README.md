# Cloudflare Worker — collector history dashboard

Goal: a Grafana dashboard for the **campsite-availability collector** Cloudflare
Worker (`robot-geographical-society-backend`) — its run history and health over
time.

## Architecture (as of robot-geographical-society PR #42)

The collector went **all-in on Cloudflare Workflows** (dropped the earlier
Queues + Browser Rendering/Playwright design):

- **Cron** `0 13 * * *` (06:00 PT) → `scheduled()` creates a `COLLECTOR_WF`
  Workflow instance for the day.
- **`CampsiteCollectorWorkflow`** — one `step.do` per site (61: rec.gov + WA),
  each a plain `fetch` of availability → R2 `raw/<date>/…` +
  `summary/<date>/<id>.json`. Per-step retries (4×, exp backoff), resume after
  eviction. Returns `{date, collected, results}`.
- **`HotDateWatchWorkflow`** — adaptive watcher for one `(campsite, target_date)`:
  dense burn-down points to `watch/…`, `step.sleep` on an adaptive cadence.
- Boundary unchanged: RGS owns *collection → R2*; this repo owns
  *R2 → InfluxDB / analytics → Grafana*.

## Decision: data source = Workers Analytics Engine (+ native Workflow analytics)

Decided after probing the live account (`tommyroar-dev`,
`d7adee58513c1b2f770ccaac90cf114f`, now on **Workers Paid**).

| Source | Role | Retention |
|--------|------|-----------|
| **Workers Analytics Engine** (dataset `campsite_collector`) | **primary** — custom per-run coverage the built-ins lack | ~90 days |
| Workflows GraphQL (`workflowsAdaptiveGroups`) | optional augment — live instance/step status, errored runs | 31 days |

Rejected: Infinity-only (no time macros for SQL), worker→InfluxDB heartbeat
(needlessly couples the worker to InfluxDB given Workflows + AE are native and
already paid-for).

### AE dataset `campsite_collector`

Emitted per successfully-collected site, **inside the `step.do` on success**
(replay-safe — the step result is cached, so no double-count on resume/retry):

- `index1` = `site.id`
- `blobs` = `[run_date, agency, kind, name, outcome("ok")]`
- `doubles` = `[dates_collected, duration_ms]`

Per-run rollups ("sites OK of 61", per-agency, per-`target_date`) come from SQL
`GROUP BY blob1`, weighting counts by `SUM(_sample_interval)`. Outright site
failures surface via the native Workflow *errored* status (complementary).

The worker change (binding + `writeDataPoint`) is proposed on
[robot-geographical-society PR #42](https://github.com/tommyroar/robot-geographical-society/pull/42)
for the agent finishing that PR; the Grafana half (this repo) is in scope here.

## Grafana wiring (this repo)

Cloudflare's recommended path — AE's SQL is ClickHouse-compatible:

- **Datasource:** ClickHouse (Altinity) plugin, URL
  `https://api.cloudflare.com/client/v4/accounts/d7adee58513c1b2f770ccaac90cf114f/analytics_engine/sql`,
  a custom header `Authorization: Bearer ${CF_ANALYTICS_TOKEN}`, standard auth
  disabled. Use `Column:DateTime = timestamp` so `$timeSeries` / `$timeFilter`
  macros bucket by zoom level. (Plugin install = a Grafana container change:
  `GF_INSTALL_PLUGINS`, then recreate.)
- **Token:** a read-only Cloudflare API token scoped **Account → Account
  Analytics → Read**, stored in `grafana/.env` as `CF_ANALYTICS_TOKEN`
  (gitignored). *(Action: mint this in the Cloudflare dashboard.)*
- **Dashboard** `collector-history.json`: daily run cadence, sites OK/failed of
  61, per-agency / per-`target_date` coverage, step durations; optionally a live
  instance-status panel from the Workflows GraphQL dataset.

Example AE SQL (sites collected per run day, last 30d):

```sql
SELECT toStartOfDay(timestamp) AS day, blob2 AS agency,
       SUM(_sample_interval) AS sites_ok,
       SUM(_sample_interval * double1) / SUM(_sample_interval) AS avg_dates
FROM campsite_collector
WHERE timestamp > now() - INTERVAL '30' DAY AND blob5 = 'ok'
GROUP BY day, agency ORDER BY day
```

## Status / next steps

1. [x] (RGS) land the AE `writeDataPoint` change → emitting (RGS commit `92abdba`).
3. [x] add ClickHouse plugin (`vertamedia-clickhouse-datasource` in `GF_INSTALL_PLUGINS`)
   + datasource provisioning (`grafana/provisioning/datasources/cloudflare-ae.yml`).
4. [x] `collector-history.json` scaffolded — info panel + coverage table + sites
   OK/failed timeseries (uid `collector-history`).
2. [x] `Account Analytics: Read` token minted + in the live `grafana/.env` as
   `CF_ANALYTICS_TOKEN`. Verified against the AE SQL API (returns real rows).
5. [x] **Verified live end-to-end** — temporarily provisioned the datasource +
   dashboard into the running Grafana (plugin installed into the container) and
   rendered both query panels against the real AE endpoint: coverage table
   (e.g. `2026-05-31`: 15 attempts / 14 ok / 1 failed / 807 ms avg) and the sites
   OK/failed timeseries both show real data. The ClickHouse query/macro shapes
   (`$timeFilter`, `$timeSeries`) work as-is — **no query changes needed**. Temp
   provisioning was then removed; the plugin stays staged in the container volume.
6. [ ] **Permanent deploy** — merge this PR (and #2) so the primary worktree the
   running Grafana reads gets `cloudflare-ae.yml` + `collector-history.json` and the
   compose `GF_INSTALL_PLUGINS`/`CF_ANALYTICS_TOKEN` passthrough. (Token already in
   the live `.env`; plugin already installed — so post-merge it comes up live with
   no extra steps.)

Deployed Mac-system work (PR #2) is unaffected — separate worktree/branch.
