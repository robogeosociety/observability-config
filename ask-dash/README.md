# ask-dash

Read-only, natural-language Q&A over the local Grafana/InfluxDB stack — exposed
three ways:

- **Discord** — a gateway bot with an `/ask` slash command.
- **Claude CLI** — an MCP server, so any `claude` session (interactive or a
  scripted `claude -p` weekly job) can query the stack natively.
- **Local CLI utility** — `ask-dash` on the Mac mini's PATH: ask questions, and
  `ask-dash render` a Grafana panel to a PNG and open it (so you can glance at a
  panel without browsing the Grafana UI over Tailscale).

Both share one core: read-only data tools in `ask_dash/tools.py`. Nothing here can
write or delete — the InfluxDB token is read-scoped across all buckets and the
Grafana token is a Viewer. "Read-only" is enforced by the credentials and the tool
surface, not by the prompt.

```
                 ┌───────────── ask_dash/tools.py ─────────────┐
                 │  list_buckets · list_measurements · …       │
   Discord  ─────┤  query_influx (read-only Flux)              ├──▶ InfluxDB :8086 (read token)
   Claude CLI ───┤  search_dashboards · get_dashboard_queries  ├──▶ Grafana  :3000 (viewer token)
   Local CLI  ───┤  list_panels · render_panel (PNG)           ├──▶ image renderer (PNG)
                 └─────────────────────────────────────────────┘
   Discord bot + CLI drive their own Claude loop (agent.py); the MCP path lets the
   caller's Claude CLI session be the loop. `ask-dash render` skips the loop and
   pulls a panel PNG straight from the renderer.
```

## Layout

- `ask_dash/tools.py` — the read-only data core + shared tool registry.
- `ask_dash/agent.py` — Claude tool-use loop (Discord bot + CLI).
- `ask_dash/discord_bot.py` — gateway bot, `/ask` (the long-running container).
- `ask_dash/mcp_server.py` — MCP stdio server for Claude CLI sessions.
- `ask_dash/cli.py` — the `ask-dash` CLI: `ask` / `dashboards` / `panels` / `render`.

## Setup

```sh
cp .env.example .env && chmod 600 .env      # then fill it in
uv sync
```

Secrets in `.env` (gitignored): a read-only Influx token (mint command in
`.env.example`), the existing Grafana viewer token, an `ANTHROPIC_API_KEY`, and —
for the bot — a Discord bot token + your guild/user ids.

## Run

**Discord bot** (long-running OrbStack container):
```sh
docker compose up -d --build       # joins influxdb_default; reaches influxdb + grafana by name
```
Create the app/bot at <https://discord.com/developers>, invite it with the
`applications.commands` scope, set `DISCORD_GUILD_ID`/`DISCORD_ALLOWED_USER_IDS`,
then `/ask how many sites are open at Goodell Creek this week?`.

**Claude CLI (MCP)** — add to a project's `.mcp.json`:
```json
{
  "mcpServers": {
    "ask-dash": {
      "command": "uv",
      "args": ["run", "--directory", "/Volumes/dev/observability/ask-dash",
               "python", "-m", "ask_dash.mcp_server"]
    }
  }
}
```
Now a session can call `list_buckets`, `query_influx`, `get_dashboard_queries`, etc.

**Scripted weekly summary** — either a `claude -p` run against the MCP, or the CLI:
```sh
uv run ask-dash "Summarize transit_tracker trends over the last 7 days; call out anomalies."
```
Wrap that in a launchd/cron job to post a weekly digest.

**Local CLI utility** (on the Mac mini) — install once on PATH as an editable tool,
so it always runs the live tree and reads this dir's `.env`:
```sh
uv tool install --editable /Volumes/dev/observability/ask-dash
```
Then, from anywhere:
```sh
ask-dash "how many sites are open at Goodell Creek this week?"   # ask the agent
ask-dash dashboards backup        # find a dashboard uid by title
ask-dash panels backups           # list that dashboard's panel ids
ask-dash render backups 2         # render panel 2 → PNG, opens it (last 6h)
ask-dash render backups 2 --from 7d --width 1200 --no-open --out /tmp/p.png
```
`render` keeps Grafana headless — it talks to the image renderer with the same
read-only Viewer token, no Tailscale UI needed. `--from`/`--to` accept `now`, a
shorthand like `6h`/`7d`/`now-30m`, or epoch ms. Headless Chromium is slow
(~5–7s/render, slower under host load); the timeout is `RENDER_TIMEOUT_S` (45s).

## Test

```sh
uv run pytest            # hermetic registry/dispatch tests
```
Live data access is proven by a quick smoke-test:
```sh
uv run python -c "from ask_dash import tools; print(tools.list_buckets())"
```

## Notes

- The bot runs as an OrbStack container (not Nomad): a gateway bot is an outbound
  socket, so no public endpoint/tunnel is needed, and it's a long-running service.
- Guard rails: `query_influx` is capped (`MAX_ROWS`) and timed out (`QUERY_TIMEOUT_S`)
  so a vague question can't trigger a full-history scan.
