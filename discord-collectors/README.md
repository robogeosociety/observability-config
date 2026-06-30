# discord-collectors

Small "ops → Discord" collectors for the home stack, reworked from requirements
up (the originals were Cowork-generated and broken — see `REQUIREMENTS.md` for
the contract each one now meets, and the per-file docstrings for the specific
bugs that were fixed).

Convention-aligned with `/Volumes/dev/CLAUDE.md` + this repo's `AGENTS.md`:

- **scheduled batch → Nomad `raw_exec`** running `uv run --with <deps>` (no
  global pip; deps are ephemeral per run);
- **long-running daemon → launchd `KeepAlive`**;
- **secrets never committed** — read from env / per-dir `.env` at runtime.

> Per `AGENTS.md`, an agent opens the PR and stops. **Nothing here is registered
> live by the PR** — no `nomad job run`, no `launchctl bootstrap`. The maintainer
> deploys after merge (steps below).

## Files

| File | What |
| --- | --- |
| `transit_discord.py` | OneBusAway per-route situations → Discord |
| `digest.py` | Weekly InfluxDB + dev-status ops digest → Discord |
| `watcher.py` | Long-running dev-status UP/DOWN watcher → Discord |
| `run-transit.sh` | Wrapper: sources the OBA key, `uv run` transit |
| `run-digest.sh` | Wrapper: maps ask-dash creds, `uv run` digest |
| `nomad/*.hcl` | Nomad periodic specs (transit 5m, digest Mon 08:15) |

> The Claude output-token **milestone notifier** (`claude_tokens.py`, cumulative
> every-1M posts to #claude-usage) was **retired** and replaced by the live
> `discord-claude-heatmap` container (a tick-updated per-project token heatmap in #ops).
| `launchd/com.tommydoerr.discord-watcher.plist` | launchd KeepAlive spec for the watcher |
| `tests/` | Hermetic pytest for the pure logic |

## Secrets / inputs

- **`DISCORD_WEBHOOK_URL`** — the channel webhook. The Nomad jobs (transit/
  digest) self-discover it from `grafana/.env` (the Nomad agent has Full Disk
  Access, so `raw_exec` can read `/Volumes`). The **watcher cannot** (launchd is
  TCC-blocked from `/Volumes`) — it gets the webhook from the plist
  `EnvironmentVariables` instead (placeholder in the committed plist).
- **`OBA_API_KEY`** (transit) — `run-transit.sh` greps it from
  `/Volumes/dev/transit_tracker/.local/service.yaml` (single source of truth).
- **InfluxDB read creds** (digest) — `run-digest.sh` sources `ask-dash/.env` and
  maps `INFLUX_READ_TOKEN` → `INFLUXDB_TOKEN`.

## Run locally (verification)

From this dir, with the webhook in env for a real dry-run:

```sh
source /Volumes/dev/observability/grafana/.env   # DISCORD_WEBHOOK_URL

# Transit (wrapper sources the OBA key):
./run-transit.sh --dry

# Digest (wrapper maps Influx creds):
./run-digest.sh --dry-run

# Watcher — single poll, no posting:
uv run --with httpx watcher.py --once
```

Tests (hermetic, no network):

```sh
uv run --with httpx --with pytest pytest tests/ -q
```

## Deploy (maintainer, after merge — NOT the PR agent)

### Nomad jobs (transit / digest)

```sh
cd /Volumes/dev/observability/discord-collectors
nomad job run nomad/discord-transit.hcl
nomad job run nomad/discord-digest.hcl
# run one now to verify:
nomad job periodic force discord-transit
```

These reference `/Volumes/dev/observability/discord-collectors/...` directly; the
Nomad agent's Full Disk Access lets `raw_exec` reach them and read `grafana/.env`.

### Watcher (launchd) — internal-disk copy + injected webhook

launchd is TCC-blocked from `/Volumes`, so the watcher runs from an
**internal-disk copy** and the webhook is injected via the plist (the committed
plist keeps a placeholder — never the real URL):

```sh
mkdir -p ~/.local/share/discord-collectors
cp /Volumes/dev/observability/discord-collectors/watcher.py \
   ~/.local/share/discord-collectors/watcher.py

cp /Volumes/dev/observability/discord-collectors/launchd/com.tommydoerr.discord-watcher.plist \
   ~/Library/LaunchAgents/com.tommydoerr.discord-watcher.plist
# edit ~/Library/LaunchAgents/com.tommydoerr.discord-watcher.plist:
#   replace REPLACE_WITH_DISCORD_WEBHOOK_URL with the real webhook
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tommydoerr.discord-watcher.plist
```

Re-run the two `cp` steps on every change (the internal copy is the one that
runs). The uv cache stays on the internal disk — do **not** set
`UV_CACHE_DIR=/Volumes` for the launchd job (launchd can't write there).

## Related (NOT in this dir)

- **Weather alerts** are Grafana, not a collector:
  `grafana/provisioning/alerting/weather-alerts.yml` (Tempest thresholds → the
  existing `discord` contact point).
- `notify_v2.py` lives in **obsidian-automations**, and the `.ts` notifiers live
  in **is-the-mountain-out** / **robot-geographical-society** — deliberately not
  here.
- **GitHub activity → Discord** is handled by **GitHub's native Discord
  integration** (per-repo webhook), not a collector. The old `github_discord.py`
  poller was retired: it only emitted PR/issue embeds on a 30-min poll and read the
  *public* user-events feed (blind to private repos), where the native webhook is
  real-time and visibility-agnostic.
