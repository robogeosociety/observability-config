"""Window z-score anomaly detector — the telemetry layer for Alerts & Anomalies.

Reads anomaly-detection.yaml, and for each metric pulls its series from InfluxDB,
computes a window z-score for the latest point, and writes an anomaly event to the
`ops` bucket (measurement `anomaly`) when |z| crosses a threshold. The dashboard
reads those events; a scheduled Claude pass reads the event history + the raw
metrics and proposes config tweaks via PR (store-as-code).

Pure functions (z_scores / score_latest / event_line) are unit-tested with no IO;
the InfluxDB query/write are thin wrappers. Mirrors the drift-sentinel layout
(internal-disk launchd job; see deploy.sh).
"""
import json
import math
import os
import pathlib
import time
import urllib.error
import urllib.request


def _load_env():
    envf = pathlib.Path(__file__).resolve().parent / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = pathlib.Path(os.environ.get("ANOMALY_CONFIG", HERE / "anomaly-detection.yaml"))
INFLUX_URL = os.environ.get("INFLUX_URL", "http://localhost:8086")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
# read scoped to the source buckets; write scoped to ops. One token with both is fine.
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", os.environ.get("INFLUX_OPS_TOKEN", ""))
OPS_BUCKET = os.environ.get("ANOMALY_BUCKET", "ops")


# --- pure functions (unit-tested) -----------------------------------------
def z_scores(values):
    """Population z-scores for a list of floats. Empty if <2 points or zero variance."""
    n = len(values)
    if n < 2:
        return []
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    sd = math.sqrt(var)
    if sd == 0:
        return []
    return [(v - mean) / sd for v in values]


def score_latest(values, z_warn, z_crit, min_points):
    """Score the most recent point against the window. Returns a dict or None.

    None = not enough baseline, zero variance, or |z| below z_warn (not anomalous)."""
    if len(values) < min_points:
        return None
    n = len(values)
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
    if sd == 0:
        return None
    latest = values[-1]
    z = (latest - mean) / sd
    if abs(z) < z_warn:
        return None
    return {
        "zscore": round(z, 3),
        "value": round(latest, 4),
        "mean": round(mean, 4),
        "stddev": round(sd, 4),
        "severity": "crit" if abs(z) >= z_crit else "warn",
        "direction": "high" if z > 0 else "low",
        "n": n,
    }


def event_line(metric, bucket, ev, ts_ns):
    """InfluxDB line protocol for one anomaly event."""
    tags = (f"anomaly,metric={metric},bucket={bucket},method=window_zscore,"
            f"severity={ev['severity']},direction={ev['direction']}")
    fields = (f"zscore={ev['zscore']},value={ev['value']},mean={ev['mean']},"
              f"stddev={ev['stddev']},n={ev['n']}i")
    return f"{tags} {fields} {ts_ns}"


# --- IO (thin) -------------------------------------------------------------
def query_series(flux):
    """Run Flux, return the _value column as a list of floats (time order)."""
    req = urllib.request.Request(
        f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}",
        data=flux.encode(),
        headers={"Authorization": f"Token {INFLUX_TOKEN}",
                 "Content-Type": "application/vnd.flux", "Accept": "application/csv"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = [ln for ln in r.read().decode().splitlines() if ln and not ln.startswith("#")]
    if len(rows) < 2:
        return []
    header = rows[0].split(",")
    try:
        vi = header.index("_value")
    except ValueError:
        return []
    out = []
    for ln in rows[1:]:
        cols = ln.split(",")
        if vi < len(cols) and cols[vi] != "":
            try:
                out.append(float(cols[vi]))
            except ValueError:
                pass
    return out


def write_lines(lines):
    if not lines:
        return
    body = "\n".join(lines).encode()
    req = urllib.request.Request(
        f"{INFLUX_URL}/api/v2/write?org={INFLUX_ORG}&bucket={OPS_BUCKET}&precision=ns",
        data=body,
        headers={"Authorization": f"Token {INFLUX_TOKEN}",
                 "Content-Type": "text/plain; charset=utf-8"})
    urllib.request.urlopen(req, timeout=15).read()


def load_config(path=CONFIG_PATH):
    import yaml
    return yaml.safe_load(pathlib.Path(path).read_text()) or {}


def run(now_ns=None):
    """One detection pass. Returns the list of anomaly events written."""
    cfg = load_config()
    d = cfg.get("defaults", {})
    window = d.get("window", "6h")
    every = d.get("every", "1m")
    now_ns = now_ns if now_ns is not None else time.time_ns()
    written = []
    lines = []
    for m in cfg.get("metrics", []):
        flux = m["flux"].replace("__WINDOW__", window).replace("__EVERY__", every)
        try:
            values = query_series(flux)
        except (urllib.error.URLError, OSError) as e:
            print(f"[skip] {m['name']}: query failed: {e}", flush=True)
            continue
        ev = score_latest(
            values,
            z_warn=float(m.get("z_warn", d.get("z_warn", 3.0))),
            z_crit=float(m.get("z_crit", d.get("z_crit", 4.0))),
            min_points=int(m.get("min_points", d.get("min_points", 30))),
        )
        if ev is None:
            continue
        lines.append(event_line(m["name"], m["bucket"], ev, now_ns))
        ev = dict(ev, metric=m["name"], bucket=m["bucket"])
        written.append(ev)
        print(f"[anomaly] {m['name']} z={ev['zscore']} {ev['severity']} ({ev['direction']})", flush=True)
    write_lines(lines)
    if not written:
        print("no anomalies this pass", flush=True)
    return written


if __name__ == "__main__":
    run()
