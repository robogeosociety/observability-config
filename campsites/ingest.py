#!/usr/bin/env python3
"""
Ingest campsite-availability summaries from R2 → InfluxDB (`campsites` bucket).

The raw-collection job (robot-geographical-society backend Worker) scrapes
recreation.gov + WA State Parks via Browser Rendering and writes
`summary/<date>/<id>.json` to the `campsite-raw` R2 bucket. This — the
observability side — pulls those summaries (S3 API) and writes one time-series
point per (campsite, target_date), feeding the Campsite Availability dashboard's
burn-down / sell-out projection. No scraping here; download + write only.

Run via uv (self-contained, no project):
    uv run --no-project --with boto3 python ingest.py
    uv run --no-project --with boto3 python ingest.py --date 2026-06-01 --dry-run
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --- InfluxDB line protocol -------------------------------------------------

def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def build_lines(cid, name, agency, by_date, ts):
    lines = []
    for d, c in sorted(by_date.items()):
        a = int(c.get("available", 0)); r = int(c.get("reserved", 0)); t = int(c.get("total", a + r))
        tags = f"campsite={_esc(cid)},name={_esc(name)},agency={_esc(agency)},target_date={_esc(d)}"
        lines.append(f"availability,{tags} available={a}i,reserved={r}i,total={t}i {ts}")
    return lines


def influx_write(lines):
    url = os.environ.get("INFLUX_URL", "http://localhost:8086").rstrip("/")
    org = os.environ.get("INFLUX_ORG", "home")
    bucket = os.environ.get("INFLUX_CAMPSITES_BUCKET", "campsites")
    token = os.environ.get("INFLUX_CAMPSITES_TOKEN") or os.environ.get("INFLUX_ADMIN_TOKEN")
    if not token:
        raise SystemExit("No InfluxDB token (set INFLUX_CAMPSITES_TOKEN in campsites/.env)")
    if not lines:
        return 0
    endpoint = f"{url}/api/v2/write?org={org}&bucket={bucket}&precision=s"
    for i in range(0, len(lines), 5000):
        chunk = "\n".join(lines[i : i + 5000]).encode()
        req = urllib.request.Request(endpoint, data=chunk, method="POST",
            headers={"Authorization": f"Token {token}", "Content-Type": "text/plain; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status not in (200, 204):
                raise SystemExit(f"InfluxDB write HTTP {resp.status}")
    return len(lines)


def heartbeat(source, kind, success, duration_s, rows, interval_s):
    """Best-effort collector heartbeat → ops bucket (see COLLECTORS.md)."""
    import socket
    url = os.environ.get("INFLUX_URL", "http://localhost:8086").rstrip("/")
    org = os.environ.get("INFLUX_ORG", "home")
    token = os.environ.get("INFLUX_OPS_TOKEN") or os.environ.get("INFLUX_ADMIN_TOKEN")
    if not token:
        print("no INFLUX_OPS_TOKEN — skipping heartbeat", file=sys.stderr)
        return
    host = socket.gethostname().split(".")[0]
    line = (f"collector,source={_esc(source)},kind={_esc(kind)},host={_esc(host)} "
            f"success={1 if success else 0}i,duration_s={duration_s:.3f},"
            f"rows={int(rows)}i,interval_s={int(interval_s)}i")
    try:
        endpoint = f"{url}/api/v2/write?org={org}&bucket=ops&precision=s"
        req = urllib.request.Request(endpoint, data=line.encode(), method="POST",
            headers={"Authorization": f"Token {token}", "Content-Type": "text/plain; charset=utf-8"})
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:  # heartbeat must never fail the run
        print(f"heartbeat failed (non-fatal): {e}", file=sys.stderr)

# --- R2 ---------------------------------------------------------------------

def _load_env(p: Path):
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _r2():
    import boto3
    acct = os.environ["R2_ACCOUNT_ID"]
    return boto3.client("s3", endpoint_url=f"https://{acct}.r2.cloudflarestorage.com",
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    _load_env(Path(__file__).resolve().parent / ".env")
    bucket = os.environ.get("R2_CAMPSITE_BUCKET", "campsite-raw")
    prefix = f"summary/{a.date}/"

    s3 = _r2()
    objs = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    print(f"{len(objs)} summaries under {prefix}", file=sys.stderr)

    lines, n = [], 0
    for o in objs:
        obj = s3.get_object(Bucket=bucket, Key=o["Key"])
        ts = int(obj["LastModified"].timestamp())  # true scrape time
        rec = json.loads(obj["Body"].read())
        lines += build_lines(rec["id"], rec["name"], rec.get("agency", ""), rec.get("by_date", {}), ts)
        n += 1
    print(f"{n} campsites · {len(lines)} points", file=sys.stderr)
    if a.dry_run:
        print("\n".join(lines[:6]))
        print(f"... ({len(lines)} total, dry-run)", file=sys.stderr)
        return

    # Real run: write points, then emit a collector heartbeat regardless of outcome
    # (0 rows when the Worker hasn't run for this date yet is still a success).
    t0 = time.time(); written = 0; ok = False
    try:
        written = influx_write(lines)
        print(f"wrote {written} points → InfluxDB 'campsites'", file=sys.stderr)
        ok = True
    finally:
        heartbeat("campsites", "batch", ok, time.time() - t0, written, 86400)


if __name__ == "__main__":
    main()
