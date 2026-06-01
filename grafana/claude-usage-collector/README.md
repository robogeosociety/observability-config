# claude-usage-collector

Parses Claude Code session transcripts (`~/.claude/projects/**/*.jsonl`) and writes
per-message token usage to the InfluxDB `claude_code` bucket, feeding the
**Claude Code — Token Usage** dashboard (`claude-usage`). Design + schema:
`../SPEC-claude-usage.md`.

**Cost is out of scope by design** — transcripts carry no cost, so we track raw
token counts only (input / output / cache-create / cache-read) and never guess.

## How it runs

`collector.py` is pure stdlib. Like `dev-status/`, the repo file is the source of
truth but the *running* copy lives on the internal disk — macOS TCC blocks
launchd from reading `/Volumes` (exit 78). `deploy.sh` copies it to
`~/.local/share/claude-usage-collector/` and loads a launchd job
(`com.tommy.claude-usage`, every 5 min). The collector only reads `~/.claude` and
writes to `localhost:8086`, so it needs no `/Volumes` access at runtime.

A per-file byte-offset cursor (`~/.local/state/claude-usage-collector/cursor.json`)
makes each run incremental and idempotent — re-running never double-counts.

## One-time activation

```sh
source /Volumes/dev/observability/influxdb/.env
# 1. bucket + write token (keep history)
docker exec influxdb influx bucket create --org home --token "$INFLUX_ADMIN_TOKEN" --name claude_code --retention 0
docker exec influxdb influx auth create --org home --token "$INFLUX_ADMIN_TOKEN" \
  --write-bucket "$(docker exec influxdb influx bucket list --org home --token "$INFLUX_ADMIN_TOKEN" --hide-headers --name claude_code | awk '{print $1}')" \
  --description "claude_code write"
# 2. add the printed token to influxdb/.env as INFLUX_CLAUDE_USAGE_TOKEN.
#    The Grafana read token is per-bucket: mint a fresh one covering all dashboard
#    buckets PLUS claude_code, set it as INFLUX_GRAFANA_TOKEN in grafana/.env.

# 3. load the new datasource (provisioning lives on the internal disk now) and,
#    because grafana/.env changed, recreate grafana to re-read the env
( cd /Volumes/dev/observability/grafana && ./deploy-provisioning.sh && docker compose up -d )

# 4. backfill all existing transcripts once, then start the scheduled job
INFLUX_CLAUDE_USAGE_TOKEN=… /usr/bin/python3 grafana/claude-usage-collector/collector.py --reset
./deploy.sh
```

## Local use

```sh
uv run --no-project python collector.py --dry-run   # print line protocol, write nothing
uv run --no-project python collector.py --reset     # re-read every transcript (full backfill)
```

## Tests

`test_collector.py` is hermetic (pure `parse_line` / `build_line`) and runs in the
unit tier / CI.
