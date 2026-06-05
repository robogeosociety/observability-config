# runtime-versions collector

Writes the **current vs latest** version of each language runtime to the InfluxDB
`ops` bucket so the **Runtime Versions** dashboard (`dev/runtime-versions.json`) can
show what's installed and whether anything is behind.

| Runtime | Manager | Current | Latest source |
| --- | --- | --- | --- |
| node | volta | `volta list node` (default) | newest in the active major, `nodejs.org/dist/index.json` |
| python | uv | uv-managed default (`python3 -V`) | newest downloadable patch in the series (`uv python list`) |
| rust | rustup | active stable toolchain | `rustup check` |

**Schema** — measurement `toolchain_version`, tags `runtime` / `manager` / `host`,
fields `current` (string), `latest` (string), `update_available` (int 0/1). A
`collector,source=runtime-versions` heartbeat is emitted alongside.

**Scheduling** — Nomad periodic job `nomad/runtime-versions.hcl` (daily, 08:30
America/Los_Angeles). This follows the workspace **batch-on-Nomad** standard rather
than the older launchd collectors in this repo: it runs under the host Nomad agent
and reads `/Volumes` (the `INFLUX_OPS_TOKEN` in `influxdb/.env`) directly via the
agent's Full Disk Access — no internal-disk `deploy.sh` step.

Apply (maintainer, on merge):
```sh
nomad job run grafana/runtime-versions/nomad/runtime-versions.hcl
nomad job periodic force runtime-versions   # seed a first data point
```

This complements the weekly **notify-only** `sys-update` job (in `/Volumes/dev/scripts`)
which pings Discord when something is behind; this one is the steady metric feed.
