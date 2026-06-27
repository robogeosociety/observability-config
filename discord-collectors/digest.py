#!/usr/bin/env python3
"""Weekly Ops Digest -> Discord (Monday mornings).

Queries InfluxDB for per-bucket last-write age + job heartbeats, fetches
deployment status from the dev-status server, and posts a single summary embed.

## The dev-status schema gotcha (the AttributeError fix)

dev-status (`:8077`) returns:

    {"total": 24, "up": 6, "down": 18,
     "summary": [{"up": 6, "down": 18, "total": 24}],
     "deployments": [{"name": "grafana", "up": 1, "port": 3001, ...}, ...]}

It is NOT a `name -> status` map. The old `_format_deployments` iterated the top
level and called `.get()` on the `summary` int / list, crashing with
`AttributeError: 'int' object has no attribute 'get'`. The fix reads
`data["deployments"]` and each entry's `up` (1/0) field.

Buckets: the old `["telegraf","weather","ops"]` don't exist. The real buckets
are home_assistant / ops / system / transit_tracker (continuous), plus the
frozen/event-driven ones we don't report on. We summarise the continuous ones.

InfluxDB creds: env INFLUXDB_{URL,TOKEN,ORG} (run-digest.sh maps ask-dash/.env's
INFLUX_READ_TOKEN -> INFLUXDB_TOKEN). Discord webhook: env -> grafana/.env.

    uv run --with httpx --with influxdb-client digest.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Buckets the digest reports on (continuously-written; the frozen/event-driven
# ones — tempest_archive, zigbee_archive, claude_code — are deliberately omitted
# so a quiet bucket never looks "broken" in the digest).
BUCKETS = ["home_assistant", "ops", "system", "transit_tracker"]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _read_dotenv(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    p = Path(path).expanduser()
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("\"'")
    return env


def load_config() -> dict:
    ask_dash_env = _read_dotenv("~/dev/observability/ask-dash/.env")
    grafana_env = _read_dotenv("~/dev/observability/grafana/.env")
    return {
        "influxdb_url": os.environ.get("INFLUXDB_URL", ask_dash_env.get("INFLUX_URL", "http://localhost:8086")),
        "influxdb_token": os.environ.get("INFLUXDB_TOKEN", ask_dash_env.get("INFLUX_READ_TOKEN", "")),
        "influxdb_org": os.environ.get("INFLUXDB_ORG", ask_dash_env.get("INFLUX_ORG", "home")),
        # Prefer a digest-specific webhook (its own #ops-digest channel); fall back
        # to the shared platform webhook so a single-channel setup still works.
        "discord_webhook_url": (
            os.environ.get("DISCORD_WEBHOOK_URL_DIGEST")
            or grafana_env.get("DISCORD_WEBHOOK_URL_DIGEST")
            or os.environ.get("DISCORD_WEBHOOK_URL")
            or grafana_env.get("DISCORD_WEBHOOK_URL", "")
        ),
        "dev_status_url": os.environ.get("DEV_STATUS_URL", "http://localhost:8077"),
    }


# ---------------------------------------------------------------------------
# InfluxDB queries (influxdb_client is lazy-imported so unit tests stay hermetic)
# ---------------------------------------------------------------------------

def query_bucket_health(client, org: str) -> list[dict]:
    api = client.query_api()
    results = []
    for bucket in BUCKETS:
        flux = (
            f'from(bucket: "{bucket}")\n'
            "  |> range(start: -7d)\n"
            "  |> last()\n"
            '  |> keep(columns: ["_time"])\n'
        )
        try:
            tables = api.query(flux, org=org)
            last_time = None
            for table in tables:
                for record in table.records:
                    t = record.get_time()
                    if last_time is None or t > last_time:
                        last_time = t
            results.append({"bucket": bucket, "last_write": last_time})
        except Exception as exc:  # noqa: BLE001
            results.append({"bucket": bucket, "last_write": None, "error": str(exc)})
    return results


def query_job_heartbeats(client, org: str) -> list[dict]:
    api = client.query_api()
    flux = """
from(bucket: "ops")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "heartbeat")
  |> filter(fn: (r) => r._field == "success")
  |> group(columns: ["task_name"])
  |> reduce(
       identity: {total: 0, ok: 0, fail: 0},
       fn: (r, accumulator) => ({
         total: accumulator.total + 1,
         ok:   if r._value == 1 then accumulator.ok + 1 else accumulator.ok,
         fail: if r._value != 1 then accumulator.fail + 1 else accumulator.fail
       })
     )
"""
    results = []
    try:
        tables = api.query(flux, org=org)
        for table in tables:
            for record in table.records:
                results.append({
                    "task_name": record.values.get("task_name", "unknown"),
                    "total": record.values.get("total", 0),
                    "ok": record.values.get("ok", 0),
                    "fail": record.values.get("fail", 0),
                })
    except Exception as exc:  # noqa: BLE001
        results.append({"task_name": "QUERY_ERROR", "error": str(exc)})
    return results


def query_notable_events(client, org: str) -> list[dict]:
    api = client.query_api()
    flux = """
