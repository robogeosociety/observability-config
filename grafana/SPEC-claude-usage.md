# SPEC — Claude Code Token Usage dashboard

Owner question this answers: *"How many tokens is Claude Code on this Mac burning,
by hour / model / project / session, and how much of that is prompt-cache reuse
vs real input+output?"*

**Cost is intentionally out of scope.** Transcripts carry no `costUSD`, so cost
would have to be *derived* from a per-model price table that silently rots as
prices change. We track raw token counts only — honest, and never wrong.

Three pieces, in order — **bucket → collector → dashboard**. The first two are the
data side; the dashboard is `claude-usage.json`. The `claude-usage` entry in
`dashboards.index.d/` is the one-liner.

---

## 1. Where the data comes from

Claude Code writes one JSONL transcript per session under
`~/.claude/projects/<slug>/<session-uuid>.jsonl` (internal disk, **not** `/Volumes`).
Every `type:"assistant"` line carries a `message.usage` block — that's the whole
source. No external API, no scraping. Observed shape (Opus 4.8, 2026-06):

```jsonc
{
  "type": "assistant",
  "timestamp": "2026-05-30T13:39:07.597Z",
  "cwd": "/Volumes/dev/RealityCapture",
  "sessionId": "6e6b1979-…",
  "message": {
    "model": "claude-opus-4-8",
    "usage": {
      "input_tokens": 4346,
      "output_tokens": 126,
      "cache_creation_input_tokens": 5945,
      "cache_read_input_tokens": 16197,
      "service_tier": "standard",
      "server_tool_use": { "web_search_requests": 0, "web_fetch_requests": 0 }
    }
  }
}
```

Facts that drive the design:

- **Cache is the dominant token class.** `cache_read_input_tokens` routinely dwarfs
  `input_tokens`. Tokens must be shown as four distinct classes (input / output /
  cache-create / cache-read), never one "tokens" number, or the picture lies. This
  is exactly what the *cache-vs-real* panel exists to surface.
- **`<synthetic>` model lines exist** (local UI messages) with no real usage — skip
  any line whose model is `<synthetic>` or whose `usage` is missing/all-zero.
- **`cwd` → `project` tag** (basename). **`sessionId` → `session` tag** so we can
  rank "highest usage in a session" (bounded cardinality on a single-user machine —
  see collector notes).

## 2. Collector (`grafana/claude-usage-collector/`)

A small Python script — same shape as `campsites/ingest.py` and `dev-status/` — that
parses the transcripts and writes line protocol to InfluxDB. It does **not** touch
`/Volumes` at runtime (reads `~/.claude`, writes `localhost:8086`), but the repo copy
lives on `/Volumes`, so it follows the dev-status rule: **edit here, `deploy.sh`
copies it to `~/.local/share/claude-usage-collector/` and (re)loads a launchd job**
that runs the deployed copy. launchd cannot exec a script from `/Volumes` (TCC, exit 78).

Behavior:

1. Glob `~/.claude/projects/**/*.jsonl`.
2. Maintain a **cursor** at `~/.local/state/claude-usage-collector/cursor.json` mapping
   `file → byte offset`. Each run seeks to the stored offset, reads only new lines,
   then saves the new offset — idempotent on a 5-minute re-run, no double-counting.
   New files start at 0. If `size < stored offset` (a rewrite/compaction), re-read
   from 0.
3. For each assistant line with real `usage`: emit one point.
4. Batch-write with `precision=ms` (ms timestamps make same-tagset point collisions
   within a session vanishingly unlikely). Skip-and-continue on a malformed line.
5. Schedule: launchd `com.tommy.claude-usage`, every **5 min** (`StartInterval 300`),
   log to `~/Library/Logs/claude-usage-collector.log` (internal disk).

### Bucket + datasource

New InfluxDB bucket **`claude_code`**, retention `0` (history is tiny; trends are the
point). The collector needs a write-scoped `INFLUX_CLAUDE_USAGE_TOKEN` in
`influxdb/.env`. Grafana reads it through a `claude_code` datasource (uid `claude_code`)
added to `provisioning/datasources/influxdb.yml`, using the shared `INFLUX_GRAFANA_TOKEN`
(ensure that token can read the new bucket).

### Schema

Measurement **`tokens`**, one point per assistant message, ts = message `timestamp` (ms).

| Tags (bounded cardinality) | Fields (int) |
|---|---|
| `source` = `claude-cli` | `input_tokens`, `output_tokens` |
| `model` (`claude-opus-4-8`, …) | `cache_creation_tokens`, `cache_read_tokens` |
| `project` (basename of `cwd`) | `total_tokens` (= input+output+cache_creation+cache_read) |
| `service_tier` (`standard`, …) | `web_search_requests`, `web_fetch_requests` |
| `session` (sessionId) | `messages` = 1 (counter) |

`session` is a tag (not a field) so "top sessions" is a one-line group/sum. On a
single-user machine that's ~hundreds–low-thousands of series/year — acceptable; revisit
if it ever balloons.

## 3. Dashboard (`provisioning/dashboards/claude-usage.json`, uid `claude-usage`)

Datasource **influxdb** (Flux), `claude_code`. One template var `$project` (multi, All).
The committed v1 is deliberately a few focused graphs:

- **Token usage by hour** — time series, `aggregateWindow(1h, sum)` stacked by the four
  token classes. The core "what's the spend rhythm" view.
- **Highest usage in a session** — table, `group(["session","project"]) |> sum()` on
  `total_tokens`, sorted desc, top 10. Which sessions were the heavy hitters.
- **Cache vs real tokens** — pie, cache (read+create) vs real (input+output). The
  caching-efficiency headline.

Future panels (by model, by project, web tool use, per-message averages) are in the
index `todos`.

## 4. Activation (one-time, off the PR)

1. `claude_code` bucket + `INFLUX_CLAUDE_USAGE_TOKEN` in `influxdb/.env` (CLAUDE.md recipe);
   mint a fresh Grafana read token covering all dashboard buckets **plus** `claude_code`
   and update `INFLUX_GRAFANA_TOKEN` in `grafana/.env` (the read token is per-bucket).
2. `grafana/deploy-provisioning.sh` to sync the new datasource to the internal-disk
   provisioning copy and reload Grafana (provisioning is no longer bind-mounted from
   `/Volumes`). A token change in `grafana/.env` also needs `docker compose up -d` from
   `grafana/` to re-read the env.
3. `grafana/claude-usage-collector/deploy.sh`, then a first backfill run (cursor at 0 →
   full history).
4. Verify panels light up; capture a Playwright visual baseline.
