"""Integration: every campsites panel actually returns rows — not *No data*.

Self-skips if Grafana is down (via grafana_get). For each campsites dashboard we
resolve its template variables to the values Grafana would default to, interpolate
them into every panel query, and run the query through `/api/ds/query` over two
windows:

- the dashboard's own default range — catches a variable defaulting to a dataless
  value (the `target_date=2026-05-01` bug: the trend panels came back empty);
- `now-24h` — catches a panel hardcoding its own window while the viewport is
  shorter (the collector-history `now()-INTERVAL '30' DAY` bug: empty at 24h).

A query that *errors* (e.g. an AE token isn't configured) is treated as "can't
evaluate" and skipped, not failed — only a successful-but-empty result fails.
"""

import json
import pathlib
import urllib.error
import urllib.request

import pytest

# fixtures (grafana_auth, grafana_get) come from conftest via pytest auto-discovery
GRAFANA_URL = "http://localhost:3001"
DASH_DIR = pathlib.Path(__file__).parent.parent / "provisioning" / "dashboards"

pytestmark = pytest.mark.integration

CAMPSITES = sorted((DASH_DIR / "campsites").glob("*.json"))
IDS = [f.name for f in CAMPSITES]

# datasource types we can execute here (the R2/GraphQL infinity panels need an
# external Cloudflare token and aren't part of the nodata class we're guarding).
QUERYABLE = {"vertamedia-clickhouse-datasource", "influxdb"}
# panels allowed to be legitimately empty (no failures recorded is a good thing).
ALLOW_EMPTY = ("failing",)


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
            # /api/ds/query returns 4xx with the per-query error in the JSON body
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {}
    return post


def _panels(dash):
    for p in dash.get("panels", []):
        yield p
        for sub in p.get("panels", []):
            yield sub


def _ds(panel, target):
    return target.get("datasource") or panel.get("datasource") or {}


def _run(post, target, ds, frm, to):
    """Run one target. Returns (frames, error_or_None)."""
    q = {
        "refId": "A",
        "datasource": ds,
        "query": target["__query"],
        "rawQuery": True,
        "format": target.get("format", "time_series"),
    }
    if ds.get("type") == "vertamedia-clickhouse-datasource":
        q["dateTimeColDataType"] = target.get("dateTimeColDataType", "timestamp")
        q["dateTimeType"] = target.get("dateTimeType", "DATETIME")
        q["intervalMs"] = 600000 if frm == "now-24h" else 3600000
        q["maxDataPoints"] = 500
    status, body = post("/api/ds/query", {"queries": [q], "from": frm, "to": to})
    res = body.get("results", {}).get("A", {})
    if res.get("error"):
        return [], res["error"]
    if status != 200:
        return [], f"HTTP {status}"
    return res.get("frames", []), None


def _has_rows(frames):
    for fr in frames:
        for col in fr.get("data", {}).get("values", []):
            if any(v is not None for v in col):
                return True
    return False


def _resolve_vars(post, dash, frm, to):
    """Return {name: {'one': first_value, 'all': [values]}} for query variables."""
    out = {}
    for v in dash.get("templating", {}).get("list", []):
        if v.get("type") != "query":
            continue
        ds = v.get("datasource") or {}
        frames, err = _run(post, {"__query": v["query"], "format": "table"}, ds, frm, to)
        if err:
            continue
        vals = []
        for fr in frames:
            cols = fr.get("data", {}).get("values", [])
            if cols:
                vals.extend(v for v in cols[0] if v is not None)
        if vals:
            out[v["name"]] = {"one": vals[0], "all": vals}
    return out


def _interpolate(query, vars_):
    for name, vv in vars_.items():
        query = query.replace(f"${{{name}:json}}", json.dumps(vv["all"]))
        query = query.replace(f"${{{name}}}", str(vv["one"]))
    return query


@pytest.mark.parametrize("f", CAMPSITES, ids=IDS)
def test_campsites_panels_return_data(f, grafana_post):
    dash = json.loads(f.read_text())
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
            allow_empty = any(tok in title.lower() for tok in ALLOW_EMPTY)

            # --- default range: catches "variable defaults to a dataless value" ---
            t_def = dict(t, __query=_interpolate(t["query"], vars_default))
            frames, err = _run(grafana_post, t_def, ds, default_from, default_to)
            if err:
                continue  # datasource unavailable — can't judge, skip this target
            evaluated += 1
            default_had_rows = _has_rows(frames)
            if not default_had_rows and not allow_empty:
                failures.append(
                    f"[{title!r}] No data over default range "
                    f"{default_from}..{default_to} (vars={ {k: v['one'] for k, v in vars_default.items()} })")
                continue

            # --- short range: catches "panel hardcodes a window > the viewport" ---
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
