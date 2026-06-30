# proc-mem-collector

Samples the Mac mini's **per-process memory** into InfluxDB so it can be visualized —
first as the live #ops Discord memory treemap (`discord-mini-mem`), and later as a
Grafana panel. A Linux/OrbStack container can't read macOS host processes, so the host
samples and writes to InfluxDB; consumers just query.

## What it writes

Every minute (Nomad periodic), to the `ops` bucket:

- `proc_mem,name=<process> rss_bytes=<int>i` — the **top-N processes by RSS** (default
  15, `PROC_MEM_TOP_N`). Aggregated by command basename; noisy multi-process apps are
  rolled up (e.g. all `OrbStack Helper` → `OrbStack`).
- `mem_summary used_bytes=,total_bytes=,wired_bytes=,comp_bytes=,free_bytes=` — the totals
  for the header line.

Cardinality is **bounded to top-N names + one summary series** on purpose — an unfiltered
`procstat` would create a series per process and blow up InfluxDB.

## Run

```sh
# dry run — print line protocol, write nothing
grafana/proc-mem-collector/run.sh --dry-run

# deploy + run via Nomad (existing tooling)
nomad job run grafana/proc-mem-collector/nomad/proc-mem-collector.hcl
nomad job periodic force proc-mem-collector
```

`run.sh` reads `INFLUX_OPS_TOKEN` from `influxdb/.env` at runtime (host Nomad agent has
Full Disk Access), same as `runtime-versions` — no secret in the Nomad spec.

## Tests

`test_proc_mem.py` is hermetic (no subprocess, no InfluxDB) and runs in CI under
`grafana/proc-mem-collector`. It pins the parsing, the KiB→bytes conversion, the summary
math, the top-N cardinality bound, and tag escaping.
