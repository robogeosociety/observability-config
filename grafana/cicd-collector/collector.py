#!/usr/bin/env python3
"""cicd-collector → InfluxDB `cicd` bucket: every GitHub Actions pipeline in the org.

Discovery is dynamic — the collector lists every non-archived repo in GH_ORG each
poll (plus GH_EXTRA_REPOS for anything still user-owned), so a new repo or a new
workflow file shows up on the dashboard within one poll cycle, no config edit.
Runs on the host under launchd (same pattern as claude-usage / walksheds-uptime:
the home InfluxDB is localhost-only, and launchd jobs must live on the internal
disk because TCC blocks /Volumes). Three measurements:

  workflow_run        — one point per *completed* run, at its completion time.
                        tags: org, repo, workflow, branch, event, conclusion
                        fields: ok, duration_s, queue_s, run_attempt, run_id
  workflow_inventory  — one point per workflow file per poll (the pipeline map,
                        including pipelines that have never run in the window).
                        tags: org, repo, workflow, state, path; field: present
  collector_poll      — one heartbeat per poll: repos/workflows seen, runs
                        written, API calls, errors, rate-limit remaining.

Rewriting a run is idempotent: identical measurement+tags+timestamp overwrites in
place, so the poll window deliberately overlaps the previous one (OVERLAP_MIN)
instead of tracking per-run cursors. A re-run (run_attempt bump) lands as a new
point at its own completion time — attempt history is kept, `last()` gives the
dashboard the current verdict.

Config from `.env` beside this file (see .env.example). `--dry-run` prints line
protocol instead of writing, so it can be exercised anywhere with outbound HTTPS.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
TIMEOUT = 30
PER_PAGE = 100
MAX_PAGES = 10  # per endpoint per poll — bounds a runaway backfill


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


def fmt_field(v):
    """Line-protocol field value: ints get the i suffix, floats/others pass through."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    return str(v)


def lp(measurement, tags, fields, ts):
    """Build one line-protocol record (second precision). Empty tag values drop."""
    def esc(v):
        return str(v).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")
    tagstr = "".join(f",{esc(k)}={esc(v)}" for k, v in tags.items() if v not in (None, ""))
    fieldstr = ",".join(f"{k}={fmt_field(v)}" for k, v in fields.items() if v is not None)
    return f"{measurement}{tagstr} {fieldstr} {ts}"


# ---------------------------------------------------------------- GitHub side

class GitHub:
    """Thin paginating client; counts calls and tracks the rate-limit header."""

    def __init__(self, env):
        self.token = env.get("GH_TOKEN", "")
        self.calls = 0
        self.rate_remaining = None

    def get(self, path, params=None):
        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(
            f"{API}{path}{qs}",
            headers={
                "User-Agent": "cicd-collector/1.0",
                "Accept": "application/vnd.github+json",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            })
        self.calls += 1
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            rem = resp.headers.get("X-RateLimit-Remaining")
            if rem is not None:
                self.rate_remaining = int(rem)
            return json.loads(resp.read())

    def paginate(self, path, params=None, key=None):
        """Yield items across pages until a short page or MAX_PAGES."""
        params = dict(params or {})
        params["per_page"] = PER_PAGE
        for page in range(1, MAX_PAGES + 1):
            params["page"] = page
            data = self.get(path, params)
            items = data.get(key, []) if key else data
            yield from items
            if len(items) < PER_PAGE:
                return


def discover_repos(gh, env):
    """Every non-archived repo in the org, plus GH_EXTRA_REPOS (owner/name csv)."""
    repos = [r["full_name"] for r in gh.paginate(f"/orgs/{env['GH_ORG']}/repos",
                                                 {"type": "all"})
             if not r.get("archived")]
    extra = [r.strip() for r in env.get("GH_EXTRA_REPOS", "").split(",") if r.strip()]
    return repos + [r for r in extra if r not in repos]


# ------------------------------------------------------------- pure mappers

def run_to_point(run, org, workflow_names=None):
    """Map one workflow-run API object to a line-protocol record.

    Only completed runs land (an in-progress run has no conclusion — it will be
    picked up on a later poll once it completes, timestamped at completion).
    The workflow tag prefers the inventory's file-level name over run["name"],
    which a `run-name:` override can repaint per run.
    """
    if run.get("status") != "completed" or not run.get("conclusion"):
        return None
    finished = iso_to_epoch(run["updated_at"])
    started = iso_to_epoch(run["run_started_at"]) if run.get("run_started_at") else finished
    created = iso_to_epoch(run["created_at"]) if run.get("created_at") else started
    workflow = (workflow_names or {}).get(run.get("workflow_id")) or run.get("name") or "unknown"
    repo = run["repository"]["name"] if run.get("repository") else None
    return lp("workflow_run",
              {"org": org, "repo": repo, "workflow": workflow,
               "branch": run.get("head_branch"), "event": run.get("event"),
               "conclusion": run["conclusion"]},
              {"ok": 1 if run["conclusion"] == "success" else 0,
               "duration_s": max(0, finished - started),
               "queue_s": max(0, started - created),
               "run_attempt": int(run.get("run_attempt") or 1),
               "run_id": int(run["id"])},
              finished)


