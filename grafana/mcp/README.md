# Grafana MCP server (for Claude Code)

Gives Claude Code typed tools over the Grafana API (fetch dashboards, run
datasource queries, check datasource health, search, alerting, etc.) — the
API-poking half of dashboard troubleshooting, without curl. It does **not**
render panels; for visual checks use the image renderer + the Playwright visual
tier (see `../TESTING.md`).

Official server: `mcp/grafana` (Grafana Labs). Runs as a stdio subprocess of
Claude Code via Docker.

## Setup (already wired on this machine)

1. **Service account + token** in Grafana (`Admin` role so all 56 tools work):

   ```sh
   source /Volumes/dev/observability/grafana/.env
   AUTH="$GRAFANA_ADMIN_USER:$GRAFANA_ADMIN_PASSWORD"
   said=$(curl -s -u "$AUTH" -X POST localhost:3001/api/serviceaccounts \
     -H 'Content-Type: application/json' \
     -d '{"name":"claude-mcp","role":"Admin"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
   curl -s -u "$AUTH" -X POST "localhost:3001/api/serviceaccounts/$said/tokens" \
     -H 'Content-Type: application/json' -d '{"name":"claude-mcp-token"}'
   # -> copy the "key" value
   ```

2. **Token in the environment** (kept out of any committed file), in `~/.zshenv`:

   ```sh
   export GRAFANA_SA_TOKEN=glsa_xxx...
   ```

3. **Claude Code config** at the workspace root `/Volumes/dev/.mcp.json`
   (untracked — references the token by env var, no secret in the file):

   ```json
   {
     "mcpServers": {
       "grafana": {
         "command": "docker",
         "args": ["run","--rm","-i","-e","GRAFANA_URL","-e","GRAFANA_SERVICE_ACCOUNT_TOKEN","mcp/grafana","-t","stdio"],
         "env": {
           "GRAFANA_URL": "http://host.docker.internal:3001",
           "GRAFANA_SERVICE_ACCOUNT_TOKEN": "${GRAFANA_SA_TOKEN}"
         }
       }
     }
   }
   ```

   `host.docker.internal:3001` is how the MCP container reaches Grafana on the host.

4. **Restart Claude Code** in `/Volumes/dev` and approve the `grafana` MCP server
   when prompted (`/mcp` lists it).

## Smoke-test without Claude

```sh
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"x","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
| docker run -i --rm -e GRAFANA_URL=http://host.docker.internal:3001 \
    -e GRAFANA_SERVICE_ACCOUNT_TOKEN="$GRAFANA_SA_TOKEN" mcp/grafana -t stdio
# expect a tools/list response with ~56 tools
```

## Revoke

```sh
source /Volumes/dev/observability/grafana/.env; AUTH="$GRAFANA_ADMIN_USER:$GRAFANA_ADMIN_PASSWORD"
# find the SA, then: DELETE /api/serviceaccounts/<id>
curl -s -u "$AUTH" "localhost:3001/api/serviceaccounts/search?query=claude-mcp"
```
