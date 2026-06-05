"""Static, hermetic checks that the intent index stays in sync.

The index is **conf.d** — `grafana/dashboards.index.d/<uid>.yaml`, one file per
dashboard (so concurrent PRs never collide on a shared file). Each file is a
single-key mapping `{<uid>: {...}}`; the tests glob the dir and dict-merge it.
The index captures dashboard *intent* — purpose, rationale, backlog. These tests
make it self-maintaining: a dashboard JSON with no index entry, or an entry that
drifts from the JSON it documents, fails CI. That keeps the context honest as
dashboards change, which is the whole point of storing it as code.

An entry may lead the JSON: mark it `status: pending` to document a dashboard
before its JSON is committed (the orphan check tolerates only pending entries).
"""
import json
from pathlib import Path

import pytest
import yaml

GRAFANA_DIR = Path(__file__).parent.parent
DASHBOARD_DIR = GRAFANA_DIR / "provisioning" / "dashboards"
INDEX_DIR = GRAFANA_DIR / "dashboards.index.d"

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
    return sorted(DASHBOARD_DIR.rglob("*.json"))


def _load_json(path):
    with path.open() as fh:
        return json.load(fh)


def _index_files():
    return sorted(INDEX_DIR.glob("*.yaml"))


def _index():
    """Merge every conf.d sidecar into one {uid: entry} mapping."""
    merged = {}
    for f in _index_files():
        merged.update(yaml.safe_load(f.read_text()) or {})
    return merged


def test_each_sidecar_is_single_uid_matching_filename():
    """One file per uid, named <uid>.yaml — so adding a dashboard is a new file,
    never a shared-file edit, and the filename is the unique claim on the uid."""
    for f in _index_files():
        data = yaml.safe_load(f.read_text()) or {}
        assert list(data.keys()) == [f.stem], (
            f"{f.name} must contain exactly one entry keyed {f.stem!r}, got {list(data)}"
        )


def test_no_duplicate_uid_across_files():
    """Two projects grabbing the same uid is a hermetic collision, caught here."""
    seen = {}
    for f in _index_files():
        for uid in (yaml.safe_load(f.read_text()) or {}):
            assert uid not in seen, (
                f"uid {uid!r} defined in both {seen[uid]} and {f.name}"
            )
            seen[uid] = f.name


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
    rel = str(dashboard_path.relative_to(DASHBOARD_DIR))
    assert entry["file"] == rel, (
        f"{uid}: index file {entry['file']!r} != {rel!r} "
        f"(file is folder-relative — dashboards live in domain subdirs)"
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


VALID_SOURCE_STATUS = {"active", "external", "migrated", "deprecated"}


def test_every_entry_has_source():
    """Provenance convention: every committed dashboard declares where its data
    comes from, so drift (an upstream pipeline changing/migrating) is detectable."""
    idx = _index()
    json_uids = {_load_json(p)["uid"] for p in _dashboards()}
    missing = sorted(uid for uid in json_uids if not (idx.get(uid) or {}).get("source"))
    assert not missing, f"dashboards missing a `source:` provenance block: {missing}"


def test_source_block_well_formed():
    """Validate the source schema. `repo`+`paths` are required only when the
    producing code is something we track (status: active); external pipelines may
    have a TBD repo."""
    idx = _index()
    for uid, entry in idx.items():
        src = (entry or {}).get("source")
        if src is None:
            continue
        assert isinstance(src, dict), f"{uid}: source must be a mapping"
        status = src.get("status")
        assert status in VALID_SOURCE_STATUS, (
            f"{uid}: source.status {status!r} not in {sorted(VALID_SOURCE_STATUS)}"
        )
        produces = src.get("produces")
        assert isinstance(produces, dict) and ("bucket" in produces or "dataset" in produces), (
            f"{uid}: source.produces must name a `bucket` or `dataset`"
        )
        assert produces.get("via"), f"{uid}: source.produces needs a `via`"
        if status == "active":
            repo = src.get("repo", "")
            assert isinstance(repo, str) and "/" in repo, (
                f"{uid}: source.repo (owner/name) required when status=active, got {repo!r}"
            )
            assert src.get("paths"), f"{uid}: source.paths required when status=active"
