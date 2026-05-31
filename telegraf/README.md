# Telegraf — Mac host system metrics

Collects CPU, load, memory, swap, disk usage, disk I/O, and network throughput
from `tommys-mac-mini` into the InfluxDB `system` bucket, feeding the
**Mac System** Grafana dashboard.

```
telegraf (brew service, native host)  ──15s──▶  InfluxDB `system` bucket  ──▶  Mac System dashboard
  inputs: cpu mem swap disk diskio net system
```

Telegraf runs **natively via Homebrew**, not in a container — a container would
report the OrbStack Linux VM's metrics, not macOS.

## Files

| File | Role |
|------|------|
| `telegraf.conf` | canonical config (source of truth); `${INFLUX_TOKEN}` placeholder |
| `deploy.sh` | bake token from `.env` → `$(brew --prefix)/etc/telegraf.conf`, validate, restart service |
| `.env` | `INFLUX_TOKEN` (write-only, scoped to `system`); gitignored, chmod 600 |

Dashboard: `grafana/provisioning/dashboards/mac-system.json` · datasource
`system` in `grafana/provisioning/datasources/influxdb.yml`.

## Setup

1. **Install**: `brew install telegraf`
2. **Token**: a write-only token scoped to the `system` bucket already exists in
   `.env`. To re-mint:
   ```sh
   source ../influxdb/.env
   BID=$(docker exec influxdb influx bucket list --org home --token "$INFLUX_ADMIN_TOKEN" --hide-headers --name system | awk '{print $1}')
   docker exec influxdb influx auth create --org home --token "$INFLUX_ADMIN_TOKEN" \
     --write-bucket "$BID" --description "telegraf system metrics write"
   ```
3. **Deploy + start**: `./deploy.sh` (substitutes the token, validates with
   `telegraf --test`, runs `brew services restart telegraf`).

## Edit / redeploy

Edit `telegraf.conf` here, then `./deploy.sh`. Check status with
`brew services info telegraf`; logs via `brew services` (stderr to
`$(brew --prefix)/var/log/telegraf.log` if configured, else Console).

## Notes

- No `/Volumes` TCC issue: the deployed config lives on the internal disk, and
  the disk input reads filesystem **stats** (statfs), not file contents.
- `inputs.disk` skips macOS synthetic filesystems (`devfs`, `autofs`, …) so only
  real volumes show — including the 2TB external `/Volumes/dev`.
- CPU is overall-only (`percpu = false`); add `percpu = true` for per-core.
