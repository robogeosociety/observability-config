#!/usr/bin/env python3
"""Per-bucket InfluxDB inventory collector → `ops` bucket.

Nothing else records per-bucket storage/cardinality over time: InfluxDB exposes
it live at `/metrics` (Prometheus text, keyed by *hex bucket id*) but never
persists it. This collector scrapes that endpoint every run, resolves ids → human
names via the buckets API, derives a real per-bucket write rate, and writes one
`bucket_stats` point per bucket to the `ops` bucket. The `InfluxDB Buckets`
Grafana dashboard reads it back.

Per bucket, each run emits (measurement `bucket_stats`, tag `bucket=<name>`):
  - disk_bytes      int   on-disk size = storage_tsm_files_disk_bytes
                          + storage_cache_disk_bytes (compacted TSM + WAL/cache)
  - series          int   storage_bucket_series_num   (series cardinality)
  - measurements    int   storage_bucket_measurement_num
  - points_per_sec  float points written in the last INTERVAL window / INTERVAL
                          (storage_writer_ok_points is global-only, so throughput
                          is counted per bucket via Flux over the recent window)

Stdlib-only (urllib), mirroring campsites/ingest.py: a launchd-spawned `uv`/TLS
stack hangs in the background session, and everything here is plain-HTTP to
localhost:8086, so urllib is enough. /metrics needs no auth; the buckets API and
the per-bucket Flux counts use INFLUX_TOKEN (needs read on all buckets + write on
`ops` — the admin token satisfies both).
"""
from __future__ import annotations

import os
import re
import sys
import time
import urllib.error
import urllib.request

INFLUX_URL = os.environ.get("INFLUX_URL", "http://localhost:8086").rstrip("/")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "")
OPS_BUCKET = os.environ.get("INFLUX_OPS_BUCKET", "ops")
# Window over which throughput is counted; should match the launchd cadence.
INTERVAL_S = int(os.environ.get("BUCKET_STATS_INTERVAL_S", "300"))
HTTP_TIMEOUT = 30

# /metrics series we care about, all labelled bucket="<hex id>".
_METRIC_LINE = re.compile(r'^(?P<name>\w+)\{(?P<labels>[^}]*)\}\s+(?P<value>[\d.eE+-]+)\s*$')
_BUCKET_LABEL = re.compile(r'bucket="([0-9a-fA-F]+)"')

# Sum these per bucket → disk_bytes. The rest map 1:1.
_SUM_BYTES = {"storage_tsm_files_disk_bytes", "storage_cache_disk_bytes"}
_DIRECT = {"storage_bucket_series_num": "series",
           "storage_bucket_measurement_num": "measurements"}


def _req(path: str, *, method: str = "GET", data: bytes | None = None,
         headers: dict | None = None) -> bytes:
    req = urllib.request.Request(INFLUX_URL + path, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def scrape_metrics() -> dict[str, dict[str, float]]:
    """Return {bucket_id: {disk_bytes, series, measurements}} from /metrics."""
    text = _req("/metrics").decode("utf-8", "replace")
    out: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        if not (line.startswith("storage_tsm_files_disk_bytes")
                or line.startswith("storage_cache_disk_bytes")
                or line.startswith("storage_bucket_series_num")
                or line.startswith("storage_bucket_measurement_num")):
            continue
        m = _METRIC_LINE.match(line)
        if not m:
            continue
        bl = _BUCKET_LABEL.search(m.group("labels"))
        if not bl:
            continue
        bid = bl.group(1)
        val = float(m.group("value"))
        agg = out.setdefault(bid, {"disk_bytes": 0.0, "series": 0.0, "measurements": 0.0})
        name = m.group("name")
        if name in _SUM_BYTES:
            agg["disk_bytes"] += val
        elif name in _DIRECT:
            agg[_DIRECT[name]] = val
    return out


def bucket_names() -> dict[str, str]:
    """Map bucket id → name via the buckets API (skips internal _-buckets)."""
    import json
    raw = _req("/api/v2/buckets?limit=100",
               headers={"Authorization": f"Token {INFLUX_TOKEN}"})
    data = json.loads(raw)
    return {b["id"]: b["name"] for b in data.get("buckets", [])
            if not b["name"].startswith("_")}


def points_in_window(bucket: str) -> int:
    """Count points written to `bucket` in the last INTERVAL_S seconds."""
    flux = (f'from(bucket:"{bucket}")\n'
            f'  |> range(start: -{INTERVAL_S}s)\n'
            f'  |> group()\n'
            f'  |> count()\n'
            f'  |> keep(columns:["_value"])')
    body = flux.encode("utf-8")
    try:
        raw = _req(f"/api/v2/query?org={INFLUX_ORG}", method="POST", data=body,
                   headers={"Authorization": f"Token {INFLUX_TOKEN}",
                            "Content-Type": "application/vnd.flux",
                            "Accept": "application/csv"}).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # bucket unreadable / empty schema
        print(f"  ! count {bucket}: HTTP {e.code}", file=sys.stderr)
        return 0
    total = 0
    for row in raw.splitlines():
        row = row.strip()
        if not row or row.startswith("#") or row.startswith(",result"):
            continue
        # CSV columns: ,result,table,_value
        parts = row.split(",")
        if parts and parts[-1].isdigit():
            total += int(parts[-1])
    return total


def line_protocol(name: str, stats: dict, pps: float, ts: int) -> str:
    fields = (f"disk_bytes={int(stats['disk_bytes'])}i,"
              f"series={int(stats['series'])}i,"
              f"measurements={int(stats['measurements'])}i,"
              f"points_per_sec={pps:.4f}")
    return f"bucket_stats,bucket={name} {fields} {ts}"


def write_lines(lines: list[str]) -> None:
    body = "\n".join(lines).encode("utf-8")
    _req(f"/api/v2/write?org={INFLUX_ORG}&bucket={OPS_BUCKET}&precision=s",
         method="POST", data=body,
         headers={"Authorization": f"Token {INFLUX_TOKEN}",
                  "Content-Type": "text/plain; charset=utf-8"})


def main() -> int:
    if not INFLUX_TOKEN:
        print("ERROR: INFLUX_TOKEN unset (need read-all + write-ops).", file=sys.stderr)
        return 2
    ts = int(time.time())
    metrics = scrape_metrics()
    names = bucket_names()
    lines: list[str] = []
    for bid, name in sorted(names.items(), key=lambda kv: kv[1]):
        stats = metrics.get(bid)
        if stats is None:  # bucket exists but has no storage shards yet
            stats = {"disk_bytes": 0.0, "series": 0.0, "measurements": 0.0}
        pps = points_in_window(name) / INTERVAL_S
        lines.append(line_protocol(name, stats, pps, ts))
        print(f"  {name:16s} {int(stats['disk_bytes']):>12d}B "
              f"series={int(stats['series']):>6d} {pps:8.2f} pts/s")
    if not lines:
        print("ERROR: no buckets resolved — nothing written.", file=sys.stderr)
        return 1
    write_lines(lines)
    print(f"Wrote {len(lines)} bucket_stats points to `{OPS_BUCKET}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
