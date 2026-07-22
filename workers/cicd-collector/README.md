# cicd-collector — org CI/CD telemetry + red-CI alerting on Workers

WS5 of CICD-everything (robogeosociety/robot-geographical-society#167; task
#156). The Workers port of the parked #149 launchd collector
(`grafana/cicd-collector/collector.py`, branch head `f9ab362`) — off the
mini's TIG stack and onto Workers cron + Analytics Engine. With InfluxDB on
the mini failing, the **red-CI Discord alert** and the **collector_poll
heartbeat** are the first-class outputs; the run history is what dashboards
build on next.

## Beats

| cron          | beat      | what happens                                                        |
| ------------- | --------- | ------------------------------------------------------------------- |
| `*/5 * * * *` | poll      | completed runs since the overlap window → `cicd_workflow_runs`; red default-branch runs → one compact `#dev` message (alert-once per `run_id:attempt`); one `cicd_collector_polls` heartbeat row |
| `7 * * * *`   | inventory | one `cicd_workflow_inventory` row per workflow file per repo (the pipeline map, run-or-not) + a heartbeat row |

Discovery is dynamic (`GET /installation/repositories`, non-archived): a new
repo or workflow shows up within one poll, no config edit. API budget ≈ 3 +
one `runs` call per repo per poll (~20/tick at today's org size), well inside
the app installation's 5k/hour.

## Datasets (Workers Analytics Engine)

One dataset per #149 measurement shape. AE columns are positional — the
mapping is fixed here and mirrored in `src/index.js`:

**`cicd_workflow_runs`** — one row per completed run, write-once
(KV seen-set `run_id:attempt`; re-runs land as their own row).

| column   | value        | column  | value                                   |
| -------- | ------------ | ------- | --------------------------------------- |
| index1   | repo         | double1 | ok (1 = success)                        |
| blob1    | repo         | double2 | duration_s                              |
| blob2    | workflow     | double3 | queue_s                                 |
| blob3    | branch       | double4 | run_attempt                             |
| blob4    | event        | double5 | completed_at (epoch s — **query this**) |
| blob5    | conclusion   | double6 | run_id                                  |

**`cicd_workflow_inventory`** — index1/blob1 repo, blob2 workflow, blob3
state, blob4 path; double1 present (1), double2 workflow_id.

**`cicd_collector_polls`** — index1/blob1 beat (`poll`|`inventory`), blob2
outcome (`ok`|`error`); doubles: repos, runs_seen, runs_written, alerts_sent,
errors, api_calls, duration_ms, rate_remaining.

## Design deltas from #149

- **Append-only + write-time timestamps.** AE can't overwrite on identical
  tags+timestamp (the InfluxDB idempotency trick) and can't backdate rows. The
  overlap window (30 min) stays for reliability; a KV seen-set makes writes
  once-only; the true completion time is `double5` (`completed_at`) — query on
  it, not the row timestamp (first-deploy backfill rows all share write time).
- **First poll backfills 7 days** (vs #149's 30): enough for the parity check,
  bounded pages per repo.
- **Repo discovery via the App installation**, not a user token (the
  github-heartbeat pattern) — also supplies `default_branch` for the alerts.
- **Workflow tag** on 5-min ticks is `run.name` (fallback: file basename);
  the file-level name map costs a call per repo, so it rides the hourly beat.
- **Retention:** AE keeps ~3 months. Long history (D1 or R2 export) is a later
  phase (#156's dashboard checkpoint), not this Worker.

## Alerting

A completed run with `conclusion=failure` on its repo's default branch posts
one compact line to `#dev` (bot-token REST, channel resolved by name — the
deploy-gate pattern). Dedupe: KV alert-once gate keyed `run_id:attempt`,
independent of the write gate, so a failed Discord post retries for the whole
overlap window. Note: `github-heartbeat` (discobots) also announces red CI on
a 30-min beat — this lane is the 5-min replacement for TIG alerting; retire
the heartbeat's `scanCiFailures` once this is trusted.

## Verify (sanity check, same-day)

```sql
-- AE SQL API: POST https://api.cloudflare.com/client/v4/accounts/$ACC/analytics_engine/sql
SELECT blob1 AS repo, blob5 AS conclusion, SUM(_sample_interval) AS runs
FROM cicd_workflow_runs
WHERE timestamp > NOW() - INTERVAL '1' DAY
GROUP BY repo, conclusion ORDER BY repo
```

against `gh run list -R robogeosociety/<repo> --created ">=<date>" --limit
200 --json conclusion` per repo. `SUM(_sample_interval)` (not `count()`) is
the AE-correct row count; at this volume sampling is 1:1 so they match.
Spot-check individual runs via `double6` (run_id). Heartbeat freshness:
newest `cicd_collector_polls` row with `blob1='poll'` should never be older
than ~6 min.

## Deploy

CD: `.github/workflows/cicd-collector.yml` (push to main touching
`workers/cicd-collector/**`; `environment: production` behind the Discord
deploy gate). It resolves/creates the `cicd-collector-state` KV namespace,
substitutes the placeholder id, `wrangler deploy`s, and syncs secrets
(`DISCORD_BOT_TOKEN`, `APP_PRIVATE_KEY` → PKCS#8). Datasets need no
provisioning — they exist on first write.
