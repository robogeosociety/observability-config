#!/usr/bin/env python3
"""Claude Code output-token milestone -> Discord.

Posts a Discord embed each time cumulative **non-cache output tokens** in the
`claude_code` InfluxDB bucket cross another MILESTONE_STEP (default 1,000,000).

Output tokens are inherently non-cached — caching is input-side (cache_read_tokens
/ cache_creation_tokens), so "non-cache output tokens" == `output_tokens`.

## No-backfill seeding

The all-time total is already tens of millions, so the FIRST run (no state file)
just records the current milestone and posts nothing — otherwise it would fire
dozens of alerts for milestones already passed. Every later run notifies once per
newly-crossed boundary (and, if several boundaries were crossed between runs,
announces the latest with a "since last check" count).

State (last-notified milestone index) persists in
~/.local/share/claude-tokens-discord/state.json.

Webhook:  env DISCORD_WEBHOOK_URL_CLAUDE -> grafana/.env -> DISCORD_WEBHOOK_URL.
InfluxDB: INFLUXDB_{URL,TOKEN,ORG} (run-claude-tokens.sh maps ask-dash read creds).

    uv run --with httpx --with influxdb-client claude_tokens.py --dry-run
    uv run --with httpx --with influxdb-client claude_tokens.py --reseed   # re-seed state to current, no post
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

MILESTONE_STEP = 1_000_000
BUCKET = "claude_code"
STATE_FILE = Path.home() / ".local/share/claude-tokens-discord/state.json"
GOLD = 0xF1C40F


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
        # Prefer a Claude-usage-specific channel webhook; fall back to the shared one.
        "discord_webhook_url": (
            os.environ.get("DISCORD_WEBHOOK_URL_CLAUDE")
            or grafana_env.get("DISCORD_WEBHOOK_URL_CLAUDE")
            or os.environ.get("DISCORD_WEBHOOK_URL")
            or grafana_env.get("DISCORD_WEBHOOK_URL", "")
        ),
    }


# ---------------------------------------------------------------------------
# Pure milestone logic (hermetically tested)
# ---------------------------------------------------------------------------

def milestone_index(total: int, step: int = MILESTONE_STEP) -> int:
    """How many whole `step`-sized milestones `total` has reached."""
    return total // step


def decide(total: int, last_milestone: int | None, step: int = MILESTONE_STEP) -> dict:
    """Decide whether to notify.

    Returns {current, crossed, notify, seed}:
      - seed=True   → first run (last_milestone is None): record current, don't post.
      - notify=True → current milestone advanced past last_milestone; `crossed` is
                      how many boundaries since last check.
      - otherwise   → nothing to do.
    """
    current = milestone_index(total, step)
    if last_milestone is None:
        return {"current": current, "crossed": 0, "notify": False, "seed": True}
    crossed = current - last_milestone
    return {"current": current, "crossed": crossed, "notify": crossed > 0, "seed": False}


def _human(n: int) -> str:
    """80_000_000 -> '80M'; 1_500_000 -> '1.5M'."""
    m = n / 1_000_000
    return f"{m:.0f}M" if m == int(m) else f"{m:.1f}M"


def _fmt(n: int) -> str:
    """Compact token count: 14_618_886 -> '14.6M'; 286_810 -> '287k'; 42 -> '42'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _short_model(m: str) -> str:
    """'claude-opus-4-8' -> 'opus-4-8'; strip date suffix on dated ids."""
    return m.replace("claude-", "").rsplit("-2", 1)[0]


def _breakdown_field(rows: list[tuple[str, int]], total: int) -> str:
    """Render 'name — 14.6M (18%)' lines for an embed field."""
    if not rows:
        return "—"
    out = []
    for name, val in rows:
        pct = f" ({100 * val / total:.0f}%)" if total else ""
        out.append(f"`{_fmt(val)}`{pct} {name}")
    return "\n".join(out)


def build_embed(
    total: int,
    current: int,
    crossed: int,
    step: int = MILESTONE_STEP,
    projects: list[tuple[str, int]] | None = None,
    models: list[tuple[str, int]] | None = None,
    window_total: int = 0,
) -> dict:
    reached = current * step
    since = (
        f"+{crossed} milestone{'s' if crossed != 1 else ''} since last check"
        if crossed > 1
        else "next milestone reached"
    )
    fields = [
        {"name": "Milestone", "value": _human(reached), "inline": True},
        {"name": "Exact total", "value": f"{total:,}", "inline": True},
        {"name": "Step", "value": _human(step), "inline": True},
    ]
    # Breakdown is scoped to "what drove this milestone" — the window since the last
    # one — so percentages are a share of `window_total`, not the cumulative grand total.
    denom = window_total or sum(v for _, v in (projects or []))
    if projects:
        fields.append({"name": "Top projects (this milestone)", "value": _breakdown_field(projects, denom), "inline": False})
    if models:
        short = [(_short_model(n), v) for n, v in models]
        fields.append({"name": "By model (this milestone)", "value": _breakdown_field(short, denom), "inline": False})
    return {
        "embeds": [
            {
                "title": f"🎰 Claude usage: {_human(reached)} output tokens",
                "description": (
                    f"Crossed **{_human(reached)}** cumulative non-cache output tokens "
                    f"({since})."
                ),
                "color": GOLD,
                "fields": fields,
            }
        ]
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> tuple[int | None, str | None]:
    """Returns (last_milestone, last_ts). last_ts marks when that milestone was
    recorded, so the breakdown window is 'since the last milestone'."""
    try:
        d = json.loads(STATE_FILE.read_text())
        return int(d["last_milestone"]), d.get("last_ts")
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None, None


def save_state(milestone: int, ts: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"last_milestone": int(milestone), "last_ts": ts}) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# InfluxDB
# ---------------------------------------------------------------------------

