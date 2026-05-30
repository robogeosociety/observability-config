"""Shared fixtures for the InfluxDB config tests."""

import json
import pathlib
import urllib.error
import urllib.request

import pytest

INFLUX_DIR = pathlib.Path(__file__).parent.parent
INFLUX_URL = "http://localhost:8086"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: needs the running InfluxDB container")


def parse_env(path):
    out = {}
    p = pathlib.Path(path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


@pytest.fixture(scope="session")
def influx_env():
    return parse_env(INFLUX_DIR / ".env")


@pytest.fixture(scope="session")
def influx_api(influx_env):
    """Return an authed JSON getter for the Influx API, or skip if it's down."""
    try:
        with urllib.request.urlopen(f"{INFLUX_URL}/health", timeout=4) as r:
            body = json.loads(r.read())
    except (urllib.error.URLError, OSError) as e:
        pytest.skip(f"InfluxDB not reachable at {INFLUX_URL}: {e}")
    if body.get("status") != "pass":
        pytest.skip(f"InfluxDB unhealthy: {body}")

    token = influx_env.get("INFLUX_ADMIN_TOKEN")
    if not token:
        pytest.skip("INFLUX_ADMIN_TOKEN missing from influxdb/.env")

    def getter(path, timeout=6):
        req = urllib.request.Request(f"{INFLUX_URL}{path}",
                                     headers={"Authorization": f"Token {token}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    return getter
