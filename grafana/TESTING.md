# Testing

A test pyramid covering the deployed dashboards and collectors.

```
              ▲  e2e (few)         Playwright — real browser against running Grafana
             ╱ ╲                   grafana/playwright/*.spec.ts
            ╱   ╲ integration      live Grafana/InfluxDB/collector contracts (self-skip if down)
           ╱     ╲                 grafana/tests/test_grafana_integration.py
          ╱       ╲                influxdb/tests/test_influx_integration.py
         ╱         ╲               grafana/dev-status/test_server.py (-m integration)
        ╱___________╲ unit/static  pure functions + config validation (hermetic, runs in CI)
                                   grafana/tests/test_*_static.py
                                   grafana/dev-status/test_server.py
                                   influxdb/tests/test_backup_static.py
```

## Run

```sh
./run-tests.sh            # all three tiers
./run-tests.sh unit       # Tier 1 only (hermetic, fast)
./run-tests.sh integration
./run-tests.sh e2e
```

Python tiers run via `uv` (pytest + pyyaml, no project venv needed). E2E uses the
`grafana/playwright` package (`npm install` + `npx playwright install chromium`
once).

## What each tier covers

**Tier 1 — unit + static (hermetic, no running stack; also runs in GitHub Actions)**
- Collector pure functions (`target_host_port`, `endpoint_url`, registry parsing)
  and `collect()` row-building with mocked tailscale/probe IO.
- Every dashboard JSON: valid, required keys, unique panel ids, unique uids,
  and **every panel's datasource uid resolves to a provisioned datasource**.
- Datasource YAML: valid, required fields, unique uids, Flux for InfluxDB.
- Provider: **`allowUiUpdates: false`** is locked in.
- **All** dashboards (`test_dashboards_static.py`): the *nodata* regression guards
  (repo-wide as of #67) — every time-axis (`timeseries`/`trend`) panel binds to the
  dashboard picker (ClickHouse `$timeFilter`/`$timeSeries`, Flux `v.timeRangeStart`)
  and never hardcodes its own window; ClickHouse targets that use the time macros
  declare `dateTimeColDataType`; and a single-value query variable feeding an
  `== "${var}"` filter is data-scoped so it can't default to a dataless edge value.
  A legitimately fixed-window time-axis panel goes in the `TIME_AXIS_EXCEPTIONS`
  allowlist (keyed by `(dash_uid, panel_id)`) — encode the exception, don't weaken
  the rule. New dashboards are covered automatically (the tests glob the tree).
- `backup.sh`: shell syntax + presence of safety flags, backup/cp/prune steps,
  and that the token comes from the env (never hardcoded).

**Tier 2 — integration (needs the live stack; tests self-skip if a service is down)**
- Collector: spawns a real instance on an ephemeral port, checks the HTTP/JSON contract.
- Grafana: each dashboard is provisioned (read-only) and matches its file; every
  datasource exists; InfluxDB datasource health is OK.
- **All** dashboard panels (`test_dashboards_integration.py`, repo-wide as of #67):
  resolves each dashboard's template variables to Grafana's default selection,
  interpolates them into every panel query, and runs it through `/api/ds/query` over
  the dashboard's default range **and** `now-24h` — failing any panel that comes
  back *No data*. Health-aware: a query that errors on a **healthy** datasource is a
  real bug (an unsupported function, a type conflict — bug class 3); on an
  **unhealthy** one (token not configured, e.g. AE/R2) it's skipped, not failed.
  Per-dashboard exceptions (legitimately-empty panels, inapplicable windows) live
  in `tests/dashboard_coverage.yaml`, keyed by dashboard uid — a dashboard with no
  entry is strict. `allow_empty` suppresses *empty* results only; a real query
  error still fails.
- InfluxDB: healthy, the org exists, and every bucket a datasource points at exists.

**Tier 3 — e2e (Playwright, real browser)**
- Tempest dashboard renders without panel errors / "No data"; the Pressure panel
  mounts a canvas (guards the legacy-legend blank-panel bug).
- Status page renders, the dev-status collector serves its contract, the Routes &
  backends table is populated, and the live-summary stat shows readable numbers.
- **Visual regression**: panels are screenshotted via Grafana `d-solo` and diffed
  against committed baselines with `toHaveScreenshot`. This is what catches the
  *canvas-drawn* regressions DOM assertions can't see — blank panels, collapsed
  axes, unreadable stats. Two flavours:
  - `visual.spec.ts` — **fixed past time range** (immutable InfluxDB data →
    deterministic, tight images).
  - `campsites-visual.spec.ts` + `dashboards-visual.spec.ts` (repo-wide as of #67)
    — **relative** window for live/continuous data, where the drift-proof guard is
    the structural pair (canvas mounted **and** no "No data") and the screenshot
    runs at a loose threshold. Driven by a `PANELS` manifest: cover a new
    dashboard by adding rows, not a new spec. Only reliably-rendering panels are
    baselined; event-driven/weather-dependent panels (the
    `dashboard_coverage.yaml` allow-empty set) are intentionally left to the
    integration tier so a "No data" frame never gets baked into a baseline.

  Baselines live in `playwright/<spec>.spec.ts-snapshots/` (machine-specific;
  regenerate after an intentional change with
  `npx playwright test <spec> --update-snapshots`).

## Troubleshooting tools

- **grafana-image-renderer** (compose service `renderer`): server-side panel PNGs,
  the fastest way to eyeball one panel deterministically (no login/scroll/lazy-render):

  ```sh
  source .env; AUTH="$GRAFANA_ADMIN_USER:$GRAFANA_ADMIN_PASSWORD"
  # from/to are epoch MS (not ISO); panelId from the dashboard JSON
  curl -s -u "$AUTH" -o /tmp/panel.png \
    "http://localhost:3001/render/d-solo/<uid>/x?panelId=7&from=<ms>&to=<ms>&width=1000&height=500"
  ```

- **Grafana MCP** (`mcp/grafana`): typed API tools for Claude Code — see `mcp/README.md`.

## CI

`.github/workflows/test.yml` runs Tier 1 on every push/PR. Tiers 2 and 3 are
local-only — they require the Grafana/InfluxDB containers and the dev-status
launchd job running on the Mac mini.
