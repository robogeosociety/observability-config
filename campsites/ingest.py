#!/usr/bin/env python3
"""
Ingest campsite-availability summaries from R2 → InfluxDB (`campsites` bucket).

The raw-collection job (robot-geographical-society backend Worker) scrapes
recreation.gov + WA State Parks via Browser Rendering and writes
`summary/<date>/<id>.json` to the `campsite-raw` R2 bucket. This — the
observability side — pulls those summaries and writes one time-series point per
(campsite, target_date), feeding the Campsite Availability dashboard's burn-down
/ sell-out projection. No scraping here; download + write only.

R2 is read through the **existing wrangler OAuth session** (Cloudflare R2 API,
`GET /accounts/{acct}/r2/buckets/{bucket}/objects[...]`) — no S3 key, no token to
mint or paste. The access token (~1h) is auto-refreshed via `wrangler whoami` on
a 401; if the session is truly dead the run fails and the campsite-collector-stale
alert fires (run `wrangler login` to fix). Stdlib only.

Stdlib only — no deps:
    python3 ingest.py
    python3 ingest.py --date 2026-06-01 --dry-run
The launchd job runs `python3` from the INTERNAL disk (deploy.sh): under launchd's
background session, `uv run`/CPython TLS hangs and reading the script off /Volumes
hits the TCC block and hangs — so the runner lives on the internal disk and all
HTTPS goes through curl.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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


def _night_s(d):
    """'YYYY-MM-DD' night -> epoch SECONDS at 00:00 UTC (the write uses precision=s)."""
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def build_site_lines(rec):
    """Per-SITE availability from a `sites/<date>/<id>.json` record.

    Models the night (target_date) as the POINT TIMESTAMP, not a tag — so series
    cardinality is bounded by the number of sites (~11k), not sites×nights (~2M)
    which would strain the 2GB InfluxDB. Re-runs upsert (last-write-wins per
    series+night), keeping the latest status per (site, night). Powers the
    cascading loop/site dropdowns + the per-site calendar on the campsite dash.
    """
    name = rec.get("name", ""); agency = rec.get("agency", "")
    lines = []
    for sid, s in (rec.get("sites") or {}).items():
        loop = s.get("loop") or "—"
        site = s.get("label") or str(sid)
        typ = s.get("type") or "—"
        tags = (f"name={_esc(name)},agency={_esc(agency)},loop={_esc(loop)},"
                f"site={_esc(site)},type={_esc(typ)}")
        for d, status in (s.get("by_date") or {}).items():
            try:
                ts = _night_s(d)
            except (ValueError, TypeError):
                continue
            av = 1 if status == "available" else 0
            rs = 1 if status == "reserved" else 0
            lines.append(f"site_availability,{tags} available={av}i,reserved={rs}i {ts}")
    return lines


def build_demand_lines(rec):
    """Pre-aggregated demand summaries from a `sites/` record — so the demand
    dashboard never aggregates the ~1.5M site_availability points live (too slow /
    OOM-risky on the 2GB Influx). Two small measurements:

    - `site_demand`  : per site, available_nights/reserved_nights over the window
                       (_time = collected night, so re-runs upsert). Top-sites list.
    - `campground_demand`: per campground per night, available/reserved site counts
                       (_time = the night). Powers the heatmap + hottest-nights +
                       top-campgrounds.
    """
    name = rec.get("name", ""); agency = rec.get("agency", "")
    collected = rec.get("collected_date")
    lines = []
    night_av, night_rs = {}, {}
    try:
        cts = _night_s(collected)
    except (ValueError, TypeError):
        cts = int(time.time())
    for sid, s in (rec.get("sites") or {}).items():
        loop = s.get("loop") or "—"
        site = s.get("label") or str(sid)
        av = rs = 0
        for d, status in (s.get("by_date") or {}).items():
            a = 1 if status == "available" else 0
            r = 1 if status == "reserved" else 0
            av += a; rs += r
            night_av[d] = night_av.get(d, 0) + a
            night_rs[d] = night_rs.get(d, 0) + r
        tags = f"name={_esc(name)},agency={_esc(agency)},loop={_esc(loop)},site={_esc(site)}"
        lines.append(f"site_demand,{tags} available_nights={av}i,reserved_nights={rs}i,"
                     f"total_nights={av + rs}i {cts}")
    for d in night_av:
        try:
            ts = _night_s(d)
        except (ValueError, TypeError):
            continue
        lines.append(f"campground_demand,name={_esc(name)},agency={_esc(agency)} "
                     f"available={night_av[d]}i,reserved={night_rs[d]}i {ts}")
    return lines


# --- prediction readiness (PREDICT.md §10 / checkpoint D) -------------------
# A 0–1 gauge that fills as collection history deepens, gating when sell-out
# predictions are trustworthy. Computed here (observability owns InfluxDB
# emission) by scanning the R2 sites/ history. The formula mirrors the
# robot-geographical-society `predict/readiness.py` module and must stay in
# lockstep with it: (events · coverage · depth)^⅓, a geometric mean so one
# lagging component holds the gauge down.
EVENTS_TARGET = 500
DEPTH_TARGET = 6
READINESS_BANDS = ((0.33, "insufficient"), (0.80, "directional"), (1.01, "reliable"))


def _band(score):
    return next(name for edge, name in READINESS_BANDS if score < edge)


def compute_readiness(bucket, max_days=60):
    """Scan sites/ history → per-cell at-risk intervals → readiness dict.

    Bounded to the most recent `max_days` collection dates so the daily job stays
    cheap as history grows (that window is also the right horizon for "is there
    enough *recent* depth to predict"). An at-risk interval is a consecutive pair
    of snapshots whose earlier status is `available`; the first `available ->
    reserved` flip is the sell-out event (mirrors the C0 builder, which stops a
    cell at its first sell-out)."""
    by_date = {}
    for key, _lm in r2_list(bucket, "sites/"):
        parts = key.split("/")
        if len(parts) >= 3 and parts[0] == "sites" and parts[-1].endswith(".json"):
            by_date.setdefault(parts[1], []).append(key)
    dates = sorted(by_date)[-max_days:]

    cells = {}  # (campground, site, target_date) -> {collected_date: status}
    for d in dates:
        for key in by_date[d]:
            rec = json.loads(r2_get(bucket, key))
            cg = str(rec.get("id"))
            cd = rec.get("collected_date") or d
            for sid, s in (rec.get("sites") or {}).items():
                for tgt, status in (s.get("by_date") or {}).items():
                    cells.setdefault((cg, str(sid), tgt), {})[cd] = status

    total = len(cells); active = 0; events = 0; depths = []
    for seq in cells.values():
        days = sorted(seq)
        intervals = 0; sold = False
        for i in range(len(days) - 1):
            if seq[days[i]] != "available":
                continue
            intervals += 1
            nxt = seq[days[i + 1]]
            if nxt == "reserved":
                sold = True; break
            if nxt == "other":
                break
        if intervals > 0:
            active += 1; depths.append(intervals)
        if sold:
            events += 1

    median_depth = sorted(depths)[len(depths) // 2] if depths else 0
    event_score = min(1.0, events / EVENTS_TARGET) if EVENTS_TARGET else 0.0
    coverage = active / total if total else 0.0
    depth_score = min(1.0, median_depth / DEPTH_TARGET) if DEPTH_TARGET else 0.0
    score = (event_score * coverage * depth_score) ** (1 / 3)
    return {"readiness": score, "band": _band(score), "event_score": event_score,
            "coverage": coverage, "depth_score": depth_score, "events": events,
            "active_cells": active, "cells": total, "median_depth": median_depth}


def build_readiness_line(stats, ts):
    """One global `predict_readiness` point (no tags) feeding the gauge."""
    return [f"predict_readiness "
            f"readiness={stats['readiness']:.4f},event_score={stats['event_score']:.4f},"
            f"coverage={stats['coverage']:.4f},depth_score={stats['depth_score']:.4f},"
            f"events={int(stats['events'])}i,active_cells={int(stats['active_cells'])}i,"
            f"cells={int(stats['cells'])}i,median_depth={int(stats['median_depth'])}i,"
            f"band=\"{stats['band']}\" {ts}"]


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
    # duration_s is an INTEGER on the shared `collector` measurement (the
    # coordinator/backup heartbeats set the type first); a float here is a type
    # conflict → 422. Match it.
    line = (f"collector,source={_esc(source)},kind={_esc(kind)},host={_esc(host)} "
            f"success={1 if success else 0}i,duration_s={int(duration_s)}i,"
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


CF_API = "https://api.cloudflare.com/client/v4"


def _wrangler_token() -> str:
    """The OAuth access token from the local `wrangler login` session."""
    import glob
    for p in sorted(glob.glob(os.path.expanduser("~/Library/Preferences/.wrangler/config/*.toml"))):
        for line in Path(p).read_text().splitlines():
            if line.strip().startswith("oauth_token"):
                return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("No wrangler OAuth token — run `wrangler login`.")


def _refresh_wrangler():
    """Trigger wrangler to refresh its (expired) access token; wrangler owns the
    refresh_token rotation, so we let it manage the lifecycle."""
    try:
        subprocess.run(["npx", "-y", "wrangler@latest", "whoami"],
                       capture_output=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"wrangler refresh failed (non-fatal): {e}", file=sys.stderr)


def _cf(path: str, raw: bool = False, _retried: bool = False):
    """GET the Cloudflare API via curl. (CPython's HTTPS/TLS hangs under launchd's
    session — curl doesn't.) Refresh the OAuth token + retry once on 401/403."""
    import tempfile
    fd, tmp = tempfile.mkstemp()
    os.close(fd)
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "45", "-o", tmp, "-w", "%{http_code}",
             f"{CF_API}/{path}", "-H", f"Authorization: Bearer {_wrangler_token()}"],
            capture_output=True, timeout=60)
        code = r.stdout.decode().strip()
        if code in ("401", "403") and not _retried:
            _refresh_wrangler()
            return _cf(path, raw, _retried=True)
        if not code.startswith("2"):
            raise SystemExit(f"CF API {path} -> HTTP {code or 'none'}: {r.stderr.decode()[:200]}")
        data = Path(tmp).read_bytes()
        return data if raw else json.loads(data)
    finally:
        os.unlink(tmp)


