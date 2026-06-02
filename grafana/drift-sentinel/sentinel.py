#!/usr/bin/env python3
"""Drift sentinel — catch stale/degraded dashboard data and warn in Discord with
the suspect upstream commits.

For each dashboard in dashboards.index.yaml with a `source:` block we track
(status: active, with a produced InfluxDB bucket), this:
  1. checks data freshness + an anomaly ratio (recent write-rate vs trailing
     baseline) against InfluxDB,
  2. when stale/degraded, fetches the recent commits/merges that touched the
     source repo's `paths` (via `gh`),
  3. posts a Discord embed naming the dashboard, the symptom, and the likely
     cause (those commits).

Grafana can't do step 2 (alert templates can't fetch GitHub), which is why this
is a sidecar collector rather than a Grafana alert rule. It reuses the same
Discord webhook as the Grafana alerts.

Pure decision logic (parse_cadence/classify/build_embed) is unit-tested; I/O
(InfluxDB, gh, Discord) is thin and mockable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

# --- config ---------------------------------------------------------------
def _load_env():
    """Minimal .env loader (real environment wins, so launchd/CI can override)."""
    envf = Path(__file__).resolve().parent / ".env"
    if not envf.exists():
        return
    for line in envf.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

REPO = Path(__file__).resolve().parent.parent.parent          # observability-config root
INDEX = Path(os.environ.get("INDEX_PATH", REPO / "grafana" / "dashboards.index.yaml"))
INFLUX_URL = os.environ.get("INFLUX_URL", "http://localhost:8086")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
INFLUX_TOKEN = os.environ.get("INFLUX_READ_TOKEN", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = Path(os.environ.get(
    "DRIFT_STATE", Path.home() / ".local/share/drift-sentinel/state.json"))
REALERT_AFTER_S = int(os.environ.get("REALERT_AFTER_S", 6 * 3600))   # throttle repeats
DEGRADED_RATIO = float(os.environ.get("DEGRADED_RATIO", 0.4))        # <40% of baseline = degraded
# gh lives outside launchd's minimal PATH — make it findable.
os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:" \
    + str(Path.home() / ".local/bin")


# --- pure logic (unit-tested) ---------------------------------------------
def parse_cadence(s: str | None) -> int | None:
    """'15s'|'5m'|'24h' -> seconds. None/unknown -> None."""
    if not s or not isinstance(s, str):
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        return int(s[:-1]) * units[s[-1]]
    except (ValueError, KeyError, IndexError):
        return None


def classify(recent: int, recent_window_s: int, base: int, base_window_s: int,
             degraded_ratio: float = DEGRADED_RATIO,
             check_ratio: bool = True) -> tuple[str, dict]:
    """Decide ok|stale|degraded from point counts. Pure.

    `check_ratio` gates the rate-anomaly test: it's only meaningful for
    continuously-writing buckets. For daily/bursty buckets a recent-vs-trailing
    rate comparison is noise, so staleness (recent==0) is the only signal.
    """
    detail = {"recent": recent, "recent_window_s": recent_window_s}
    if recent == 0:
        return "stale", detail
    if not check_ratio:
        return "ok", detail
    recent_rate = recent / recent_window_s
    base_rate = (base / base_window_s) if base_window_s else 0
    ratio = (recent_rate / base_rate) if base_rate > 0 else 1.0
    detail["ratio"] = round(ratio, 2)
    if base_rate > 0 and ratio < degraded_ratio:
        return "degraded", detail
    return "ok", detail


# A bucket is "continuous" (rate-anomaly meaningful) only if it writes at least
# every couple of minutes; slower than that we trust staleness alone.
CONTINUOUS_CADENCE_S = 120


def build_embed(uid: str, title: str, bucket: str, state: str, detail: dict,
                repo: str | None, commits: list[dict]) -> dict:
    """Discord embed payload. Pure (commits already fetched)."""
    color = 0xE0245E if state == "stale" else 0xF5A623           # red / amber
    sym = ("no new data" if state == "stale"
           else f"write-rate at {int(detail.get('ratio', 0) * 100)}% of baseline")
    fields = [
        {"name": "Dashboard", "value": f"{title} (`{uid}`)", "inline": True},
        {"name": "Bucket", "value": f"`{bucket}`", "inline": True},
        {"name": "Symptom", "value": sym, "inline": False},
    ]
    if repo and commits:
        lines = "\n".join(
            f"[`{c['sha'][:7]}`](https://github.com/{repo}/commit/{c['sha']}) "
            f"{c['msg']} — {c['author']}" for c in commits[:5])
        fields.append({"name": f"Recent commits in {repo}", "value": lines or "—", "inline": False})
    elif repo:
        fields.append({"name": f"Recent commits in {repo}",
                       "value": "none in the lookback window", "inline": False})
    return {
        "embeds": [{
            "title": f"⚠️ Dashboard data {state}: {title}",
            "color": color,
            "fields": fields,
            "footer": {"text": "drift-sentinel"},
        }]
    }


# --- I/O (thin) -----------------------------------------------------------
def flux_count(bucket: str, window_s: int) -> int:
    flux = (f'from(bucket: "{bucket}") |> range(start: -{window_s}s) '
            f'|> group() |> count() |> keep(columns: ["_value"])')
    req = urllib.request.Request(
        f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}",
        data=flux.encode(),
        headers={"Authorization": f"Token {INFLUX_TOKEN}",
                 "Content-Type": "application/vnd.flux", "Accept": "application/csv"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rows = [ln for ln in r.read().decode().splitlines() if ln and not ln.startswith("#")]
    # CSV: header then one data row; _value is the last column.
    if len(rows) < 2:
        return 0
    try:
        return int(rows[-1].split(",")[-1])
    except ValueError:
        return 0


def recent_commits(repo: str, paths: list[str], since_days: int = 14) -> list[dict]:
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - since_days * 86400))
    seen, out = set(), []
    for path in (paths or [""]):
        try:
            res = subprocess.run(
                ["gh", "api", f"repos/{repo}/commits",
                 "-X", "GET", "-f", f"since={since}", "-f", f"path={path}", "-f", "per_page=5"],
                capture_output=True, text=True, timeout=20)
            if res.returncode != 0:
                continue
            for c in json.loads(res.stdout):
                sha = c["sha"]
                if sha in seen:
                    continue
                seen.add(sha)
                out.append({"sha": sha,
                            "msg": c["commit"]["message"].splitlines()[0][:80],
                            "author": c["commit"]["author"]["name"]})
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError):
            continue
    return out


def post_discord(payload: dict) -> None:
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 # Discord (Cloudflare) 403s the default Python-urllib UA.
                 "User-Agent": "drift-sentinel/0.1 (+observability-config)"})
    urllib.request.urlopen(req, timeout=15).read()


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


# --- orchestration --------------------------------------------------------
def tracked_sources(index: dict):
    """Yield (uid, entry, source) for entries the sentinel can check."""
    for uid, entry in (index or {}).items():
        src = (entry or {}).get("source") or {}
        if src.get("monitor") == "skip":            # usage/event-driven — opt out
            continue
        if src.get("status") == "active" and (src.get("produces") or {}).get("bucket"):
            yield uid, entry, src


def main() -> int:
    dry = "--dry-run" in sys.argv
    index = yaml.safe_load(INDEX.read_text())
    state = _load_state()
    now = time.time()
    findings = []

    for uid, entry, src in tracked_sources(index):
        bucket = src["produces"]["bucket"]
        cadence = parse_cadence(src.get("cadence")) or 900
        window = max(3 * cadence, 900)
        recent = flux_count(bucket, window)
        base = flux_count(bucket, 86400)
        st, detail = classify(recent, window, base, 86400,
                              check_ratio=cadence <= CONTINUOUS_CADENCE_S)
        findings.append((uid, entry, src, bucket, st, detail))
        if st == "ok":
            continue
        # throttle repeats per uid+state
        key = f"{uid}:{st}"
        if not dry and now - state.get(key, 0) < REALERT_AFTER_S:
            print(f"[skip] {uid} {st} (throttled)")
            continue
        repo = src.get("repo")
        commits = recent_commits(repo, src.get("paths", [])) if repo else []
        payload = build_embed(uid, entry.get("title", uid), bucket, st, detail, repo, commits)
        if dry:
            print(f"[{st}] {uid}: {json.dumps(payload['embeds'][0]['fields'])}")
        else:
            post_discord(payload)
            state[key] = now
            print(f"[alert] {uid} {st} -> Discord")

    if not dry:
        _save_state(state)
    oks = sum(1 for f in findings if f[4] == "ok")
    print(f"checked {len(findings)} tracked pipelines: {oks} ok, "
          f"{len(findings) - oks} stale/degraded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
