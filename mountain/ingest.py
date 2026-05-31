#!/usr/bin/env python3
"""
Ingest is-the-mountain-out inference results from R2 → InfluxDB (`mountain` bucket).

The inference Worker (tommyroar/is-the-mountain-out) runs a */15 cron that
predicts whether Mount Rainier is visible and appends one record per tick to
`history.jsonl` in the public `is-the-mountain-out-public` R2 bucket. This — the
observability side — fetches that file over HTTPS (the bucket is public, no auth)
and writes two measurements per tick to InfluxDB, feeding the Mountain Out
dashboards. No inference here; download + write only.

Writes are idempotent: a point's identity is (measurement, tag set, timestamp),
so re-ingesting the same history.jsonl overwrites rather than duplicates.

  prediction  (ok ticks only) — tags: class_name, model_version, station
              fields: is_out(i), class_index(i), conf_not_out, conf_full,
                      conf_partial, visibility_sm?, ceiling_ft?   @ state.timestamp_utc
  tick        (every tick)     — tags: status, error_type?
              fields: ok(i), duration_seconds                    @ finished_at

Run via uv (self-contained, no project):
    uv run --no-project python ingest.py
    uv run --no-project python ingest.py --dry-run
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

CLASS_NAMES = ["not_out", "full", "partial"]


# --- InfluxDB line protocol -------------------------------------------------

def _esc_tag(v) -> str:
    return str(v).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _ts_ns(iso: str) -> int:
    """ISO-8601 (…Z) → epoch nanoseconds."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def build_lines(records) -> list[str]:
    lines: list[str] = []
    for rec in records:
        status = rec.get("status", "error")
        finished = rec.get("finished_at")
        if not finished:
            continue
        ts = _ts_ns(finished)

        # tick — operational health, one per tick (ok or error)
        tick_tags = f"status={_esc_tag(status)}"
        if status == "error":
            etype = (rec.get("error") or {}).get("type", "Error")
            tick_tags += f",error_type={_esc_tag(etype)}"
        ok = 1 if status == "ok" else 0
        dur = float(rec.get("duration_seconds", 0.0))
        lines.append(f"tick,{tick_tags} ok={ok}i,duration_seconds={dur} {ts}")

        # prediction — only when the tick produced a state
        state = rec.get("state")
        if status != "ok" or not state:
            continue
        conf = state.get("confidence", {})
        weather = state.get("weather", {})
        ptags = (
            f"class_name={_esc_tag(state.get('class_name', 'unknown'))}"
            f",model_version={_esc_tag(state.get('model_version') or 'unknown')}"
            f",station={_esc_tag(weather.get('station', 'unknown'))}"
        )
        fields = [
            f"is_out={1 if state.get('is_out') else 0}i",
            f"class_index={int(state.get('class_index', -1))}i",
            f"conf_not_out={float(conf.get('not_out', 0.0))}",
            f"conf_full={float(conf.get('full', 0.0))}",
            f"conf_partial={float(conf.get('partial', 0.0))}",
        ]
        if weather.get("visibility_sm") is not None:
            fields.append(f"visibility_sm={float(weather['visibility_sm'])}")
        if weather.get("ceiling_ft") is not None:
            fields.append(f"ceiling_ft={float(weather['ceiling_ft'])}")
        pts = _ts_ns(state.get("timestamp_utc", finished))
        lines.append(f"prediction,{ptags} {','.join(fields)} {pts}")
    return lines


# --- I/O --------------------------------------------------------------------

def fetch_history(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "mountain-ingest/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    records = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def influx_write(lines: list[str]) -> None:
    url = os.environ.get("INFLUX_URL", "http://localhost:8086").rstrip("/")
    org = os.environ.get("INFLUX_ORG", "home")
    bucket = os.environ.get("INFLUX_MOUNTAIN_BUCKET", "mountain")
    token = os.environ.get("INFLUX_MOUNTAIN_TOKEN") or os.environ.get("INFLUX_ADMIN_TOKEN")
    if not token:
        raise SystemExit("No InfluxDB token (set INFLUX_MOUNTAIN_TOKEN in mountain/.env)")
    endpoint = f"{url}/api/v2/write?org={org}&bucket={bucket}&precision=ns"
    data = ("\n".join(lines) + "\n").encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=data, method="POST",
        headers={"Authorization": f"Token {token}", "Content-Type": "text/plain; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 204):
            raise SystemExit(f"InfluxDB write failed: HTTP {resp.status}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Mirror is-the-mountain-out history.jsonl into InfluxDB.")
    ap.add_argument(
        "--url",
        default=os.environ.get(
            "MOUNTAIN_HISTORY_URL",
            "https://pub-66d3d1f139004e29b2afcb5fba49bdb3.r2.dev/history.jsonl",
        ),
        help="Public URL of the inference history.jsonl",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print line protocol; do not write")
    args = ap.parse_args()

    records = fetch_history(args.url)
    lines = build_lines(records)
    if not lines:
        print("No records to ingest.")
        return 0

    if args.dry_run:
        print("\n".join(lines))
        print(f"\n[dry-run] {len(lines)} points from {len(records)} ticks (not written)", file=sys.stderr)
        return 0

    influx_write(lines)
    print(f"Wrote {len(lines)} points from {len(records)} ticks to InfluxDB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
