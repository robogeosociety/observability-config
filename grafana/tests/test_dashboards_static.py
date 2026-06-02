"""Static validation of the file-provisioned dashboards (no running stack needed).

Catches the mistakes that break provisioning or silently produce empty panels:
malformed JSON, missing required keys, duplicate panel ids, colliding dashboard
uids, and panels pointing at datasource uids that aren't provisioned.
"""

import json
import pathlib

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
