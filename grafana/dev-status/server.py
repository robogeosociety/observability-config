#!/usr/bin/env python3
"""Dev-deployments status collector + HTTP server.

Runs on the HOST (needs `tailscale serve status` and host-local TCP probes that a
container can't see). On each GET it regenerates a live snapshot of:

  * every configured tailscale serve route (port/path -> backend), with the
    backend's current UP/DOWN + connect latency, and
  * every Vite port-registry entry (~/.claude/vite-ports.json) whose port isn't
    already covered by a serve route, so configured-but-unserved projects still
    show up.

Grafana (in the OrbStack `grafana` container) fetches this via the Infinity
datasource at http://host.docker.internal:8077/dev-status.json on its 1m refresh.

Kept to the stdlib so it can run under /usr/bin/python3 in a launchd job.
"""

import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

LISTEN_PORT = int(os.environ.get("DEV_STATUS_PORT", "8077"))
TAILSCALE = "/opt/homebrew/bin/tailscale"
VITE_PORTS = os.path.expanduser("~/.claude/vite-ports.json")
PROBE_TIMEOUT = 1.5  # seconds

# Labels for non-Vite-registry backends, keyed by port. Routes with an explicit
# path use the path as their name first; this only fills in the gaps.
PORT_HINTS = {
    5177: "tallest-tree-pr7",
    8001: "tallest-tree-api",
    8080: "tt-api",
    3001: "grafana",
    8086: "influxdb",
    4646: "nomad",
}


def load_vite_registry():
    """Return {port: project} reversed from the Vite port registry, or {}."""
    try:
        with open(VITE_PORTS) as fh:
            data = json.load(fh)
        return {int(port): name for name, port in data.get("assignments", {}).items()}
    except (OSError, ValueError, KeyError):
        return {}


def tailscale_serve():
    """Return parsed `tailscale serve status -json`, or {} on failure."""
    try:
        out = subprocess.run(
            [TAILSCALE, "serve", "status", "-json"],
            capture_output=True, text=True, timeout=8,
        )
        if out.returncode != 0:
            return {}
        return json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def target_host_port(proxy_url):
    """('http://127.0.0.1:5173/x') -> ('127.0.0.1', 5173). None on parse fail."""
    try:
        parts = urlsplit(proxy_url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not host:
            return None
        return host, int(port)
    except (ValueError, TypeError):
        return None


def probe(host, port):
    """TCP-connect probe. Returns (up:int, latency_ms:float|None)."""
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            return 1, round((time.monotonic() - start) * 1000, 1)
    except OSError:
        return 0, None


def endpoint_url(endpoint, path):
    """Build the public https URL for a serve handler.

    `endpoint` looks like 'host:443' or 'host:5173'. Port 443 is the implicit
    https default and is dropped from the URL.
    """
    host, _, port = endpoint.partition(":")
    base = f"https://{host}" if port in ("", "443") else f"https://{host}:{port}"
    if path and path != "/":
        return base + path
    return base + "/"


def collect():
    registry = load_vite_registry()
    serve = tailscale_serve()
    rows = []
    covered_ports = set()

    web = serve.get("Web", {}) or {}
    for endpoint, cfg in sorted(web.items()):
        handlers = (cfg or {}).get("Handlers", {}) or {}
        for path, hcfg in sorted(handlers.items()):
            proxy = (hcfg or {}).get("Proxy", "")
            hp = target_host_port(proxy)
            if hp is None:
                continue
            host, port = hp
            up, latency = probe(host, port)
            # Name: explicit path wins, else registry, else hint, else port.
            if path and path != "/":
                name = path.strip("/")
            else:
                name = registry.get(port) or PORT_HINTS.get(port) or f"port-{port}"
            rows.append({
                "name": name,
                "url": endpoint_url(endpoint, path),
                "path": path,
                "target": proxy,
                "port": port,
                "source": "serve",
                "up": up,
                "latency_ms": latency if latency is not None else 0,
            })
            if host in ("127.0.0.1", "localhost"):
                covered_ports.add(port)

    # Vite-registry projects not already exposed by a serve route.
    for port, name in sorted(registry.items(), key=lambda kv: kv[1]):
        if port in covered_ports:
            continue
        up, latency = probe("127.0.0.1", port)
        rows.append({
            "name": name,
            "url": f"http://127.0.0.1:{port}/",
            "path": "/",
            "target": f"http://127.0.0.1:{port}",
            "port": port,
            "source": "registry",
            "up": up,
            "latency_ms": latency if latency is not None else 0,
        })

    now = datetime.now(timezone.utc)
    up_count = sum(r["up"] for r in rows)
    return {
        "generated_at": now.isoformat(),
        "generated_unix": int(now.timestamp()),
        "total": len(rows),
        "up": up_count,
        "down": len(rows) - up_count,
        "deployments": rows,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        try:
            payload = json.dumps(collect()).encode()
        except Exception as exc:  # last-ditch: never 500 the dashboard
            payload = json.dumps({"error": str(exc), "deployments": []}).encode()
            self.send_response(200)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # quiet; launchd captures stdout anyway
        pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"dev-status serving on :{LISTEN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