from(bucket: "ops")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "heartbeat")
  |> filter(fn: (r) => r._field == "success")
  |> filter(fn: (r) => r._value != 1)
  |> keep(columns: ["_time", "task_name", "step"])
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 15)
"""
    events = []
    try:
        tables = api.query(flux, org=org)
        for table in tables:
            for record in table.records:
                events.append({
                    "time": record.get_time().strftime("%Y-%m-%d %H:%M UTC"),
                    "task_name": record.values.get("task_name", "unknown"),
                    "step": record.values.get("step", ""),
                })
    except Exception as exc:  # noqa: BLE001
        events.append({"time": "N/A", "task_name": "QUERY_ERROR", "step": str(exc)})
    return events


# ---------------------------------------------------------------------------
# Dev-status
# ---------------------------------------------------------------------------

def fetch_deployments(url: str) -> dict | None:
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] dev-status unreachable: {exc}", file=sys.stderr)
        return None


def parse_deployments(data: dict | None) -> dict[str, bool]:
    """Return {name: up_bool} from the dev-status payload (deduped, UP wins)."""
    out: dict[str, bool] = {}
    if not isinstance(data, dict):
        return out
    for entry in data.get("deployments", []) or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        is_up = entry.get("up") == 1 or entry.get("up") is True
        out[name] = out.get(name, False) or is_up
    return out


# ---------------------------------------------------------------------------
# Embed formatting (pure)
# ---------------------------------------------------------------------------

def _format_bucket_health(health: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    lines = []
    for entry in health:
        bucket = entry["bucket"]
        lt = entry.get("last_write")
        if lt is None:
            lines.append(f"\U0001F6A8 **{bucket}** — no data in 7d")
            continue
        hours = (now - lt).total_seconds() / 3600
        ts = lt.strftime("%Y-%m-%d %H:%M UTC")
        emoji = "\U0001F6A8" if hours > 72 else ("⚠️" if hours > 24 else "✅")
        lines.append(f"{emoji} **{bucket}** — last write {ts} ({hours:.0f}h ago)")
    return "\n".join(lines) or "No buckets checked."


def _format_jobs(jobs: list[dict]) -> str:
    if not jobs:
        return "No heartbeat data."
    lines = []
    for j in jobs:
        if "error" in j:
            lines.append(f"\U0001F6A8 Query error: {j['error']}")
            continue
        status = "✅" if j["fail"] == 0 else "⚠️"
        lines.append(f"{status} **{j['task_name']}** — {j['ok']}/{j['total']} ok, {j['fail']} fail")
    return "\n".join(lines)


def format_deployments(data: dict | None) -> str:
    """One-line summary + the list of any DOWN deployments (pure)."""
    if data is None:
        return "\U0001F6A8 dev-status server unreachable"
    services = parse_deployments(data)
    if not services:
        return "No deployments reported."
    up = sum(1 for v in services.values() if v)
    total = len(services)
    header = f"✅ **{up}/{total}** deployments up"
    down = sorted(n for n, v in services.items() if not v)
    if not down:
        return header
    listed = down[:12]
    lines = [header] + [f"\U0001F534 {n}" for n in listed]
    if len(down) > len(listed):
        lines.append(f"…and {len(down) - len(listed)} more down")
    return "\n".join(lines)


def _format_notable(events: list[dict]) -> str:
    if not events:
        return "No failures this week."
    lines = []
    for e in events:
        step = f" (step: {e['step']})" if e.get("step") else ""
        lines.append(f"• `{e['time']}` — **{e['task_name']}**{step}")
    return "\n".join(lines)


def build_embed(health, jobs, deployments, notable) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "embeds": [
            {
                "title": "\U0001F4CA Weekly Ops Digest",
                "color": 0x3498DB,
                "timestamp": now.isoformat(),
                "footer": {"text": "ops-digest | ran on schedule"},
                "fields": [
                    {"name": "\U0001F5C4️ Bucket Health", "value": _format_bucket_health(health)[:1024], "inline": False},
                    {"name": "\U0001F4BC Jobs (past 7d)", "value": (_format_jobs(jobs) or "—")[:1024], "inline": False},
                    {"name": "\U0001F680 Deployments", "value": format_deployments(deployments)[:1024], "inline": False},
                    {"name": "⚡ Notable Events", "value": _format_notable(notable)[:1024], "inline": False},
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def post_to_discord(webhook_url: str, payload: dict) -> None:
    resp = httpx.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()
    print(f"[ok] Discord webhook returned {resp.status_code}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly Ops Digest for Discord")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the embed JSON instead of posting")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg["influxdb_token"]:
        print("[error] INFLUXDB_TOKEN not set and not found in .env", file=sys.stderr)
        sys.exit(1)
    if not args.dry_run and not cfg["discord_webhook_url"]:
        print("[error] DISCORD_WEBHOOK_URL not set and not found in .env", file=sys.stderr)
        sys.exit(1)

    from influxdb_client import InfluxDBClient  # lazy: keeps unit tests hermetic

    client = InfluxDBClient(url=cfg["influxdb_url"], token=cfg["influxdb_token"], org=cfg["influxdb_org"])
    print("[*] Querying bucket health ...")
    health = query_bucket_health(client, cfg["influxdb_org"])
    print("[*] Querying job heartbeats ...")
    jobs = query_job_heartbeats(client, cfg["influxdb_org"])
    print("[*] Querying notable events ...")
    notable = query_notable_events(client, cfg["influxdb_org"])
    client.close()

    print("[*] Fetching deployment status ...")
    deployments = fetch_deployments(cfg["dev_status_url"])

    payload = build_embed(health, jobs, deployments, notable)

    if args.dry_run:
        print("\n--- DRY RUN — embed payload: ---")
        print(json.dumps(payload, indent=2, default=str))
    else:
        print("[*] Posting to Discord ...")
        post_to_discord(cfg["discord_webhook_url"], payload)
    print("[done]")


if __name__ == "__main__":
    main()
