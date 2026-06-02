"""Static validation of the file-provisioned alerting (contact points, policies, rules).

Hermetic — parses the YAML under provisioning/alerting/ without a running stack.
Guards the issue-#11 availability alert against drift and accidental secret leaks.
"""

import pathlib

import pytest
import yaml

PROVISIONING = pathlib.Path(__file__).parent.parent / "provisioning"
ALERTING = PROVISIONING / "alerting"

CONTACTS = ALERTING / "contactpoints.yml"
POLICIES = ALERTING / "notification-policies.yml"
RULES = ALERTING / "influxdb-availability.yml"

ALL_FILES = sorted(ALERTING.glob("*.yml"))


@pytest.mark.parametrize("f", ALL_FILES, ids=[f.name for f in ALL_FILES])
def test_valid_yaml_and_apiversion(f):
    data = yaml.safe_load(f.read_text())
    assert data.get("apiVersion") == 1, f"{f.name}: apiVersion must be 1"


def test_contact_point_uses_env_not_literal_secret():
    """The Discord webhook URL must come from $__env{}, never a committed literal."""
    cp = yaml.safe_load(CONTACTS.read_text())
    receivers = cp["contactPoints"][0]["receivers"]
    discord = next(r for r in receivers if r["type"] == "discord")
    url = str(discord["settings"].get("url", ""))
    assert url.startswith("$__env{"), \
        f"discord webhook url must be a $__env{{}} ref, got: {url!r}"


def test_policy_routes_to_a_defined_contact_point():
    cp = yaml.safe_load(CONTACTS.read_text())
    names = {c["name"] for c in cp["contactPoints"]}
    pol = yaml.safe_load(POLICIES.read_text())
    receiver = pol["policies"][0]["receiver"]
    assert receiver in names, \
        f"root policy receiver {receiver!r} has no matching contact point {names}"


def test_influxdb_rule_alerts_on_missing_data():
    """The whole point: a gap or a query error must page, not pass silently."""
    rules_doc = yaml.safe_load(RULES.read_text())
    group = rules_doc["groups"][0]
    rule = next(r for r in group["rules"] if r["uid"] == "influxdb-down")
    assert rule["noDataState"] == "Alerting", "no data must alert (telegraf gap)"
    assert rule["execErrState"] == "Alerting", "query error must alert (influx down)"
    # The query series must target the InfluxDB `system` datasource (telegraf sink).
    query_node = next(d for d in rule["data"] if d["refId"] == rule["data"][0]["refId"])
    assert query_node["datasourceUid"] == "system"
    assert "system" in query_node["model"]["query"], "rule must read the system bucket"
