#!/usr/bin/env python3
"""walksheds-uptime collector → InfluxDB `ops` bucket (tag service=walksheds).

Runs on the host under launchd (the home InfluxDB is localhost/LAN/tailnet only,
so GitHub-hosted CI can't write to it — this collector bridges that, matching the
dev-status pattern). Three signals:

  uptime   — synthetic HTTP probe of the live site (up, latency_ms, status)
  ci_smoke — latest "Live smoke" workflow run conclusion (ok, duration_s)
  deploy   — recent github-pages deployment_status events (ok, by state)

Config from `.env` beside this file (see .env.example). `--dry-run` prints the
line protocol instead of writing (and skips the InfluxDB call), so it can be
exercised anywhere with outbound HTTPS.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_URL = "https://walksheds.xyz/"
WORKFLOW_FILE = "smoke.yml"
DEPLOY_ENV = "github-pages"
TIMEOUT = 20


def load_env():
    path = os.path.join(HERE, ".env")
    env = dict(os.environ)
    if os.path.exists(path):
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def iso_to_epoch(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def lp(measurement, tags, fields, ts):
    """Build one line-protocol record (second precision)."""
    def esc(v):
        return str(v).replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")
    tagstr = "".join(f",{esc(k)}={esc(v)}" for k, v in tags.items() if v not in (None, ""))
    fieldstr = ",".join(f"{k}={v}" for k, v in fields.items())
    return f"{measurement}{tagstr} {fieldstr} {ts}"


def http_get(url, token=None, accept=None):
    headers = {"User-Agent": "walksheds-uptime/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read()


def probe_site(now):
    t0 = time.monotonic()
    try:
        status, _ = http_get(SITE_URL)
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        status = 0
    latency_ms = round((time.monotonic() - t0) * 1000, 1)
    up = 1 if 200 <= status < 400 else 0
    return [lp("uptime", {"service": "walksheds", "source": "synthetic"},
              {"up": up, "latency_ms": latency_ms, "status": status}, now)]


def gh_json(env, path):
    url = f"https://api.github.com/repos/{env['GH_REPO']}/{path}"
    try:
        status, body = http_get(url, token=env.get("GH_TOKEN"),
                                accept="application/vnd.github+json")
    except urllib.error.HTTPError as e:
        return None  # missing workflow (404 before merge) etc. — skip this signal
    return json.loads(body) if status == 200 else None


def ci_smoke_points(env):
    data = gh_json(env, f"actions/workflows/{WORKFLOW_FILE}/runs?per_page=10")
    out = []
    for run in (data or {}).get("workflow_runs", []):
        if run.get("status") != "completed":
            continue
        ok = 1 if run.get("conclusion") == "success" else 0
        ts = iso_to_epoch(run["updated_at"])
        dur = max(0, iso_to_epoch(run["updated_at"]) - iso_to_epoch(run["run_started_at"]))
        out.append(lp("ci_smoke",
                      {"service": "walksheds", "conclusion": run.get("conclusion")},
                      {"ok": ok, "duration_s": dur, "run_id": run["id"]}, ts))
    return out


def deploy_points(env):
    deployments = gh_json(env, f"deployments?environment={DEPLOY_ENV}&per_page=10")
    out = []
    for d in deployments or []:
        statuses = gh_json(env, f"deployments/{d['id']}/statuses?per_page=1")
        if not statuses:
            continue
        st = statuses[0]
        ok = 1 if st["state"] == "success" else 0
        ts = iso_to_epoch(st["created_at"])
        out.append(lp("deploy",
                      {"service": "walksheds", "environment": DEPLOY_ENV, "state": st["state"]},
                      {"ok": ok, "deploy_id": d["id"]}, ts))
    return out


def write_influx(env, lines):
    """Write line protocol to InfluxDB. Prefer the published HTTP port; fall back
    to the container CLI when host->container port-forwarding is unavailable
    (OrbStack publishes 8086, but that path can be flaky from headless contexts)."""
    payload = "\n".join(lines)
    url = (f"{env['INFLUX_URL']}/api/v2/write?org={env['INFLUX_ORG']}"
           f"&bucket={env['INFLUX_BUCKET']}&precision=s")
    req = urllib.request.Request(url, data=payload.encode(), method="POST",
                                 headers={"Authorization": f"Token {env['INFLUX_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return f"http {resp.status}"
    except Exception as e:
        import subprocess
        proc = subprocess.run(
            ["docker", "exec", "-i", env.get("INFLUX_CONTAINER", "influxdb"),
             "influx", "write", "--org", env["INFLUX_ORG"], "--bucket", env["INFLUX_BUCKET"],
             "--token", env["INFLUX_TOKEN"], "--precision", "s"],
            input=payload, text=True, capture_output=True, timeout=TIMEOUT)
        if proc.returncode != 0:
            raise RuntimeError(f"http write failed ({e}); docker fallback failed: {proc.stderr.strip()}")
        return f"docker-exec (http unavailable: {e})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print line protocol, don't write")
    args = ap.parse_args()
    env = load_env()
    now = int(time.time())

    lines = probe_site(now)
    try:
        lines += ci_smoke_points(env)
        lines += deploy_points(env)
    except Exception as e:  # GitHub hiccup shouldn't drop the uptime point
        print(f"warning: GitHub poll failed: {e}", file=sys.stderr)

    if args.dry_run:
        print("\n".join(lines))
        return
    code = write_influx(env, lines)
    print(f"wrote {len(lines)} points to {env['INFLUX_BUCKET']} (HTTP {code})")


if __name__ == "__main__":
    main()
