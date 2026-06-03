"""Integration: every dashboard panel actually returns rows — not *No data*.

Repo-wide generalization of the campsites nodata check (#67). Self-skips if
Grafana is down (via grafana_get). For each dashboard we resolve its template
variables to the values Grafana would default to, interpolate them into every
panel query, and run the query through `/api/ds/query` over two windows:

- the dashboard's own default range — catches a variable defaulting to a dataless
  value (the `target_date=2026-05-01` bug);
- `now-24h` — catches a panel hardcoding its own window while the viewport is
  shorter (the collector-history `now()-INTERVAL '30' DAY` bug).

Health-aware: a query that *errors* on a **healthy** datasource is a real bug
(rule 3 — an unsupported SQL/Flux function); on an **unhealthy** one (token not
configured, e.g. the AE/R2 infinity panels) it's "can't evaluate here" and is
skipped, not failed.

Per-dashboard exceptions (legitimately-empty panels, inapplicable windows) live
in `dashboard_coverage.yaml`, not in code. A dashboard with no entry is strict.
"""

import json
import pathlib
import re
import urllib.error
import urllib.request

import pytest
import yaml

# fixtures (grafana_auth, grafana_get) come from conftest via pytest auto-discovery
GRAFANA_URL = "http://localhost:3001"
TESTS_DIR = pathlib.Path(__file__).parent
DASH_DIR = TESTS_DIR.parent / "provisioning" / "dashboards"
COVERAGE_FILE = TESTS_DIR / "dashboard_coverage.yaml"

pytestmark = pytest.mark.integration

DASHBOARDS = sorted(DASH_DIR.rglob("*.json"))
IDS = [f"{f.parent.name}/{f.name}" for f in DASHBOARDS]

# datasource types we can execute here (the R2/GraphQL infinity panels need an
# external Cloudflare token and aren't part of the nodata class we're guarding).
QUERYABLE = {"vertamedia-clickhouse-datasource", "influxdb"}

COVERAGE = yaml.safe_load(COVERAGE_FILE.read_text()) or {}


