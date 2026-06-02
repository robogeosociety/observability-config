# CLAUDE.md — observability-config

Infrastructure-as-code for the home observability stack on `tommys-mac-mini`:
**Grafana** (dashboards) + **InfluxDB** (time-series), both OrbStack containers,
file-provisioned. This file holds context that isn't derivable from the code.

Repo lives at `/Volumes/dev/observability/`; the live config **is** the working
tree. GitHub: private `tommyroar/observability-config`.

**Grafana provisioning is no longer bind-mounted from `/Volumes`.** After the
2026-05-31 external-disk unmount broke containers, Grafana now mounts an
**internal-disk copy** at `~/.observability/grafana/provisioning`. The repo's
`grafana/provisioning/` stays the version-controlled source of truth — edit it
here, then run **`grafana/deploy-provisioning.sh`** to rsync repo → internal and
reload Grafana. (Same internal-disk pattern as the dev-status collector and the
Telegraf config.)

## Layout

- `grafana/` — Grafana 11.3 OSS. `docker-compose.yml` + `provisioning/`
  (dashboard JSON, datasource YAML, provider). Subdirs: `dev-status/` (host
  collector), `playwright/` (e2e + visual suite), `tests/` (pytest), `mcp/`
  (Grafana MCP setup).
- `influxdb/` — InfluxDB 2.7. `docker-compose.yml`, `backup.sh` (+ `r2_upload.py`).
- `telegraf/` — Mac host metrics collector. Telegraf runs **natively** via
  `brew services` (not a container — a container would report the OrbStack VM,
  not macOS), writing cpu/mem/swap/disk/diskio/net/system to the `system` bucket
  every 15s for the `mac-system` dashboard. Config is `telegraf.conf`; `deploy.sh`
  bakes the token from `.env` into Homebrew's config path and restarts the service.
- `campsites/` — R2→InfluxDB ingest for the `campsites` bucket (launchd, daily).

## Backups

`influxdb/backup.sh` dumps Influx to a `.tar.gz` on the 2TB disk, emits a
heartbeat to the `ops` bucket (`backup` measurement: `success`/`bytes`/`duration_s`,
tag `target=local|r2`), and — when `R2_*` creds are in `.env` — uploads offsite to
the Cloudflare R2 `influxdb-backups` bucket via `r2_upload.py`. The **Backups**
Grafana dashboard (`backups.json`, datasource `ops`) shows last-success age, result,
size, and history per target. R2 is **pending a scoped R2 token** in `.env`
(bucket exists; until then it's local-only and the dashboard shows R2 "NOT CONFIGURED").
The token + bucket are defined as code in `terraform/` (Cloudflare provider v5):
`terraform apply` (with `TF_VAR_cloudflare_api_token`) mints the scoped R2 token and
outputs the S3 creds for `influxdb/.env` — see `terraform/README.md`.

## Alerting

File-provisioned Grafana alerting lives in `grafana/provisioning/alerting/`
(contact points, root notification policy, rules) — deployed by the same
`deploy-provisioning.sh` sync as dashboards. The rules, all routed to the same
Discord contact point:

- **InfluxDB availability** (`influxdb-availability.yml`, issue #11) — counts a
  cheap telegraf series (`cpu`/`usage_idle`/`cpu-total`) in the `system` bucket
  over 5m, pages below 1. Both `noDataState` and `execErrState` are `Alerting`,
  so a telegraf gap (InfluxDB up, no writes) **and** a query error (InfluxDB
  down/unreachable) surface within minutes instead of going silent.
- **Backup stale** (`backup-stale.yml`) — no successful `target=local` backup
  heartbeat in the `ops` bucket for 26h (`noDataState=Alerting`).
- **Disk filling** (`disk-space.yml`) — `system` disk `used_percent` for
  `/Volumes/dev` over 85%.
- **Container OOM-killed** (`container-oom.yml`) — telegraf `docker_container_status`
  `oomkilled==true` (the runaway-container failure mode the mem_limits bound).
- **Collector freshness** (`collector-freshness.yml`) — per-bucket "no writes in
  15m" for the continuously-writing collectors (`transit_tracker`,
  `home_assistant`, `tempest_archive`). Scoped to proven-continuous buckets only;
  dormant/event-driven/usage-driven buckets (mountain, campsites, zigbee_archive,
  claude_code) are deliberately excluded so the rules never fire on deploy.

The three secondary rules set `execErrState=OK` (plus `noDataState=OK` for the
latter two) so an InfluxDB outage pages **once** (via the availability rule), not
four times.

Notification is **Discord** via Grafana's native `discord` contact point, which
posts a color-coded embed (firing red / resolved green) with the alert summary,
labels, and runbook annotation. It reads `$__env{DISCORD_WEBHOOK_URL}` (a channel
webhook — the secret), which `grafana/docker-compose.yml` passes through from
`grafana/.env`; the URL is never committed. Stand-up: create the webhook in
Discord (Server Settings → Integrations → Webhooks), put `DISCORD_WEBHOOK_URL` in
`.env`, then `docker compose up -d` to recreate grafana (provisioning reload
alone isn't enough — `$__env{}` is read at container start). Resolve messages are
on (Discord embeds distinguish firing from resolved). `tests/test_alerting_static.py`
guards the config (valid YAML, no literal secret, alert-on-missing-data, routes
to a defined contact point).

## Hard rules

- **Dashboards are code.** `provider.yml` sets `allowUiUpdates: false`, so the
  Grafana UI is read-only for provisioned dashboards. Edit the JSON in
  `grafana/provisioning/dashboards/`, never the UI. The provider reloads files
  every 30s; datasource or `docker-compose` changes need a container recreate
  (`docker compose up -d` from the relevant subdir).
- **Dashboard intent is code too.** Every dashboard has an entry in
  `grafana/dashboards.index.yaml` capturing its purpose, design rationale, and
  TODO backlog — the "why" the JSON can't hold. Read it before changing a
  dashboard; record follow-ups there instead of losing them. Add or remove a
  dashboard JSON and update the index in the same change: `tests/test_dashboard_index.py`
  fails if an entry is missing, orphaned, or drifts from the JSON's
  title/file/datasources. (The index lives in `grafana/`, not
  `provisioning/dashboards/`, because Grafana's provider would try to parse a
  stray YAML there as a provider config.)
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

- **`/Volumes` + launchd = TCC block.** Any launchd job that must read or write
  `/Volumes` fails ("Operation not permitted"). That's why dev-status runs from
  the internal disk. The InfluxDB backup keeps its data on `/Volumes` and instead
  requires **Full Disk Access granted to `/bin/zsh`** (System Settings → Privacy &
  Security → Full Disk Access); `influxdb/backup.sh` is structured so that single
  grant suffices (zsh does all `/Volumes` access; the dump is streamed out of the
  container via a redirect, not `docker cp`). Its log is on the internal disk
  (`~/Library/Logs/influxdb-backup.log`) since launchd can't open a `/Volumes` log.
- **Grafana → host** services use `host.docker.internal` (OrbStack).
- **InfluxDB** binds only named volumes (`influxdb_data`, `influxdb_config`), not
  its host config dir — editing `influxdb/` never disturbs the running container.
- Container images are pinned (Grafana 11.3.0, Influx 2.7); the renderer is
  pinned by digest.
