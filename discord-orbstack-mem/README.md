# discord-orbstack-mem

A live **OrbStack per-container memory treemap** as a self-editing message in #ops —
the container sibling of `discord-mini-mem`, and the Discord twin of the
"Memory map — container usage (btop-style)" panel on the OrbStack Grafana dashboard.

## How it works

- Queries InfluxDB `system` bucket for the latest `docker_container_mem.usage` per
  container (Telegraf's docker input — the **same data** the Grafana container treemap
  reads), via a read-all token. Reaches InfluxDB by container name on the shared
  `influxdb_default` network.
- Renders the top-7 containers by memory as a squarified **colored-square emoji** treemap
  + legend (emoji render in color on mobile; Discord ANSI does not).
- Edits **one** Discord message every `INTERVAL` seconds (default 60). The message id
  persists under `/state` (its own dir, separate from discord-mini-mem's).

## Deploy

```sh
cp discord-orbstack-mem/.env.example discord-orbstack-mem/.env   # add INFLUX_READ_TOKEN + DISCORD_BOT_TOKEN (both in ask-dash/.env), chmod 600
mkdir -p ~/.local/share/discord-orbstack-mem
ORBMEM_STATE_DIR=$HOME/.local/share/discord-orbstack-mem \
  docker compose -f discord-orbstack-mem/docker-compose.yml up -d --build
nomad job run orbstack/nomad/ctl-discord-orbstack-mem.hcl
```

## Tests

`test_orbmem.py` is hermetic (no network) — pins the CSV→MiB parse (sorted, bad rows
dropped), the header count/total, and the 7-tile cap. Runs in CI under
`discord-orbstack-mem`.
