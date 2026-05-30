"""Integration: assert the files actually provision into the running Grafana.

Self-skips (via the grafana_get fixture) if Grafana isn't reachable, so it's safe
to run anywhere. These are the checks that catch drift between the repo and what
Grafana actually loaded.
"""

import json
import pathlib

import pytest
import yaml

PROVISIONING = pathlib.Path(__file__).parent.parent / "provisioning"
DASH_DIR = PROVISIONING / "dashboards"
DS_DIR = PROVISIONING / "datasources"

pytestmark = pytest.mark.integration

DASHBOARDS = sorted(DASH_DIR.glob("*.json"))
DASH_IDS = [f.name for f in DASHBOARDS]


def _defined_datasources():
    out = []
    for f in DS_DIR.glob("*.yml"):
        data = yaml.safe_load(f.read_text()) or {}
        out.extend(data.get("datasources", []))
    return out


def test_health(grafana_get):
    _, body = grafana_get("/api/health")
    assert body["database"] == "ok"
    assert body["version"].startswith("11."), f"unexpected version {body['version']}"


@pytest.mark.parametrize("f", DASHBOARDS, ids=DASH_IDS)
def test_dashboard_provisioned_and_matches_file(f, grafana_get):
    spec = json.loads(f.read_text())
    status, body = grafana_get(f"/api/dashboards/uid/{spec['uid']}")
    assert status == 200, f"{spec['uid']} not loaded into Grafana"
    assert body["meta"]["provisioned"] is True, \
        f"{spec['uid']} should be provisioned (read-only) — UI lock not applied?"
    assert body["dashboard"]["title"] == spec["title"]
    # panel count in Grafana should match the file (provisioning actually took)
    assert len(body["dashboard"]["panels"]) == len(spec["panels"])


@pytest.mark.parametrize("ds", _defined_datasources(), ids=[d["uid"] for d in _defined_datasources()])
def test_datasource_exists(ds, grafana_get):
    status, body = grafana_get(f"/api/datasources/uid/{ds['uid']}")
    assert status == 200, f"datasource {ds['uid']} not provisioned"
    assert body["type"] == ds["type"]


@pytest.mark.parametrize(
    "ds",
    [d for d in _defined_datasources() if d["type"] == "influxdb"],
    ids=[d["uid"] for d in _defined_datasources() if d["type"] == "influxdb"],
)
def test_influxdb_datasource_health_ok(ds, grafana_get):
    status, body = grafana_get(f"/api/datasources/uid/{ds['uid']}/health")
    assert status == 200
    assert body.get("status") == "OK", f"{ds['uid']} health: {body.get('message')}"
