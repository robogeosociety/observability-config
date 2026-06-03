"""Static validation of the file-provisioned dashboards (no running stack needed).

Catches the mistakes that break provisioning or silently produce empty panels:
malformed JSON, missing required keys, duplicate panel ids, colliding dashboard
uids, and panels pointing at datasource uids that aren't provisioned.
"""

import json
import pathlib
import re

import pytest
import yaml

PROVISIONING = pathlib.Path(__file__).parent.parent / "provisioning"
DASH_DIR = PROVISIONING / "dashboards"
DS_DIR = PROVISIONING / "datasources"

DASHBOARDS = sorted(DASH_DIR.rglob("*.json"))
IDS = [f.name for f in DASHBOARDS]


def _defined_ds_uids():
    uids = set()
    for f in DS_DIR.glob("*.yml"):
        data = yaml.safe_load(f.read_text()) or {}
        for ds in data.get("datasources", []):
            uids.add(ds["uid"])
    return uids


def _iter_ds_uids(obj):
    """Yield every datasource uid referenced under a `datasource` block."""
    if isinstance(obj, dict):
        ds = obj.get("datasource")
        if isinstance(ds, dict) and isinstance(ds.get("uid"), str):
            yield ds["uid"]
        for v in obj.values():
            yield from _iter_ds_uids(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_ds_uids(v)


def test_dashboards_present():
    assert DASHBOARDS, f"no dashboard JSON found in {DASH_DIR}"


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_valid_json(f):
    json.loads(f.read_text())


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_required_keys(f):
    d = json.loads(f.read_text())
    for key in ("uid", "title", "panels", "schemaVersion"):
        assert key in d, f"{f.name}: missing top-level '{key}'"
    assert d["uid"], f"{f.name}: empty uid"
    assert isinstance(d["panels"], list) and d["panels"], f"{f.name}: no panels"


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_unique_panel_ids(f):
    d = json.loads(f.read_text())
    ids = [p["id"] for p in d["panels"] if "id" in p]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"{f.name}: duplicate panel ids {dupes}"


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_every_panel_has_title_and_type(f):
    d = json.loads(f.read_text())
    for p in d["panels"]:
        assert p.get("type"), f"{f.name}: panel {p.get('id')} has no type"
        # row panels may legitimately exist; everything else needs a title
        if p.get("type") != "row":
            assert p.get("title"), f"{f.name}: panel {p.get('id')} has no title"


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_datasource_refs_resolve(f):
    defined = _defined_ds_uids()
    d = json.loads(f.read_text())
    refs = set(_iter_ds_uids(d))
    missing = refs - defined
    assert not missing, f"{f.name}: references undefined datasource uid(s): {sorted(missing)}"


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_no_legacy_hidden_legend(f):
    """`legend.displayMode: "hidden"` is the pre-10 form and no longer a valid
    enum in Grafana 11 — it makes the whole panel body fail to mount (header
    only, no canvas, no error). Use `showLegend: false` instead."""
    d = json.loads(f.read_text())
    bad = []
    for p in d["panels"]:
        legend = p.get("options", {}).get("legend")
        if isinstance(legend, dict) and legend.get("displayMode") == "hidden":
            bad.append(p.get("id"))
    assert not bad, (
        f"{f.name}: panels {bad} use legacy legend displayMode:'hidden' "
        "(fails to render in Grafana 11) — use {\"showLegend\": false}")


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_no_si_collapsing_pressure_unit(f):
    """`pressurembar` SI-scales millibars to bar, collapsing a ~1015 mb axis to
    a column of "1 bar" ticks. Use `pressurehpa` (numerically identical, native)."""
    d = json.loads(f.read_text())
    bad = [p.get("id") for p in d["panels"]
           if p.get("fieldConfig", {}).get("defaults", {}).get("unit") == "pressurembar"]
    assert not bad, (f"{f.name}: panels {bad} use unit 'pressurembar' "
                     "(axis collapses to '1 bar') — use 'pressurehpa'")


def test_dashboard_uids_unique():
    uids = [json.loads(f.read_text())["uid"] for f in DASHBOARDS]
    dupes = {u for u in uids if uids.count(u) > 1}
    assert not dupes, f"colliding dashboard uids across files: {dupes}"


def test_coverage_config_has_no_orphans():
    """Every uid in the integration tier's dashboard_coverage.yaml must map to a
    real dashboard (hermetic — guards against a renamed/removed dashboard leaving a
    stale allow-empty entry). The live nodata check itself is in the integration
    tier; this just keeps its config honest."""
    cov_file = PROVISIONING.parent / "tests" / "dashboard_coverage.yaml"
    coverage = yaml.safe_load(cov_file.read_text()) or {}
    uids = {json.loads(f.read_text())["uid"] for f in DASHBOARDS}
    orphans = set(coverage) - uids
    assert not orphans, f"dashboard_coverage.yaml references unknown uids: {sorted(orphans)}"


# ---------------------------------------------------------------------------
# nodata / regression guards (promoted repo-wide from the campsites suite, #67)
#
# Three "renders No data" bug classes that plain JSON validation misses:
#   1. a time-axis panel hardcodes its own window instead of following the picker;
#   2. a single-value query variable feeding an equality filter can default to a
#      dataless edge value (the availability target_date=2026-05-01 bug);
#   3. (runtime-only) a panel uses a datasource function the backend lacks —
#      caught health-aware in the integration tier, not here.
# Auditing all dashboards, nothing currently trips 1 or 2, so these are a
# forward-looking gate. Bug class 3 has no cheap static form.
# ---------------------------------------------------------------------------

# Panel types whose X axis is time — these MUST follow the dashboard picker, or a
# too-short range blanks them. Deliberately narrow: a `barchart` is usually
# *categorical* (x = agency, a current-snapshot aggregation) and legitimately uses
# its own window, like a stat/table/gauge. Only `timeseries`/`trend` plot over time.
TIME_AXIS_TYPES = {"timeseries", "trend"}

# Escape hatch: a (dashboard uid, panel id) that is a *legitimately* fixed-window
# time-axis panel (e.g. an intentional status trend over a pinned range). Encode
# the exception here rather than weakening the rule. Seeded empty — today nothing
# in the repo needs it.
#   TIME_AXIS_EXCEPTIONS = {("some-dashboard-uid", 7)}
TIME_AXIS_EXCEPTIONS: set = set()

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


@pytest.mark.parametrize("f", DASHBOARDS, ids=IDS)
def test_time_axis_panels_follow_the_picker(f):
    """Every time-axis panel binds its query to the dashboard time range."""
    dash = json.loads(f.read_text())
    uid = dash.get("uid")
    offenders = []
    for p in _panels(dash):
        if p.get("type") not in TIME_AXIS_TYPES:
            continue
        if (uid, p.get("id")) in TIME_AXIS_EXCEPTIONS:
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
    blob = json.dumps(dash)
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
