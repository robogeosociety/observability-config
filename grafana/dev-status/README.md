# dev-status

Feeds the **Dev Deployments & Tailscale Serves** section of the Grafana
*Project Status* dashboard (`status-page` uid).

## What it does

`server.py` is a tiny stdlib HTTP server (port **8077**) that, on each request,
regenerates a live snapshot of:

- every configured `tailscale serve` route (port/path → backend), with each
  backend's current **UP/DOWN** + TCP connect latency, and
- every Vite port-registry entry (`~/.claude/vite-ports.json`) whose port isn't
  already exposed by a serve route.

Output: `GET http://localhost:8077/dev-status.json`

## Why it runs the way it does

- **Runs on the host (launchd), not a container** — it needs `tailscale serve
  status` and host-local TCP probes that a container can't see.
- **Runs from the internal disk**, not here on `/Volumes`. macOS TCC blocks
  launchd-spawned processes from reading the external `/Volumes` disk
  (`Operation not permitted`, exit 78). So `server.py` here is the **source of
  truth** (version-controlled), and `~/.local/share/dev-status/server.py` is the
  **deployed copy** launchd actually runs. **Edit `server.py` here, then run
  `./deploy.sh`** to copy it over and restart the job.
- **Grafana reaches it via `host.docker.internal:8077`** — the `grafana`
  OrbStack container fetches the JSON through the existing `uptime_status`
  Infinity datasource on the dashboard's 1m refresh.

## Pieces

| Thing | Path |
|---|---|
| Server (source, in repo) | `grafana/dev-status/server.py` |
| Server (deployed, runtime) | `~/.local/share/dev-status/server.py` |
| Deploy script | `grafana/dev-status/deploy.sh` |
| launchd job | `~/Library/LaunchAgents/com.tommy.dev-status.plist` |
| Log | `~/Library/Logs/com.tommy.dev-status.log` |
| Dashboard panels | `provisioning/dashboards/status-page.json` (ids 11–13) |

## Operate

```sh
# after editing server.py: copy to the runtime location + restart
./deploy.sh

# stop / start
launchctl unload ~/Library/LaunchAgents/com.tommy.dev-status.plist
launchctl load   ~/Library/LaunchAgents/com.tommy.dev-status.plist

# check it
launchctl list | grep dev-status          # col 1 = pid, col 2 = last exit
curl -s localhost:8077/dev-status.json | python3 -m json.tool
```