@pytest.fixture(scope="session")
def grafana_post(grafana_auth, grafana_get):
    # depending on grafana_get makes this skip the whole tier if Grafana is down
    def post(path, payload, timeout=20):
        req = urllib.request.Request(
            f"{GRAFANA_URL}{path}",
            data=json.dumps(payload).encode(),
            headers={"Authorization": grafana_auth, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {}
    return post


_HEALTH = {}


def _ds_healthy(grafana_get, uid):
    """Cached datasource health. A query error on a *healthy* datasource is a real
    bug (e.g. an unsupported function); on an unhealthy one it's just 'can't test
    here' (token not configured) and we skip."""
    if not uid:
        return False
    if uid not in _HEALTH:
        try:
            status, body = grafana_get(f"/api/datasources/uid/{uid}/health")
            _HEALTH[uid] = status == 200 and body.get("status") == "OK"
        except Exception:
            _HEALTH[uid] = False
    return _HEALTH[uid]


def _panels(dash):
    for p in dash.get("panels", []):
        yield p
        for sub in p.get("panels", []):
            yield sub


def _ds(panel, target):
    return target.get("datasource") or panel.get("datasource") or {}


_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


def _range_seconds(frm):
    """Rough seconds for a `now-<N><unit>` literal (for windowPeriod sizing)."""
    if isinstance(frm, str) and frm.startswith("now-") and frm[-1] in _UNIT_S:
        try:
            return int(frm[4:-1]) * _UNIT_S[frm[-1]]
        except ValueError:
            pass
    return 6 * 3600


def _run(post, target, ds, frm, to):
    """Run one target. Returns (frames, error_or_None).

    Sizes maxDataPoints/intervalMs so Flux's `v.windowPeriod` (and the ClickHouse
    `$timeSeries` step) land ~500 points — otherwise InfluxDB returns a "too many
    datapoints, truncated" *warning* in the error slot. When frames come back we
    treat any such error as a soft warning (data is present) rather than a failure;
    only a frameless error is a real query failure."""
    q = {
        "refId": "A",
        "datasource": ds,
        "query": target["__query"],
        "rawQuery": True,
        "format": target.get("format", "time_series"),
        "maxDataPoints": 500,
        "intervalMs": max(_range_seconds(frm) // 500, 1) * 1000,
    }
    if ds.get("type") == "vertamedia-clickhouse-datasource":
        q["dateTimeColDataType"] = target.get("dateTimeColDataType", "timestamp")
        q["dateTimeType"] = target.get("dateTimeType", "DATETIME")
    status, body = post("/api/ds/query", {"queries": [q], "from": frm, "to": to})
    res = body.get("results", {}).get("A", {})
    frames = res.get("frames", [])
    err = res.get("error")
    # An error *with rows* is a soft warning (e.g. "truncated to 1001 points") —
    # the data is there. An error with *no usable rows* is a real query failure
    # (an unsupported function, a type conflict) and must surface, so allow_empty
    # can't paper over it.
    if err and not _has_rows(frames):
        return [], err
    if not frames and status != 200:
        return [], f"HTTP {status}"
    return frames, None


def _has_rows(frames):
    for fr in frames:
        for col in fr.get("data", {}).get("values", []):
            if any(v is not None for v in col):
                return True
    return False


def _resolve_vars(post, dash, frm, to):
    """Return {name: {'one': first_value, 'all': [values]}} for template variables.

    Handles `query` vars (run the query) and `custom`/`constant`/`textbox` vars
    (read the configured default). A var whose own query errors is left out — the
    interpolation just leaves its `${name}` token in place, which still exercises
    the panel as Grafana would on a default load."""
    out = {}
    for v in dash.get("templating", {}).get("list", []):
        vtype = v.get("type")
        name = v.get("name")
        # is_all: multi / includeAll vars default to "All" — :regex must become a
        # match-all wildcard, not an alternation of raw (possibly /-containing)
        # values that would break the Flux /.../ literal.
        is_all = bool(v.get("multi") or v.get("includeAll"))
        if vtype == "query":
            ds = v.get("datasource") or {}
            frames, err = _run(post, {"__query": v["query"], "format": "table"}, ds, frm, to)
            if err:
                continue
            vals = []
            for fr in frames:
                cols = fr.get("data", {}).get("values", [])
                if cols:
                    vals.extend(x for x in cols[0] if x is not None)
            if vals:
                out[name] = {"one": vals[0], "all": vals, "is_all": is_all}
        elif vtype in ("custom", "constant", "textbox"):
            # default selection: current.value, else first option, else "".
            cur = (v.get("current") or {}).get("value")
            if isinstance(cur, list):
                cur = cur[0] if cur else None
            if cur in (None, "$__all"):
                opts = [o.get("value") for o in v.get("options", []) if o.get("value") not in (None, "$__all")]
                cur = opts[0] if opts else v.get("query", "")
            out[name] = {"one": cur, "all": [cur], "is_all": is_all}
    return out


def _interpolate(query, vars_):
    for name, vv in vars_.items():
        regex = ".*" if vv.get("is_all") else re.escape(str(vv["one"]))
        query = query.replace(f"${{{name}:json}}", json.dumps(vv["all"]))
        query = query.replace(f"${{{name}:csv}}", ",".join(str(x) for x in vv["all"]))
        query = query.replace(f"${{{name}:regex}}", regex)
        query = query.replace(f"${{{name}}}", str(vv["one"]))
    return query


def _cfg(uid):
    return COVERAGE.get(uid) or {}


def _allow_empty(cfg, panel):
    spec = cfg.get("allow_empty_panels") or []
    if "*" in spec:
        return True
    title = (panel.get("title") or "").lower()
    pid = panel.get("id")
    for s in spec:
        if isinstance(s, int) and s == pid:
            return True
        if isinstance(s, str) and s.lower() in title:
            return True
    return False


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_dashboard_panels_return_data(f, grafana_post, grafana_get):
    dash = json.loads(f.read_text())
    cfg = _cfg(dash.get("uid"))
    windows = cfg.get("windows", ["default", "short"])

    default_from = dash.get("time", {}).get("from", "now-30d")
    default_to = dash.get("time", {}).get("to", "now")
    vars_default = _resolve_vars(grafana_post, dash, default_from, default_to)

    evaluated = 0
    failures = []
    for p in _panels(dash):
        title = p.get("title") or ""
        for t in p.get("targets", []):
            if not isinstance(t, dict) or not t.get("query"):
                continue
            ds = _ds(p, t)
            if ds.get("type") not in QUERYABLE:
                continue
            allow_empty = _allow_empty(cfg, p)

            # --- default range: catches "variable defaults to a dataless value" ---
            t_def = dict(t, __query=_interpolate(t["query"], vars_default))
            frames, err = _run(grafana_post, t_def, ds, default_from, default_to)
            if err:
                if _ds_healthy(grafana_get, ds.get("uid")):
                    failures.append(f"[{title!r}] query ERROR on a healthy "
                                    f"datasource (broken query?): {err}")
                # else datasource unavailable (token not configured) — can't judge
                continue
            evaluated += 1
            default_had_rows = _has_rows(frames)
            if not default_had_rows and not allow_empty and "default" in windows:
                failures.append(
                    f"[{title!r}] No data over default range "
                    f"{default_from}..{default_to} (vars={ {k: v['one'] for k, v in vars_default.items()} })")
                continue

            # --- short range: catches "panel hardcodes a window > the viewport" ---
            if "short" not in windows:
                continue
            vars_short = _resolve_vars(grafana_post, dash, "now-24h", "now")
            t_short = dict(t, __query=_interpolate(t["query"], vars_short or vars_default))
            frames_s, err_s = _run(grafana_post, t_short, ds, "now-24h", "now")
            if err_s is None and default_had_rows and not _has_rows(frames_s) and not allow_empty:
                failures.append(
                    f"[{title!r}] has data at {default_from} but None at now-24h — "
                    f"panel isn't following the time picker (hardcoded window?)")

    if evaluated == 0:
        pytest.skip(f"{f.name}: no panel query could be executed "
                    "(datasource token not configured?)")
    assert not failures, f"{f.name}: nodata panels:\n  " + "\n  ".join(failures)
