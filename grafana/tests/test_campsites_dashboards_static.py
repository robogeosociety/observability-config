"""Static guards for the campsites/ dashboards (hermetic, runs in CI).

These encode the two nodata bugs we actually hit so they can't come back:

1. **Hardcoded time window on a time-axis panel.** The collector-history panels
   queried `WHERE timestamp > now() - INTERVAL '30' DAY` while Grafana only plots
   points inside the dashboard time picker — so any range shorter than the data
   span (a remembered `now-24h`) rendered an empty viewport that reads as *No
   data*. A timeseries panel must bind its query to the dashboard range instead:
   ClickHouse `$timeFilter`/`$timeSeries`, InfluxDB `v.timeRangeStart`.

2. **A ClickHouse target using the time macros without declaring its date column.**
   `$timeFilter`/`$timeSeries` silently fail to expand unless the vertamedia
   target sets `dateTimeColDataType` — which produces an empty panel with no error.

The runtime "does it actually return rows" check lives in the integration tier
(`test_campsites_integration.py`); this tier is the cheap structural backstop.
"""

import json
import pathlib
import re

import pytest

CAMPSITES_DIR = pathlib.Path(__file__).parent.parent / "provisioning" / "dashboards" / "campsites"
DASHBOARDS = sorted(CAMPSITES_DIR.glob("*.json"))
IDS = [f.name for f in DASHBOARDS]

# Panel types whose X axis is time — these MUST follow the dashboard picker, or a
# too-short range blanks them. Deliberately narrow: a `barchart` is usually
# *categorical* (x = agency, a current-snapshot aggregation) and legitimately uses
# its own window, like a stat/table/gauge. Only `timeseries`/`trend` plot over time.
TIME_AXIS_TYPES = {"timeseries", "trend"}

# ClickHouse range-binding macros (vertamedia + grafana variants).
CH_RANGE_MACROS = ("$timeFilter", "$__timeFilter", "$timeFilterByColumn")
# A hardcoded ClickHouse time bound — the exact anti-pattern we're banning.
CH_HARDCODED_WINDOW = re.compile(r"now\(\)\s*-\s*INTERVAL", re.IGNORECASE)
# A hardcoded Flux relative range literal, e.g. range(start: -30d).
FLUX_HARDCODED_RANGE = re.compile(r"range\(\s*start:\s*-\d+[smhdwy]")


def _panels(dash):
    """Flatten panels, descending into collapsed rows."""
    for p in dash.get("panels", []):
        yield p
        for sub in p.get("panels", []):
            yield sub


def _ds_type(panel, target):
    ds = target.get("datasource") or panel.get("datasource") or {}
    return ds.get("type") if isinstance(ds, dict) else None


def _targets(panel):
    return [t for t in panel.get("targets", []) if isinstance(t, dict)]


def test_campsites_dashboards_present():
    assert DASHBOARDS, f"no campsites dashboards under {CAMPSITES_DIR}"


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_time_axis_panels_follow_the_picker(f):
    """Every time-axis panel binds its query to the dashboard time range."""
    dash = json.loads(f.read_text())
    offenders = []
    for p in _panels(dash):
        if p.get("type") not in TIME_AXIS_TYPES:
            continue
        for t in _targets(p):
            q = t.get("query") or ""
            if not q:
                continue
            dstype = _ds_type(p, t)
            if dstype == "vertamedia-clickhouse-datasource":
                if not any(m in q for m in CH_RANGE_MACROS):
                    offenders.append((p.get("id"), p.get("title"),
                                      "ClickHouse timeseries without $timeFilter"))
                if CH_HARDCODED_WINDOW.search(q):
                    offenders.append((p.get("id"), p.get("title"),
                                      "hardcoded now()-INTERVAL window"))
            elif dstype == "influxdb":
                if "v.timeRangeStart" not in q:
                    offenders.append((p.get("id"), p.get("title"),
                                      "Flux timeseries without v.timeRangeStart"))
                if FLUX_HARDCODED_RANGE.search(q):
                    offenders.append((p.get("id"), p.get("title"),
                                      "hardcoded Flux range(start: -Nd)"))
    assert not offenders, (
        f"{f.name}: time-axis panels not bound to the time picker (would render "
        f"nodata on a short range): {offenders}")


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_clickhouse_time_macros_declare_date_column(f):
    """A vertamedia target using $timeFilter/$timeSeries must set dateTimeColDataType,
    or the macro never expands and the panel is silently empty."""
    dash = json.loads(f.read_text())
    offenders = []
    for p in _panels(dash):
        for t in _targets(p):
            if _ds_type(p, t) != "vertamedia-clickhouse-datasource":
                continue
            q = t.get("query") or ""
            uses_macro = "$timeFilter" in q or "$timeSeries" in q
            if uses_macro and not t.get("dateTimeColDataType"):
                offenders.append((p.get("id"), p.get("title")))
    assert not offenders, (
        f"{f.name}: ClickHouse targets use $timeFilter/$timeSeries but omit "
        f"dateTimeColDataType (macro won't expand → empty panel): {offenders}")


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_single_value_query_var_is_data_scoped(f):
    """A single-value query variable used in an equality filter must not be free to
    default to a dataless edge value (the target_date=2026-05-01 bug). We require
    such a variable's query to be scoped (a predicate/filter), not a bare
    tagValues()/SELECT DISTINCT over all history."""
    dash = json.loads(f.read_text())
    variables = dash.get("templating", {}).get("list", [])
    # names referenced as "${name}" inside an equality comparison anywhere in the dash
    blob = json.dumps(dash)
    offenders = []
    for v in variables:
        if v.get("type") != "query" or v.get("multi") or v.get("includeAll"):
            continue
        name = v.get("name")
        if f'== "${{{name}}}"' not in blob and f"=='${{{name}}}'" not in blob:
            continue  # not used as an equality filter — irrelevant
        q = v.get("query") or ""
        scoped = ("filter(" in q or "WHERE" in q.upper() or "predicate" in q)
        assert scoped, (
            f"{f.name}: single-value var '{name}' feeds an equality filter but its "
            f"query is unscoped ({q!r}) — it can default to a value with no data. "
            f"Scope it (e.g. today-and-future) so the dashboard opens on live data.")
        offenders.append(name)  # reached only if scoped; kept for clarity
