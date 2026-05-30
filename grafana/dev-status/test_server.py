"""Tests for the dev-status collector.

Tiers:
  * unit         — pure functions + collect() with mocked IO (no network). Default.
  * integration  — spawns the real HTTP server and checks the live contract.

Run unit only:        pytest -m "not integration"
Run integration too:  pytest
"""

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
import server  # noqa: E402


# --------------------------------------------------------------------------- #
# unit — pure functions
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:5173/classify/", ("127.0.0.1", 5173)),
    ("http://127.0.0.1:5173", ("127.0.0.1", 5173)),
    ("http://transit-tracker.orb.local:8000", ("transit-tracker.orb.local", 8000)),
    ("https://example.com/x", ("example.com", 443)),
    ("http://bare-host", ("bare-host", 80)),
])
def test_target_host_port(url, expected):
    assert server.target_host_port(url) == expected


@pytest.mark.parametrize("bad", ["", "not a url", "://nope"])
def test_target_host_port_bad(bad):
    assert server.target_host_port(bad) is None


@pytest.mark.parametrize("endpoint,path,expected", [
    ("h.ts.net:443", "/", "https://h.ts.net/"),
    ("h.ts.net:443", "/api", "https://h.ts.net/api"),
    ("h.ts.net:5190", "/", "https://h.ts.net:5190/"),
    ("h.ts.net:3000", "/intentcity", "https://h.ts.net:3000/intentcity"),
])
def test_endpoint_url(endpoint, path, expected):
    assert server.endpoint_url(endpoint, path) == expected


def test_load_vite_registry(tmp_path, monkeypatch):
    p = tmp_path / "vite-ports.json"
    p.write_text(json.dumps({"range": [5180, 5199],
                             "assignments": {"foo": 5180, "bar": 5181}}))
    monkeypatch.setattr(server, "VITE_PORTS", str(p))
    assert server.load_vite_registry() == {5180: "foo", 5181: "bar"}


def test_load_vite_registry_missing(monkeypatch):
    monkeypatch.setattr(server, "VITE_PORTS", "/no/such/file.json")
    assert server.load_vite_registry() == {}


# --------------------------------------------------------------------------- #
# unit — collect() with mocked tailscale + probe
# --------------------------------------------------------------------------- #

def _mock_io(monkeypatch, serve, registry, up_ports):
    monkeypatch.setattr(server, "tailscale_serve", lambda: serve)
    monkeypatch.setattr(server, "load_vite_registry", lambda: registry)
    monkeypatch.setattr(server, "probe",
                        lambda host, port: (1, 1.2) if port in up_ports else (0, None))


def test_collect_shape_health_and_naming(monkeypatch):
    serve = {"Web": {
        "host:443": {"Handlers": {
            "/": {"Proxy": "http://127.0.0.1:5190"},
            "/api": {"Proxy": "http://127.0.0.1:8001/api"},
        }},
        "host:8086": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8086"}}},
    }}
    _mock_io(monkeypatch, serve, {5190: "realitycapture", 5191: "other"}, up_ports={5190, 8086})
    out = server.collect()

    assert set(out) >= {"generated_at", "generated_unix", "total", "up", "down", "deployments"}
    rows = {(r["name"], r["port"]): r for r in out["deployments"]}

    # root route on a registered port → named from the registry, up
    assert rows[("realitycapture", 5190)]["source"] == "serve"
    assert rows[("realitycapture", 5190)]["up"] == 1
    # explicit path → named from the path, down
    assert rows[("api", 8001)]["up"] == 0
    # registry port with no serve route → appears as a registry row, down
    assert rows[("other", 5191)]["source"] == "registry"
    assert rows[("other", 5191)]["up"] == 0

    # counts are internally consistent
    assert out["total"] == len(out["deployments"])
    assert out["up"] == sum(r["up"] for r in out["deployments"])
    assert out["up"] + out["down"] == out["total"]

    # single-row summary the stat panel reads (root_selector "summary")
    assert isinstance(out["summary"], list) and len(out["summary"]) == 1
    s = out["summary"][0]
    assert s == {"up": out["up"], "down": out["down"], "total": out["total"]}


def test_collect_does_not_duplicate_served_registry_port(monkeypatch):
    serve = {"Web": {"host:443": {"Handlers": {"/x": {"Proxy": "http://127.0.0.1:5190"}}}}}
    _mock_io(monkeypatch, serve, {5190: "realitycapture"}, up_ports={5190})
    out = server.collect()
    assert sum(1 for r in out["deployments"] if r["port"] == 5190) == 1
    assert ("registry") not in [r["source"] for r in out["deployments"] if r["port"] == 5190]


def test_collect_every_row_has_required_fields(monkeypatch):
    serve = {"Web": {"host:443": {"Handlers": {"/a": {"Proxy": "http://127.0.0.1:1234"}}}}}
    _mock_io(monkeypatch, serve, {}, up_ports=set())
    for r in server.collect()["deployments"]:
        for k in ("name", "url", "path", "target", "port", "source", "up", "latency_ms"):
            assert k in r
        assert r["up"] in (0, 1)
        assert isinstance(r["latency_ms"], (int, float))


def test_collect_survives_empty_tailscale(monkeypatch):
    _mock_io(monkeypatch, {}, {}, up_ports=set())
    out = server.collect()
    assert out["total"] == 0 and out["deployments"] == []


# --------------------------------------------------------------------------- #
# integration — real server over HTTP
# --------------------------------------------------------------------------- #

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.integration
def test_server_http_contract():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "server.py")],
        env={**os.environ, "DEV_STATUS_PORT": str(port), "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        url = f"http://127.0.0.1:{port}/dev-status.json"
        payload = None
        for _ in range(50):  # up to ~5s
            if proc.poll() is not None:
                raise AssertionError(f"server exited early:\n{proc.stdout.read()}")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    assert r.headers.get("Content-Type") == "application/json"
                    payload = json.loads(r.read())
                    break
            except OSError:
                time.sleep(0.1)
        assert payload is not None, "server never became reachable"

        # contract the Grafana Infinity panels depend on
        assert set(payload) >= {"generated_at", "total", "up", "down", "deployments"}
        assert isinstance(payload["deployments"], list)
        assert payload["total"] == len(payload["deployments"])
        assert payload["up"] + payload["down"] == payload["total"]
        for r in payload["deployments"]:
            assert {"name", "url", "port", "up", "source", "latency_ms"} <= set(r)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
