"""Shared fixtures for the Grafana config tests."""

import json
import pathlib
import urllib.error
import urllib.request

import pytest

GRAFANA_DIR = pathlib.Path(__file__).parent.parent
PROVISIONING = GRAFANA_DIR / "provisioning"
DASH_DIR = PROVISIONING / "dashboards"
DS_DIR = PROVISIONING / "datasources"
GRAFANA_URL = "http://localhost:3001"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: needs the running Grafana/InfluxDB stack")


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
def grafana_env():
    return parse_env(GRAFANA_DIR / ".env")


@pytest.fixture(scope="session")
def grafana_auth(grafana_env):
    import base64
    user = grafana_env.get("GRAFANA_ADMIN_USER", "")
    pw = grafana_env.get("GRAFANA_ADMIN_PASSWORD", "")
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def _get(url, auth=None, timeout=4):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


@pytest.fixture(scope="session")
def grafana_get(grafana_auth):
    """Return a (url)->json getter, or skip the whole integration tier if Grafana is down."""
    try:
        status, body = _get(f"{GRAFANA_URL}/api/health")
    except (urllib.error.URLError, OSError) as e:
        pytest.skip(f"Grafana not reachable at {GRAFANA_URL}: {e}")
    if status != 200 or body.get("database") != "ok":
        pytest.skip(f"Grafana unhealthy: {body}")

    def getter(path, timeout=6):
        return _get(f"{GRAFANA_URL}{path}", auth=grafana_auth, timeout=timeout)
    return getter
