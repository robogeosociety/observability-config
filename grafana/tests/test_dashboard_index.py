"""Static, hermetic checks that `dashboards.index.yaml` stays in sync.

The index (grafana/dashboards.index.yaml) captures dashboard *intent* — purpose,
rationale, backlog. These tests make it self-maintaining: a dashboard JSON with
no index entry, or an entry that drifts from the JSON it documents, fails CI.
That keeps the context honest as dashboards change, which is the whole point of
storing it as code.

An entry may lead the JSON: mark it `status: pending` to document a dashboard
before its JSON is committed (the orphan check tolerates only pending entries).
"""
import json
from pathlib import Path

import pytest
import yaml

GRAFANA_DIR = Path(__file__).parent.parent
DASHBOARD_DIR = GRAFANA_DIR / "provisioning" / "dashboards"
INDEX_PATH = GRAFANA_DIR / "dashboards.index.yaml"

# JSON datasource `type` -> the friendly name used in the index. Anything in
# IGNORE_DS is a built-in/synthetic ref with no provisioned datasource and so
# isn't tracked in the index.
DS_FRIENDLY = {
    "influxdb": "influxdb",
    "yesoreyeram-infinity-datasource": "infinity",
    "vertamedia-clickhouse-datasource": "analytics-engine",
}
IGNORE_DS = {"__expr__", "grafana", "-- Grafana --", "-- Mixed --", "-- Dashboard --", "datasource"}

REQUIRED_FIELDS = ("title", "file", "purpose", "datasources", "intent", "todos")


def _dashboards():
    return sorted(DASHBOARD_DIR.glob("*.json"))


def _load_json(path):
    with path.open() as fh:
        return json.load(fh)


def _index():
    with INDEX_PATH.open() as fh:
        return yaml.safe_load(fh)


def _json_datasources(dashboard):
    """The set of friendly datasource names a dashboard's panels actually use."""
    found = set()

    def visit(panel):
        ds = panel.get("datasource")
        if isinstance(ds, dict):
            t = ds.get("type")
            if t and t not in IGNORE_DS:
                assert t in DS_FRIENDLY, (
                    f"unknown datasource type {t!r} — add it to DS_FRIENDLY or IGNORE_DS"
                )
                found.add(DS_FRIENDLY[t])

    for panel in dashboard.get("panels", []):
        visit(panel)
        for sub in panel.get("panels", []):
            visit(sub)
    return found


def test_index_is_valid_yaml_mapping():
    idx = _index()
    assert isinstance(idx, dict) and idx, "index must be a non-empty mapping"


def test_every_dashboard_has_an_entry():
    idx = _index()
    json_uids = {_load_json(p)["uid"] for p in _dashboards()}
    missing = json_uids - set(idx)
    assert not missing, f"dashboards with no index entry: {sorted(missing)}"


def test_no_orphan_entries():
    """An index entry with no matching JSON is only allowed when status: pending."""
    idx = _index()
    json_uids = {_load_json(p)["uid"] for p in _dashboards()}
    orphans = {
        uid for uid in set(idx) - json_uids
        if (idx[uid] or {}).get("status") != "pending"
    }
    assert not orphans, (
        f"index entries with no dashboard JSON (mark `status: pending` if intentional): "
        f"{sorted(orphans)}"
    )


@pytest.fixture(params=_dashboards(), ids=lambda p: p.stem)
def dashboard_path(request):
    return request.param


def test_entry_is_complete_and_in_sync(dashboard_path):
    idx = _index()
    dashboard = _load_json(dashboard_path)
    uid = dashboard["uid"]
    entry = idx.get(uid)
    assert entry, f"{uid} has no index entry"

    for field in REQUIRED_FIELDS:
        assert entry.get(field), f"{uid}: index entry missing/empty `{field}`"

    assert entry["title"] == dashboard["title"], (
        f"{uid}: index title {entry['title']!r} != JSON title {dashboard['title']!r}"
    )
    assert entry["file"] == dashboard_path.name, (
        f"{uid}: index file {entry['file']!r} != {dashboard_path.name!r}"
    )

    assert isinstance(entry["datasources"], list)
    assert isinstance(entry["todos"], list) and entry["todos"], (
        f"{uid}: `todos` must be a non-empty list"
    )

    declared = set(entry["datasources"])
    actual = _json_datasources(dashboard)
    assert declared == actual, (
        f"{uid}: index datasources {sorted(declared)} != JSON {sorted(actual)}"
    )
