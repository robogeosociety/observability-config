# ask-dash

Read-only, natural-language Q&A over the local Grafana/InfluxDB stack — exposed
two ways:

- **Discord** — a gateway bot with an `/ask` slash command.
- **Claude CLI** — an MCP server, so any `claude` session (interactive or a
  scripted `claude -p` weekly job) can query the stack natively.

Both share one core: read-only data tools in `ask_dash/tools.py`. Nothing here can
write or delete — the InfluxDB token is read-scoped across all buckets and the
Grafana token is a Viewer. "Read-only" is enforced by the credentials and the tool
surface, not by the prompt.

```
                 ┌───────────── ask_dash/tools.py ─────────────┐
                 │  list_buckets · list_measurements · …       │
   Discord  ─────┤  query_influx (read-only Flux)              ├──▶ InfluxDB :8086 (read token)
   Claude CLI ───┤  search_dashboards · get_dashboard_queries  ├──▶ Grafana  :3000 (viewer token)
                 └─────────────────────────────────────────────┘
   Discord bot + CLI drive their own Claude loop (agent.py); the MCP path lets the
   caller's Claude CLI session be the loop.
```

## Layout

- `ask_dash/tools.py` — the read-only data core + shared tool registry.
- `ask_dash/agent.py` — Claude tool-use loop (Discord bot + CLI).
- `ask_dash/discord_bot.py` — gateway bot, `/ask` (the long-running container).
- `ask_dash/mcp_server.py` — MCP stdio server for Claude CLI sessions.
- `ask_dash/cli.py` — `ask-dash "question"` for scripted summaries.

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
