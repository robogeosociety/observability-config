# discord-collectors — requirements

One section per collector: **requirement → data source → trigger → output**.
These are the contracts the scripts implement; the README covers how to run/deploy.

---

## GitHub activity → Discord — RETIRED (use the native integration)

The `github_discord.py` poller was removed. GitHub's **native Discord integration**
(per-repo webhook) covers the same PR/issue activity — but real-time instead of a
30-min poll, and across **private** repos too (the old poller read the *public*
`/users/tommyroar/events` feed, blind to private-repo activity). CI/deploy alarms
still come from Grafana + the watcher, not here.

## 1. Transit service alerts → Discord (`transit_discord.py`)

- **Requirement:** notify when a watched bus/train route has an active service
  alert (detour, reduced service, etc.), and clear it when resolved.
- **Data source:** OneBusAway, polled **per route**:
  `GET /api/where/trips-for-route/{routeId}.json?key=KEY&includeStatus=false&includeSchedule=false`,
  reading alerts from `data.references.situations[]`. (The old
  `situations-for-agency/{agency}` call is **not a real OBA method** — it 404s.)
  Watched routes: `1_100252` (Route 7), `1_100228` (Route 8), `1_100113`
  (Route 14), `1_102574` (Route 554), `40_100479` (1 Line), `40_2LINE` (2 Line).
  Requests spaced ~1.5s (OBA 429s if hammered); situations deduped by `id`
  across the sweep. OBA key from `OBA_API_KEY` (run-transit.sh sources it from
  `transit_tracker/.local/service.yaml`).
- **Trigger:** Nomad periodic, every 5 min (`nomad/discord-transit.hcl`), via
  `run-transit.sh`. New/cleared diff against
  `~/.local/share/transit-discord/state.json`.
- **Output:** Discord embed per new situation — colour by OBA camelCase
  severity (`severe`/`verySevere` → red, `moderate` → orange, else yellow),
  with summary/description/affected-routes/severity/reason/active-window; a
  green "Cleared" embed when a tracked situation disappears.

## 2. Weekly ops digest → Discord (`digest.py`)

- **Requirement:** a Monday-morning health snapshot — per-bucket last-write age,
  job heartbeats, deployment status, and the week's notable failures.
- **Data source:** InfluxDB (`INFLUXDB_{URL,TOKEN,ORG}`; run-digest.sh maps
  ask-dash/.env `INFLUX_READ_TOKEN` → `INFLUXDB_TOKEN`) for bucket freshness
  (`home_assistant`, `ops`, `system`, `transit_tracker`) + `ops` heartbeats; the
  dev-status server (`:8077`) for deployments. dev-status returns
  `{total,up,down,summary:[…],deployments:[{name,up:1|0,port,…}]}` — the digest
  reads `deployments[].up`, **not** a name→status map (the old code crashed
  calling `.get()` on the `summary` int).
- **Trigger:** Nomad periodic, Mondays 08:15 PT (`nomad/discord-digest.hcl`),
  via `run-digest.sh`.
- **Output:** one Discord embed with four fields — Bucket Health (age + ✅/⚠️/🚨),
  Jobs (ok/total/fail per task), Deployments (N/total up + list of any down),
  Notable Events (recent heartbeat failures).

## 3. Deployment-status watcher → Discord (`watcher.py`)

- **Requirement:** real-time-ish paging when a dev deployment flips UP↔DOWN, or
  when the dev-status server itself goes away.
- **Data source:** the dev-status server (`:8077`), same `deployments[].up`
  schema as the digest (the watcher's `_parse_services` had the identical
  schema bug — now reads `data["deployments"]` + `up`).
- **Trigger:** long-running launchd **KeepAlive** daemon
  (`launchd/com.tommydoerr.discord-watcher.plist`), polling every 30s with a
  2-poll debounce on DOWN.
- **Output:** Discord embeds — service UP (green), service DOWN after 2 polls
  (red), service disappeared (orange), dev-status unreachable / recovered (grey/
  green). launchd is TCC-blocked from `/Volumes`, so it runs from an
  internal-disk copy and gets its webhook from the plist `EnvironmentVariables`
  (see README deploy steps).

## 4. Claude output-token milestones → Discord (`claude_tokens.py`)

- **Requirement:** a celebratory ping every time cumulative **non-cache output
  tokens** used by Claude Code crosses another 1,000,000. Output tokens are
  inherently non-cached (caching is input-side), so this is the `output_tokens`
  field.
- **Data source:** InfluxDB `claude_code` bucket, `tokens` measurement,
  `output_tokens` field — `sum()` over all time (grouped) = cumulative total. Read
  creds via `run-claude-tokens.sh` (ask-dash `INFLUX_READ_TOKEN` → `INFLUXDB_TOKEN`).
- **Trigger:** Nomad periodic, every 15 min (`nomad/discord-claude-tokens.hcl`).
  Last-notified milestone index persists in
  `~/.local/share/claude-tokens-discord/state.json`. **First run seeds** at the
  current milestone and posts nothing (the total is already tens of millions); each
  later run fires once per newly-crossed 1M boundary (announcing the latest with a
  "since last check" count if several were crossed at once).
- **Output:** a gold Discord embed — milestone (e.g. "80M"), exact total, and step
  — to the #claude-usage channel (`DISCORD_WEBHOOK_URL_CLAUDE`, falling back to
  `DISCORD_WEBHOOK_URL`).
