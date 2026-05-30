# CLAUDE.md — observability-config

Infrastructure-as-code for the home observability stack on `tommys-mac-mini`:
**Grafana** (dashboards) + **InfluxDB** (time-series), both OrbStack containers,
file-provisioned. This file holds context that isn't derivable from the code.

Repo lives at `/Volumes/dev/observability/`; the live config **is** the working
tree (Grafana bind-mounts `grafana/provisioning`). GitHub: private
`tommyroar/observability-config`.

## Layout

- `grafana/` — Grafana 11.3 OSS. `docker-compose.yml` + `provisioning/`
  (dashboard JSON, datasource YAML, provider). Subdirs: `dev-status/` (host
  collector), `playwright/` (e2e + visual suite), `tests/` (pytest), `mcp/`
  (Grafana MCP setup).
- `influxdb/` — InfluxDB 2.7. `docker-compose.yml` + `backup.sh`.

## Hard rules

- **Dashboards are code.** `provider.yml` sets `allowUiUpdates: false`, so the
  Grafana UI is read-only for provisioned dashboards. Edit the JSON in
  `grafana/provisioning/dashboards/`, never the UI. The provider reloads files
  every 30s; datasource or `docker-compose` changes need a container recreate
  (`docker compose up -d` from the relevant subdir).
- **Secrets** live in per-dir `.env` (gitignored, chmod 600); `.env.example`
  templates are committed. Never commit a real `.env`.

## dev-status collector

`grafana/dev-status/server.py` serves live dev-deployment + tailscale-serve
status as JSON on `:8077`, feeding the status-page dashboard's Infinity panels
(Grafana reaches it at `http://host.docker.internal:8077`).

- Runs **on the host** (needs `tailscale serve status` + host TCP probes) under
  launchd `com.tommy.dev-status`.
- Runs from the **internal disk**, not here — macOS TCC blocks launchd from
  reading `/Volumes` (exit 78 / "Operation not permitted"). The repo file is the
  source of truth; **edit it here, then run `grafana/dev-status/deploy.sh`** to
  copy it to `~/.local/share/dev-status/server.py` and restart the job.

## Testing

`grafana/run-tests.sh [unit|integration|e2e|all]` — three-tier pyramid (details
in `grafana/TESTING.md`):

- **unit/static** — hermetic; dashboard JSON validity, datasource refs resolve,
  provider lock, collector pure-functions, `backup.sh` shape. Runs in CI.
- **integration** — live stack (self-skips if down); provisioning matches files,
  datasource/Influx health, buckets exist, renderer returns PNG, collector contract.
- **e2e** — Playwright browser render + visual-regression baselines.

CI (`.github/workflows/test.yml`) runs only the hermetic tier on push/PR;
integration + e2e are local-only (need the running stack on this Mac).

## Troubleshooting tools

- **grafana-image-renderer** (`renderer` compose service): deterministic panel
  PNGs — `GET /render/d-solo/<uid>/x?panelId=N&from=<ms>&to=<ms>&width=&height=`.
  from/to are epoch **milliseconds**, not ISO.
- **Grafana MCP** (`mcp/grafana`): typed Grafana API tools for Claude Code — see
  `grafana/mcp/README.md`. Live config at workspace `/Volumes/dev/.mcp.json`;
  token in `~/.zshenv` (`GRAFANA_SA_TOKEN`).

## Gotchas

- **`/Volumes` + launchd = TCC block (exit 78).** Any launchd job that must read
  or write `/Volumes` fails. That's why dev-status runs from the internal disk;
  it's also why the InfluxDB backup LaunchAgent is currently non-functional.
- **Grafana → host** services use `host.docker.internal` (OrbStack).
- **InfluxDB** binds only named volumes (`influxdb_data`, `influxdb_config`), not
  its host config dir — editing `influxdb/` never disturbs the running container.
- Container images are pinned (Grafana 11.3.0, Influx 2.7); the renderer is
  pinned by digest.