def _acct() -> str:
    return os.environ.get("R2_ACCOUNT_ID", "d7adee58513c1b2f770ccaac90cf114f")


def r2_list(bucket: str, prefix: str):
    """[(key, last_modified)] under prefix, paginating the R2 objects API."""
    out, cursor = [], ""
    while True:
        q = f"prefix={urllib.parse.quote(prefix)}&per_page=1000"
        if cursor:
            q += f"&cursor={urllib.parse.quote(cursor)}"
        d = _cf(f"accounts/{_acct()}/r2/buckets/{bucket}/objects?{q}")
        out += [(o["key"], o.get("last_modified")) for o in (d.get("result") or [])]
        info = d.get("result_info") or {}
        cursor = info.get("cursor") or ""
        if not info.get("is_truncated"):
            return out


def r2_get(bucket: str, key: str) -> bytes:
    return _cf(f"accounts/{_acct()}/r2/buckets/{bucket}/objects/{urllib.parse.quote(key)}", raw=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-sites", action="store_true",
                    help="skip per-site (sites/) ingest into site_availability")
    ap.add_argument("--no-readiness", action="store_true",
                    help="skip the prediction-readiness gauge (predict_readiness)")
    ap.add_argument("--readiness-only", action="store_true",
                    help="compute only the readiness gauge (skip availability/sites)")
    ap.add_argument("--readiness-max-days", type=int, default=60,
                    help="recent sites/ collection dates to scan for readiness")
    a = ap.parse_args()

    _load_env(Path(__file__).resolve().parent / ".env")
    bucket = os.environ.get("R2_CAMPSITE_BUCKET", "campsite-raw")
    prefix = f"summary/{a.date}/"

    lines, n = [], 0
    if not a.readiness_only:
        objs = r2_list(bucket, prefix)
        print(f"{len(objs)} summaries under {prefix}", file=sys.stderr)
        for key, last_modified in objs:
            rec = json.loads(r2_get(bucket, key))
            # true scrape time = the object's last-modified (ISO-8601 with Z)
            ts = (int(datetime.fromisoformat(last_modified.replace("Z", "+00:00")).timestamp())
                  if last_modified else int(time.time()))
            lines += build_lines(rec["id"], rec["name"], rec.get("agency", ""), rec.get("by_date", {}), ts)
            n += 1
        print(f"{n} campsites · {len(lines)} points", file=sys.stderr)

        # Per-site availability (sites/<date>/) → site_availability + demand.
        if not a.no_sites:
            sprefix = f"sites/{a.date}/"
            sobjs = r2_list(bucket, sprefix)
            sn = 0
            for key, _lm in sobjs:
                rec = json.loads(r2_get(bucket, key))
                lines += build_site_lines(rec)
                lines += build_demand_lines(rec)
                sn += 1
            print(f"{sn} site files · {len(lines)} total points", file=sys.stderr)

    # Prediction-readiness gauge (PREDICT.md §10) — scans the sites/ history.
    if not a.no_readiness:
        stats = compute_readiness(bucket, max_days=a.readiness_max_days)
        print(f"readiness {stats['readiness']:.3f} ({stats['band']}) — "
              f"events={stats['events']} active={stats['active_cells']}/{stats['cells']} "
              f"median_depth={stats['median_depth']}", file=sys.stderr)
        lines += build_readiness_line(stats, int(time.time()))

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
