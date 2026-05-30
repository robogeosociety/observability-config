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
- `backup.sh`: shell syntax + presence of safety flags, backup/cp/prune steps,
  and that the token comes from the env (never hardcoded).

**Tier 2 — integration (needs the live stack; tests self-skip if a service is down)**
- Collector: spawns a real instance on an ephemeral port, checks the HTTP/JSON contract.
- Grafana: each dashboard is provisioned (read-only) and matches its file; every
  datasource exists; InfluxDB datasource health is OK.
- InfluxDB: healthy, the org exists, and every bucket a datasource points at exists.

**Tier 3 — e2e (Playwright, real browser)**
- Tempest dashboard renders without panel errors / "No data".
- Status page renders, the dev-status collector serves its contract, and the
  Routes & backends table is populated (the `realitycapture` row is visible).

## CI

`.github/workflows/test.yml` runs Tier 1 on every push/PR. Tiers 2 and 3 are
local-only — they require the Grafana/InfluxDB containers and the dev-status
launchd job running on the Mac mini.