def inventory_points(workflows, org, repo, now):
    """One point per workflow file — the pipeline map, run-or-not."""
    return [lp("workflow_inventory",
               {"org": org, "repo": repo, "workflow": wf.get("name") or wf.get("path"),
                "state": wf.get("state"), "path": wf.get("path")},
               {"present": 1, "workflow_id": int(wf["id"])},
               now)
            for wf in workflows]


def window_start(state, now_dt, backfill_days, overlap_min):
    """Poll window start: last successful poll minus overlap; first run backfills."""
    floor = now_dt - timedelta(days=backfill_days)
    last = state.get("last_poll")
    if not last:
        return floor
    start = datetime.fromisoformat(last) - timedelta(minutes=overlap_min)
    return max(start, floor)


# ----------------------------------------------------------------- plumbing

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def write_influx(env, lines):
    """Write line protocol to InfluxDB. Prefer the published HTTP port; fall back
    to the container CLI when host->container port-forwarding is unavailable."""
    payload = "\n".join(lines)
    url = (f"{env.get('INFLUX_URL', 'http://localhost:8086')}/api/v2/write"
           f"?org={env.get('INFLUX_ORG', 'home')}"
           f"&bucket={env.get('INFLUX_BUCKET', 'cicd')}&precision=s")
    req = urllib.request.Request(url, data=payload.encode(), method="POST",
                                 headers={"Authorization": f"Token {env['INFLUX_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return f"http {resp.status}"
    except Exception as e:
        import subprocess
        proc = subprocess.run(
            ["docker", "exec", "-i", env.get("INFLUX_CONTAINER", "influxdb"),
             "influx", "write", "--org", env.get("INFLUX_ORG", "home"),
             "--bucket", env.get("INFLUX_BUCKET", "cicd"),
             "--token", env["INFLUX_TOKEN"], "--precision", "s"],
            input=payload, text=True, capture_output=True, timeout=TIMEOUT)
        if proc.returncode != 0:
            raise RuntimeError(f"http write failed ({e}); docker fallback failed: {proc.stderr.strip()}")
        return f"docker-exec (http unavailable: {e})"


def poll(gh, env, since_iso, now):
    """One collection pass. Returns (lines, stats); per-repo failures are counted,
    not fatal — one flaky repo must not blank the whole org's poll."""
    org = env["GH_ORG"]
    lines, stats = [], {"repos": 0, "repos_with_workflows": 0, "workflows": 0,
                        "runs_written": 0, "errors": 0}
    try:
        repos = discover_repos(gh, env)
    except Exception as e:
        sys.exit(f"error: repo discovery for org {org} failed: {e}")
    for full in repos:
        stats["repos"] += 1
        repo = full.split("/", 1)[1]
        try:
            workflows = list(gh.paginate(f"/repos/{full}/actions/workflows", key="workflows"))
            if not workflows:
                continue
            stats["repos_with_workflows"] += 1
            stats["workflows"] += len(workflows)
            lines += inventory_points(workflows, org, repo, now)
            names = {wf["id"]: wf.get("name") or wf.get("path") for wf in workflows}
            for run in gh.paginate(f"/repos/{full}/actions/runs",
                                   {"created": f">={since_iso}"}, key="workflow_runs"):
                point = run_to_point(run, org, workflow_names=names)
                if point:
                    lines.append(point)
                    stats["runs_written"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"warning: {full}: {e}", file=sys.stderr)
    return lines, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print line protocol, don't write")
    args = ap.parse_args()
    env = load_env()
    if not env.get("GH_ORG"):
        sys.exit("GH_ORG is required (see .env.example)")

    state_file = env.get("STATE_FILE", os.path.join(HERE, "state.json"))
    state = load_state(state_file)
    now_dt = datetime.now(timezone.utc)
    now = int(now_dt.timestamp())
    start = window_start(state, now_dt,
                         int(env.get("BACKFILL_DAYS", "30")),
                         int(env.get("OVERLAP_MIN", "30")))
    since_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    gh = GitHub(env)
    t0 = time.monotonic()
    lines, stats = poll(gh, env, since_iso, now)
    lines.append(lp("collector_poll", {"org": env["GH_ORG"]},
                    {**stats, "api_calls": gh.calls,
                     "duration_ms": int((time.monotonic() - t0) * 1000),
                     "rate_remaining": gh.rate_remaining},
                    now))

    if args.dry_run:
        print("\n".join(lines))
        return
    code = write_influx(env, lines)
    # cursor only advances after a successful write, so a failed poll re-covers
    save_state(state_file, {"last_poll": now_dt.isoformat()})
    print(f"wrote {len(lines)} points to {env.get('INFLUX_BUCKET', 'cicd')} "
          f"(since {since_iso}, {code})")


if __name__ == "__main__":
    main()
