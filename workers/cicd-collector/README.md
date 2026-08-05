# cicd-collector — org CI/CD telemetry + red-CI alerting on Workers

> **Alerts moved `#dev` → `#ops` on 2026-07-27.** Everything this Worker posts is an
> alarm, and #dev had drifted into being the alarm channel by accident — 35 of its last
> 100 messages were failure cards against 13 about development. Alarms now land where
> the @ops investigator lives. The destination is `ALERT_CHANNEL` in `wrangler.toml`,
> resolved by channel name at run time.

WS5 of CICD-everything (robogeosociety/robot-geographical-society#167; task
#156). The Workers port of the parked #149 launchd collector
(`grafana/cicd-collector/collector.py`, branch head `f9ab362`) — off the
mini's TIG stack and onto Workers cron + Analytics Engine. With InfluxDB on
the mini failing, the **red-CI Discord alert** and the **collector_poll
heartbeat** are the first-class outputs; the run history is what dashboards
build on next.

## Beats

| cron            | beat      | what happens                                                        |
| --------------- | --------- | ------------------------------------------------------------------- |
| `*/5 * * * *`   | poll      | completed runs since the overlap window → `cicd_workflow_runs`; red default-branch runs → one compact `#ops` message (alert-once per `run_id:attempt`); one `cicd_collector_polls` heartbeat row |
| `3-58/5 * * * *`| vitals    | reads the mini's `host_vitals` dataset back through the AE SQL API; disk / memory / vector-silence / bus-silence breaches → one compact `#ops` message (alert-once per signal per day) + a heartbeat row |
| `7 * * * *`     | inventory | one `cicd_workflow_inventory` row per workflow file per repo (the pipeline map, run-or-not) + a heartbeat row |

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

**`cicd_collector_polls`** — index1/blob1 beat (`poll`|`inventory`|`vitals`),
blob2 outcome (`ok`|`error`); doubles: repos, runs_seen, runs_written,
alerts_sent, errors, api_calls, duration_ms, rate_remaining. The doubles are
named for the poll beat; the **vitals** beat reuses the same row shape (one
heartbeat dataset per Worker) with `repos` = signals evaluated, `runs_seen` =
signals breaching, `api_calls` = 4 (its AE queries), and `rate_remaining` = -1.

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
one compact line to `#ops` (bot-token REST, channel resolved by name — the
deploy-gate pattern). Dedupe: KV alert-once gate keyed `run_id:attempt`,
independent of the write gate, so a failed Discord post retries for the whole
overlap window. Note: `github-heartbeat` (discobots) also announces red CI on
a 30-min beat — this lane is the 5-min replacement for TIG alerting; retire
the heartbeat's `scanCiFailures` once this is trusted.

## Host vitals (the `vitals` beat, #161)

The mini's Vector agent pushes `host_metrics` into the **`host_vitals`**
Analytics Engine dataset via the `host-vitals` Worker. That Worker is
deliberately push-only — no crons, no KV, no outbound tokens — so the alerting
half lives **here**, where the alert plumbing already is: the KV alert-once
store, the Discord bot client, the `#ops` resolution, the heartbeat dataset. One
lane means one dedupe store and one place to silence a signal. (A cron in
`host-vitals` would hand the ingest endpoint a Discord bot token and a KV
binding it has no other use for, and split alert state across two Workers.)

| signal | query | fires when |
| --- | --- | --- |
| **disk** | `filesystem_used_ratio` per mount, weighted mean over `VITALS_DISK_WINDOW_MIN` | mount over `VITALS_DISK_ALERT_RATIO` |
| **memory** | `memory_available_bytes`, weighted mean over `VITALS_MEM_WINDOW_MIN` | under `VITALS_MEM_ALERT_BYTES` with ≥ `VITALS_MEM_MIN_SAMPLES` samples. `memory_swap_used_bytes` rides the same query as context on the line, never as a trigger |
| **vector-silent** | `max(double2)` per host | newest **source** observation older than `VITALS_SILENT_SEC`, or the host absent from the whole `VITALS_FRESH_LOOKBACK_MIN` window |
| **bus-silent** | `max(double2)` of `bus.fleet.supervisor.tick` (collector `bus` — the fleet-bus Worker mirrors every publish) | **self-arming**: newest row older than `VITALS_BUS_SILENT_SEC` *and* newer than `VITALS_BUS_DISARM_SEC` — quiet before the mini's dual-publish (obs-config#174) ever starts, and quiet again if the lane is formally retired |

Freshness reads `double2` (source time), not the row timestamp: AE stamps rows
at write time, so a Vector that buffered for an hour and then flushed looks
perfectly healthy on write time. That is exactly the case this catches. The
other two window on the row `timestamp` — the indexed column, within ~2 min of
source time in the steady state (60 s scrape + 60 s sink batch). Averages are
`sum(double1 * _sample_interval) / sum(_sample_interval)`, the weighted mean
that stays correct if AE ever starts sampling this dataset.

`VITALS_HOST` is matched **case-insensitively** against `blob1`: the docs write
the mini as `tommys-mac-mini.local`, its `gethostname()` says
`Tommys-Mac-mini.local`, and a liveness alert silently blinded by capitalisation
would be worse than no alert.

**Dedupe + recovery.** Alert-once per (signal, mount, UTC day), in the same KV
doc as the red-CI gate under `vitals`. A breach announces once; a signal that
recovers posts one `✅` line and ends the episode. `alerted` is deliberately not
reset by recovery — a mount that crosses 90%, is cleaned up, and fills again the
same day stays quiet until tomorrow. Between the alert and clear thresholds sits
a **hysteresis band** where nothing happens at all, so a value hovering on the
line cannot alternate alert/recovery every five minutes. Missing data is
`unknown`: it never alerts and, just as importantly, never manufactures an
all-clear.

### Thresholds — every one of these is a guess

The lane went live 2026-07-23; these numbers have **well under a day of baseline
behind them** and are deliberately conservative (quiet beats noisy while the lane
earns trust). They are all `[vars]` in `wrangler.toml`, so re-cutting them after
a week of real data is a config edit, not a code change. Specifically unproven:

- **`/` on macOS** is the sealed system volume and its `used_ratio` reflects the
  whole APFS container — its normal range is unknown here. `/Volumes/dev` is the
  mount that actually matters.
- **500 MB available memory** on the 8 GB box is a plausible floor, not a
  measured one. The InfluxDB OOM loop (2026-07-19) is the event it is watching
  for, and nobody has yet seen what available-memory does on the way into one.
- **10 minutes of silence** (the issue said ~5): 60 s scrape + 60 s sink batch +
  AE ingest lag stack up, and a false "vector is down" costs more trust than five
  extra minutes of detection latency costs data.
- **Swap** is printed, never triggered on: a few hundred MB in use is normal on
  macOS and says nothing by itself. If a week of baseline shows swap growth
  leading the OOM, promote it to a trigger then.

### Dry run

`test/dryrun.mjs` prints the exact SQL without any credentials, and with
`CF_ACCOUNT_ID` + `CF_AE_READ_TOKEN` set it runs the queries read-only and shows
the verdict and the message that *would* go to `#ops` — no post, no KV write, no
data point. Any `VITALS_*` var is honoured, so a candidate threshold can be
trialled against live data before it is committed:

```sh
cd workers/cicd-collector
node test/dryrun.mjs                                   # SQL only
CF_ACCOUNT_ID=… CF_AE_READ_TOKEN=… node test/dryrun.mjs   # …and run it
VITALS_DISK_ALERT_RATIO=0.5 CF_ACCOUNT_ID=… CF_AE_READ_TOKEN=… node test/dryrun.mjs
```

### Verify (once the token is wired)

- Fill a disk on the mini, or `VITALS_DISK_ALERT_RATIO=0.5 node test/dryrun.mjs`
  → the disk line appears; restore → one `✅`, then silence (the gate holds).
- `launchctl bootout gui/$UID/com.tommy.vector` for 15 min → the vector-silent
  line; `bootstrap` again → recovery once row ages fall back under half the
  threshold.
- Heartbeat freshness: newest `cicd_collector_polls` row with `blob1='vitals'`
  should never be older than ~6 min.

## Tests

`node --test test/vitals.test.mjs` (no deps; Node ≥ 18), also run in CI on PRs
touching this Worker. Covers the thresholds, the hysteresis bands, the
alert-once/recovery gate, the tag parsing, and the SQL text. It does **not**
cover whether Analytics Engine accepts that SQL — nothing here has been run
against live data; that is what the dry-run harness is for.

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
(`DISCORD_BOT_TOKEN`, `APP_PRIVATE_KEY` → PKCS#8, `CF_ACCOUNT_ID`, and
`CF_AE_READ_TOKEN` for the vitals beat). Datasets need no provisioning — they
exist on first write.

`CF_AE_READ_TOKEN` is cloudflare-tfvend's `analytics_read` output (an **Account
Analytics Read** token — writing Analytics Engine needs no credential, reading it
does). Until it is vended and set as a repo/org secret the deploy still succeeds
and logs a warning; the vitals beat then throws once per tick and writes an
`error` heartbeat, while the poll and inventory beats carry on unaffected.
