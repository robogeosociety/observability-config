# host-vitals — Vector → Analytics Engine ingest Worker

The mini's host telemetry after TIG retirement (#156; Tommy's 2026-07-22
ruling: "vector it is, pushing to native cloudflare off the box"). A Vector
agent on the mini (config lives in `robogeosociety/supervisor`,
`ops/vector/`) scrapes `host_metrics` every 60s, listens for the WeatherFlow
Tempest's LAN UDP broadcasts on port 50222, and POSTs http-sink batches here.
This Worker validates auth, parses the batch, and fans rows out to two
Analytics Engine datasets. Push-only — no crons, no KV.

## Endpoints

| Route | Method | Auth | Behavior |
|---|---|---|---|
| `/ingest` | POST | `authorization: Bearer <VITALS_INGEST_KEY>` | Parse batch → AE writes. 401 bad auth, 400 malformed. |
| `/ingest` | GET | none | 200 — Vector's sink healthcheck GETs the sink uri. |
| `/health` | GET | none | 200 `{ok: true}` |

Auth is a **static bearer**: Vector's http sink can only set fixed headers
(no per-request HMAC), so the key in `VITALS_INGEST_KEY` (repo Actions secret
→ synced to the Worker by CD; mini side reads it from
`~/.config/host-vitals/env`) is the whole model. Rotate by setting a new
value in both places.

## Wire format (captured, not guessed)

Captured from a live vector **0.57.0** run (`encoding.codec = "native_json"`,
`compression = "gzip"` — see `test/sample-batch.ndjson` for verbatim lines):

- Body: **newline-delimited JSON**, one event per line, gzipped
  (`content-encoding: gzip`; the sink sends no `content-type`).
- Metric lines: `{"metric": {"name", "namespace": "host", "tags": {...},
  "timestamp": "<RFC3339>", "kind": "absolute", "gauge"|"counter":
  {"value": <num|null>}}}` — note `value` **can be null** (macOS autofs
  ratios); those are skipped.
- Log lines (Tempest, post vector-side filter+remap): `{"log": {"type":
  "obs_st"|"rapid_wind", "serial_number", "hub_sn", "obs"|"ob", "host",
  "stream": "tempest", "source_type": "socket", "timestamp", ...}}`.

## Column maps

Analytics Engine stamps rows at **write time** — the source timestamp rides
in a double. Query on that, not the row timestamp, whenever Vector may have
been buffering.

### `host_vitals` (binding `VITALS`) — index1 = host

| Column | Content |
|---|---|
| blob1 | host (e.g. `tommys-mac-mini.local`) |
| blob2 | metric name (`memory_available_bytes`, `filesystem_used_ratio`, `load1`, `cpu_seconds_total`, …) |
| blob3 | remaining tags collapsed `k=v,k=v` (sorted; host/collector dropped) e.g. `cpu=0,mode=idle` or `device=disk3s5,filesystem=apfs,mountpoint=/` |
| blob4 | collector (`cpu` `memory` `disk` `filesystem` `load` `network`) |
| double1 | value (gauge or counter — counters are cumulative totals) |
| double2 | source timestamp, epoch seconds |

### `weather_obs` (binding `WEATHER`) — index1 = serial_number

One row per observation row. The obs array is stored **verbatim** as doubles
(generic mapping — no per-field hand map) plus a raw JSON blob for full
fidelity; decode positions per the [WeatherFlow UDP
reference](https://weatherflow.github.io/Tempest/api/udp/v171/) (obs_st:
[0]=epoch, [1]=lull, [2]=avg, [3]=gust m/s, [4]=dir, [6]=hPa, [7]=°C,
[8]=%RH, [12]=rain mm/min, [16]=battery V …).

| Column | Content |
|---|---|
| blob1 | type (`obs_st` \| `rapid_wind`) |
| blob2 | serial_number (`ST-00204728`) |
| blob3 | the raw obs row, JSON (`[1784850866,0.85,…]`) |
| blob4 | reporting host |
| double1..N | the obs array verbatim — obs_st: 18 values, double1=epoch; rapid_wind: `[epoch, m/s, deg]` |

This **supersedes the collection side** of the old Tempest path (WeatherFlow →
Home Assistant `weatherflow` integration → HA `influxdb` integration →
InfluxDB `home_assistant` bucket, frozen with the TIG stack). Consumers
(e.g. obsidian-automations `weekly/collectors/tempest_weather.py`) re-point
to the AE SQL API as a follow-up.

## Sample queries (AE SQL API — `SUM(_sample_interval)`, not `count()`)

```sql
-- disk fullness (last ~10 min), per mount
SELECT blob3 AS mount, avg(double1) AS used_ratio
FROM host_vitals
WHERE blob2 = 'filesystem_used_ratio' AND timestamp > NOW() - INTERVAL '10' MINUTE
GROUP BY mount;

-- available memory over the day
SELECT toStartOfInterval(timestamp, INTERVAL '5' MINUTE) AS t, avg(double1) AS bytes
FROM host_vitals
WHERE blob2 = 'memory_available_bytes' AND timestamp > NOW() - INTERVAL '1' DAY
GROUP BY t ORDER BY t;

-- vector-silent check: newest row age
SELECT max(timestamp) FROM host_vitals WHERE blob1 = 'tommys-mac-mini.local';

-- today's temperature (obs_st field 7, °C)
SELECT toStartOfInterval(timestamp, INTERVAL '1' HOUR) AS t, avg(double8) AS temp_c
FROM weather_obs
WHERE blob1 = 'obs_st' AND timestamp > NOW() - INTERVAL '1' DAY
GROUP BY t ORDER BY t;
```

## Tests

`node --test test/shape.test.mjs` (no deps; Node ≥ 18). Runs in CI on PRs
touching this Worker (`.github/workflows/host-vitals.yml` test job). The
sample batch is a decompressed, verbatim vector 0.57.0 capture.
