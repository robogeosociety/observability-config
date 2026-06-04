# Campsite availability ingest

The observability half of the campsite-availability pipeline: pulls daily
availability summaries from R2 → InfluxDB (`campsites` bucket) → the
**Campsite Availability** Grafana dashboard (by site / agency / time, with a
sell-out burn-down + Holt-Winters projection).

```
robot-geographical-society (raw collection)        observability (THIS — ingest)
  backend Worker, cron 13:00 UTC                     campsites/ingest.py  (launchd 07:30)
   Browser Rendering → R2: campsite-raw  ───────────▶ GET summary/<date>/* (R2 S3)
   summary/<date>/<id>.json                           → line protocol → InfluxDB `campsites`
```

**Boundary:** RGS owns *collection → R2*; this repo owns *R2 → InfluxDB →
dashboard* and the InfluxDB-side infra (the `campsites` bucket, its write token,
the Grafana datasource + dashboard).

## Files
| File | Role |
|------|------|
| `ingest.py` | pull `summary/<date>/*` from R2, write to InfluxDB (self-contained) |
| `ingest.sh` | launchd entry — `uv run --no-project --with boto3 python ingest.py` |
| `com.tommydoerr.campsite-ingest.plist` | LaunchAgent, daily 07:30 local |

Dashboard: `grafana/provisioning/dashboards/campsite-availability.json` ·
datasource `campsites` in `grafana/provisioning/datasources/influxdb.yml`.

## Prediction readiness (`predict_readiness`)

The ingest also emits one global `predict_readiness` point per run (checkpoint D
of robot-geographical-society `PREDICT.md` §10): a 0–1 gauge that fills as
collection history deepens, gating when sell-out predictions can be trusted. It
scans the recent `sites/<date>/*` history (`--readiness-max-days`, default 60)
and computes `(events · coverage · depth)^⅓` — mirroring rgs
`predict/readiness.py`, which must stay in lockstep. Feeds the **Campsite
Predictions** dashboard (`…/dashboards/campsites/campsite-predictions.json`).

```sh
python3 ingest.py --readiness-only --dry-run   # compute + print the point, no write
python3 ingest.py --no-readiness               # skip it during a normal ingest
```

## Setup
1. **InfluxDB** `campsites` bucket + write token already exist; the token is in
   `campsites/.env` (gitignored).
2. **R2 read token** (still needed): Cloudflare dashboard → R2 → Manage R2 API
   Tokens → Object Read for `campsite-raw`; put `R2_ACCOUNT_ID` /
   `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` in `campsites/.env`.
3. **Test**: `uv run --no-project --with boto3 python ingest.py --dry-run`
4. **Install the job**:
   ```sh
   cp campsites/com.tommydoerr.campsite-ingest.plist ~/Library/LaunchAgents/
   launchctl load -w ~/Library/LaunchAgents/com.tommydoerr.campsite-ingest.plist
   ```
   Needs Full Disk Access on `/bin/zsh` (reads `/Volumes` + `.env`) — same grant
   as `influxdb/backup.sh`. Log: `~/Library/Logs/campsite-ingest.log`.
