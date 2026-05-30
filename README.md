# observability-config

Infrastructure-as-code for the home observability stack running on
`tommys-mac-mini` (Tommy's Mac mini). Two OrbStack containers, file-provisioned:

- **`grafana/`** — Grafana 11.3 (OSS). Dashboards + datasources are
  file-provisioned (no clicking in the UI as source of truth). Also contains
  `dev-status/`, a small host collector that feeds the *Dev Deployments &
  Tailscale Serves* section of the status page.
- **`influxdb/`** — InfluxDB 2.7. Time-series store + daily backup job.

## This repo's layout is unusual on purpose

The git repo is rooted at the `/Volumes/dev` workspace but tracks **only**
`grafana/` and `influxdb/` (see `.gitignore`). The two config dirs can't be
moved into a dedicated folder without breaking the live Docker bind mounts,
launchd jobs, and the many absolute `/Volumes/dev/...` path references in the
configs — so the repo wraps them in place. On GitHub it shows up clean (just
these two dirs). To undo entirely: `rm -rf /Volumes/dev/.git`.

## Secrets

Secrets live in per-dir `.env` files (gitignored). Templates are committed as
`.env.example`. Copy and fill them in before `docker compose up`.

## Versioning replaces the old R2 backup

Config used to be tar-snapshotted to Cloudflare R2 by a daily LaunchAgent. That
job was never fully wired (token lacked access; agent wasn't loaded) and has been
removed in favor of this repo — git gives real diffs, history, and an offsite
copy on GitHub in one move.
