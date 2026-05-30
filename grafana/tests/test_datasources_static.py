"""Static validation of the file-provisioned datasources and the dashboard provider."""

import pathlib

import pytest
import yaml

PROVISIONING = pathlib.Path(__file__).parent.parent / "provisioning"
DS_DIR = PROVISIONING / "datasources"

DS_FILES = sorted(DS_DIR.glob("*.yml"))
IDS = [f.name for f in DS_FILES]


def _all_datasources():
    out = []
    for f in DS_FILES:
        data = yaml.safe_load(f.read_text()) or {}
        out.extend(data.get("datasources", []))
    return out


@pytest.mark.parametrize("f", DS_FILES, ids=IDS)
def test_valid_yaml_and_apiversion(f):
    data = yaml.safe_load(f.read_text())
    assert data.get("apiVersion") == 1, f"{f.name}: apiVersion must be 1"
    assert isinstance(data.get("datasources"), list) and data["datasources"]


@pytest.mark.parametrize("f", DS_FILES, ids=IDS)
def test_required_fields(f):
    data = yaml.safe_load(f.read_text())
    for ds in data["datasources"]:
        for key in ("name", "uid", "type"):
            assert ds.get(key), f"{f.name}: datasource missing '{key}': {ds.get('name')}"


def test_datasource_uids_unique_across_files():
    uids = [ds["uid"] for ds in _all_datasources()]
    dupes = {u for u in uids if uids.count(u) > 1}
    assert not dupes, f"duplicate datasource uids: {dupes}"


def test_influxdb_datasources_have_url_and_flux():
    for ds in _all_datasources():
        if ds["type"] == "influxdb":
            assert ds.get("url"), f"{ds['name']}: influxdb datasource needs a url"
            assert ds.get("jsonData", {}).get("version") == "Flux", \
                f"{ds['name']}: expected Flux query language"


def test_provider_locks_ui_updates():
    """The whole point of store-as-code: the Grafana UI must not be the source of truth."""
    provider = yaml.safe_load((PROVISIONING / "dashboards" / "provider.yml").read_text())
    prov = provider["providers"][0]
    assert prov["type"] == "file"
    assert prov["allowUiUpdates"] is False, \
        "allowUiUpdates must be False so provisioned dashboards are read-only in the UI"
    assert isinstance(prov["updateIntervalSeconds"], int)
    assert prov["options"]["path"] == "/etc/grafana/provisioning/dashboards"
