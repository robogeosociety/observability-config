# discord-claude-heatmap

A live **Claude token-usage heatmap** as a self-editing message in #ops — the dynamic
replacement for the cumulative milestone notifier (`discord-claude-tokens`, retired).

## How it works

- Queries InfluxDB `claude_code` bucket (`tokens` measurement, `output_tokens` field) —
  the same series the claude-usage dashboard reads — bucketed hourly over the last
  `HEATMAP_HOURS` (default 12), grouped by project.
- Renders a GitHub-contributions-style **heatmap**: rows = top-8 projects, columns = the
  hourly buckets, color = token intensity (⬛ idle → 🟦🟩🟨🟧 → 🟥 hot), with per-row
  totals + a scale. Emoji render in color on mobile (Discord ANSI does not).
- Edits **one** #ops message every `INTERVAL` seconds (default 60). Message id persists
  under `/state`.

## Why a heatmap, not a counter

The old notifier posted cumulative milestones ("crossed Xmm tokens") — a number that only
goes up. The heatmap shows *where and when* tokens are being spent, live, so a runaway
project lights up red on a tick instead of being buried in a running total.

## Deploy

```sh
cp discord-claude-heatmap/.env.example discord-claude-heatmap/.env   # INFLUX_READ_TOKEN (ask-dash/.env) + DISCORD_BOT_TOKEN (discord-ops/.env), chmod 600
mkdir -p ~/.local/share/discord-claude-heatmap
CLAUDEHEAT_STATE_DIR=$HOME/.local/share/discord-claude-heatmap \
  docker compose -f discord-claude-heatmap/docker-compose.yml up -d --build
nomad job run orbstack/nomad/ctl-discord-claude-heatmap.hcl
# retire the cumulative notifier:
nomad job stop -purge discord-claude-tokens
```

## Tests

`test_heatmap.py` is hermetic (no network) — pins the CSV→matrix parse (empty cells → 0,
bad rows dropped), the monotonic intensity scale, header/row ordering, the top-N row cap,
and the empty-window case. Runs in CI under `discord-claude-heatmap`.
