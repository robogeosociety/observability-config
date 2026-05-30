# observability-config

Infrastructure-as-code for the home observability stack running on
`tommys-mac-mini` (Tommy's Mac mini). Two OrbStack containers, file-provisioned:

- **`grafana/`** — Grafana 11.3 (OSS). Dashboards + datasources are
  file-provisioned (no clicking in the UI as source of truth). Also contains
  `dev-status/`, a small host collector that feeds the *Dev Deployments &
  Tailscale Serves* section of the status page.
- **`influxdb/`** — InfluxDB 2.7. Time-series store + daily backup job.

## Layout

The repo lives at `/Volumes/dev/observability/` and the live config *is* the
repo working tree — Grafana's `./provisioning` bind mount, the InfluxDB backup
LaunchAgent, and the test `.env` paths all point here. Edit in place, commit,
push. (Run `docker compose up -d` from `grafana/` / `influxdb/` after changes
that affect the containers.)

## Secrets

Secrets live in per-dir `.env` files (gitignored). Templates are committed as
`.env.example`. Copy and fill them in before `docker compose up`.

## Versioning replaces the old R2 backup

Config used to be tar-snapshotted to Cloudflare R2 by a daily LaunchAgent. That
job was never fully wired (token lacked access; agent wasn't loaded) and has been
removed in favor of this repo — git gives real diffs, history, and an offsite
copy on GitHub in one move.
