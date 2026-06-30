# discord-mini-mem

A live **Mac mini memory treemap** as a single self-editing message in the #ops
Discord channel — the colored-square-emoji sibling of the Grafana `mac-system`
treemap, reading the **same data**.

## How it works

- Pulls `:8077/processes.json` from the **dev-status collector** (the host service that
  already feeds the Grafana process treemap), reached from the container over the
  OrbStack host bridge (`http://host.docker.internal:8077`). A Linux container can't read
  macOS host RAM directly — dev-status does the sampling, this just renders it.
- Renders the top processes by RSS as a squarified **colored-square emoji** treemap +
  legend. Emoji (not ANSI) because Discord only colors `ansi` code blocks on desktop —
  emoji render in color on **mobile** too.
- Edits **one** Discord message every `INTERVAL` seconds (default 60). Edits are silent
  (no push), so it's a true live dashboard. The message id is persisted under `/state`
  so a restart re-uses it instead of posting a new message.

## Deploy

Container via docker-compose + a Nomad `ctl-*` supervisor (the repo's container pattern):

```sh
cp discord-mini-mem/.env.example discord-mini-mem/.env   # add DISCORD_BOT_TOKEN, chmod 600
# Seed the existing live message id (cutover from the launchd daemon), or let it post fresh:
mkdir -p ~/.local/share/discord-mini-mem
cp ~/.local/share/mini-mem-treemap/state.json ~/.local/share/discord-mini-mem/ 2>/dev/null || true
docker compose -f discord-mini-mem/docker-compose.yml up -d --build
nomad job run orbstack/nomad/ctl-discord-mini-mem.hcl     # supervise it (orbstack ns)
```

Then retire the interim host launchd daemon (`com.tommy.mini-mem-treemap`).

## Tests

`test_minimem.py` is hermetic (no network) — pins the header math (used = total − idle),
proc-only tiles, rename map, legend ordering, and the 7-tile cap. Runs in CI under
`discord-mini-mem`.
