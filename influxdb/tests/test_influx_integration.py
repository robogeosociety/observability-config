"""Integration: assert InfluxDB is healthy and every bucket the Grafana
datasources point at actually exists. Self-skips if InfluxDB is down.
"""

import pathlib

import pytest
import yaml

INFLUX_DIR = pathlib.Path(__file__).parent.parent

pytestmark = pytest.mark.integration

# Grafana's datasource definitions live in the sibling grafana/ config dir.
GRAFANA_DS_DIR = INFLUX_DIR.parent / "grafana" / "provisioning" / "datasources"


def _influx_default_buckets():
    """defaultBucket of every influxdb datasource Grafana provisions."""
    buckets = set()
    for f in GRAFANA_DS_DIR.glob("*.yml"):
        data = yaml.safe_load(f.read_text()) or {}
        for ds in data.get("datasources", []):
            if ds.get("type") == "influxdb":
                b = ds.get("jsonData", {}).get("defaultBucket")
                if b:
                    buckets.add(b)
    return buckets


def test_influx_buckets_back_every_datasource(influx_api):
    required = _influx_default_buckets()
    assert required, "no influxdb datasources found to check"
    _, body = influx_api("/api/v2/buckets?limit=100")
    existing = {b["name"] for b in body.get("buckets", [])}
    missing = required - existing
    assert not missing, f"datasources point at non-existent buckets: {sorted(missing)}"


def test_influx_org_present(influx_api, influx_env):
    org = influx_env.get("INFLUX_ORG", "home")
    _, body = influx_api("/api/v2/orgs")
    orgs = {o["name"] for o in body.get("orgs", [])}
    assert org in orgs, f"org '{org}' not found in {orgs}"