def query_total_output_tokens(client, org: str, start: str = "0") -> int:
    flux = f'''
from(bucket: "{BUCKET}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "tokens" and r._field == "output_tokens")
  |> group()
  |> sum()
'''
    tables = client.query_api().query(flux, org=org)
    for table in tables:
        for record in table.records:
            return int(record.get_value())
    return 0


def query_breakdown(client, org: str, tag: str, start: str = "0", limit: int = 5) -> list[tuple[str, int]]:
    """output_tokens grouped by a tag (project/model/session) over [start, now), top `limit`.
    `start` is a Flux time literal — an RFC3339 stamp (since last milestone) or `0` (all-time)."""
    flux = f'''
from(bucket: "{BUCKET}")
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "tokens" and r._field == "output_tokens")
  |> group(columns: ["{tag}"])
  |> sum()
  |> group()
  |> sort(columns: ["_value"], desc: true)
  |> limit(n: {limit})
'''
    rows: list[tuple[str, int]] = []
    for table in client.query_api().query(flux, org=org):
        for record in table.records:
            name = record.values.get(tag) or "(none)"
            rows.append((name, int(record.get_value())))
    return rows


def post_to_discord(webhook_url: str, payload: dict) -> None:
    resp = httpx.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()
    print(f"[ok] Discord webhook returned {resp.status_code}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Claude output-token milestone -> Discord")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen; never post or write state")
    parser.add_argument("--reseed", action="store_true", help="Reset state to the current milestone and exit (no post)")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg["influxdb_token"]:
        print("[error] INFLUXDB_TOKEN not set and not found in .env", file=sys.stderr)
        sys.exit(1)

    from influxdb_client import InfluxDBClient  # lazy: keeps unit tests hermetic

    client = InfluxDBClient(url=cfg["influxdb_url"], token=cfg["influxdb_token"], org=cfg["influxdb_org"])
    org = cfg["influxdb_org"]
    now = _now_iso()
    total = query_total_output_tokens(client, org)
    print(f"[*] cumulative output_tokens = {total:,}")

    if args.reseed:
        client.close()
        cur = milestone_index(total)
        if not args.dry_run:
            save_state(cur, now)
        print(f"[done] reseeded state to milestone {cur} ({_human(cur * MILESTONE_STEP)})")
        return

    last_milestone, last_ts = load_state()
    d = decide(total, last_milestone)

    if d["seed"]:
        client.close()
        if not args.dry_run:
            save_state(d["current"], now)
        print(f"[seed] first run — recorded milestone {d['current']} ({_human(d['current']*MILESTONE_STEP)}), no notification")
        return

    if not d["notify"]:
        client.close()
        print(f"[*] no new milestone (at {d['current']}, last notified {last_milestone})")
        return

    if not args.dry_run and not cfg["discord_webhook_url"]:
        client.close()
        print("[error] DISCORD_WEBHOOK_URL(_CLAUDE) not set and not found in .env", file=sys.stderr)
        sys.exit(1)

    # Milestone crossed — break down "what drove it" over the window since the last
    # milestone (fall back to 30d if state predates timestamping).
    window_start = last_ts or "-30d"
    print(f"[*] querying token breakdown (project, model) since {window_start} ...")
    window_total = query_total_output_tokens(client, org, start=window_start)
    projects = query_breakdown(client, org, "project", start=window_start, limit=5)
    models = query_breakdown(client, org, "model", start=window_start, limit=4)
    client.close()

    payload = build_embed(total, d["current"], d["crossed"], projects=projects, models=models, window_total=window_total)
    if args.dry_run:
        print("\n--- DRY RUN — embed payload: ---")
        print(json.dumps(payload, indent=2))
        print(f"(would update state {last_milestone} -> {d['current']} @ {now})")
    else:
        print(f"[*] Posting milestone {d['current']} to Discord ...")
        post_to_discord(cfg["discord_webhook_url"], payload)
        save_state(d["current"], now)
    print("[done]")


if __name__ == "__main__":
    main()
