# anomaly-detector

The **telemetry layer** behind the Alerts & Anomalies dashboard. A scheduled
window z-score detector that turns raw metrics into a queryable **anomaly event
stream** — the foundation for letting Claude analyse and tune detection over time.

## What it does

Every 10 min (launchd), for each metric in `anomaly-detection.yaml`:
1. pull the series over `window` from InfluxDB,
2. compute mean + stddev across the window,
3. score the latest point `z = (value - mean) / stddev`,
4. if `|z| >= z_warn`, write an anomaly event to the **`ops`** bucket,
   measurement **`anomaly`**:
   - tags: `metric`, `bucket`, `method=window_zscore`, `severity` (warn|crit), `direction` (high|low)
   - fields: `zscore`, `value`, `mean`, `stddev`, `n`

The **Alerts & Anomalies** dashboard reads `ops.anomaly`.

## The self-improving loop (why this exists)

Detection config is **code** (`anomaly-detection.yaml`). The event stream is the
**memory**. A scheduled Claude pass (`/schedule`, via the `ask-dash` read-only
token) reads the event history + the raw metrics and answers "which metrics are
noisy / missing / mis-thresholded?", then opens a **PR** editing this YAML. Git
history is the audit trail of how detection evolved. No model runs inside a
dashboard panel — the dashboard just visualises the recorded events.

Next layers (not in v1): a **verdict** label per event (true/false positive) as
the tuning signal, and **hour-of-day baselines** so the flat window z-score stops
flagging the normal diurnal ramp.

## Run / deploy

```sh
cp .env.example .env    # fill INFLUX_TOKEN (read source buckets + write ops), chmod 600
uv run python detector.py        # one pass, from here (interactive)
./deploy.sh                      # install on the internal disk + load launchd
```

Maintainer-applied (launchd job + token), like the other collectors — see
`AGENTS.md`. Tests are hermetic: `test_detector.py` covers the z-score math,
line-protocol shape, and config validity (runs in CI).
