"""Read-only data access — the shared core behind every front-end.

These functions touch the live stack with read-only credentials only. The same
set is exposed three ways:
  - to the Discord bot's internal Claude loop (agent.py),
  - to Claude CLI sessions over MCP (mcp_server.py),
  - to scripted summaries (cli.py).

Nothing here can write or delete: the Influx token is read-scoped and the
Grafana token is a Viewer. "Read-only" is a property of the credentials, not the
prompt.
"""

from __future__ import annotations

import json
import re
import time

import requests
from influxdb_client import InfluxDBClient

from . import config


# --- InfluxDB -------------------------------------------------------------

def _influx() -> InfluxDBClient:
    return InfluxDBClient(
        url=config.INFLUX_URL,
        token=config.INFLUX_READ_TOKEN,
        org=config.INFLUX_ORG,
        timeout=config.QUERY_TIMEOUT_S * 1000,
    )


def _run_flux(flux: str) -> list[dict]:
    """Execute Flux, return up to MAX_ROWS records as plain dicts (no _start/_stop noise)."""
    drop = {"result", "table", "_start", "_stop"}
    rows: list[dict] = []
    with _influx() as client:
        for table in client.query_api().query(flux):
            for rec in table.records:
                rows.append({k: v for k, v in rec.values.items() if k not in drop})
                if len(rows) >= config.MAX_ROWS:
                    return rows
    return rows


def query_influx(flux: str) -> list[dict]:
    """Run an arbitrary read-only Flux query and return the rows."""
    return _run_flux(flux)


def list_buckets() -> list[str]:
    rows = _run_flux('buckets() |> keep(columns: ["name"])')
    return sorted(r["name"] for r in rows if not r["name"].startswith("_"))


def list_measurements(bucket: str) -> list[str]:
    flux = (
        'import "influxdata/influxdb/schema"\n'
        f'schema.measurements(bucket: "{bucket}")'
    )
    return [r["_value"] for r in _run_flux(flux)]


def list_fields(bucket: str, measurement: str | None = None) -> list[str]:
    if measurement:
        flux = (
            'import "influxdata/influxdb/schema"\n'
            f'schema.measurementFieldKeys(bucket: "{bucket}", measurement: "{measurement}")'
        )
    else:
        flux = (
            'import "influxdata/influxdb/schema"\n'
            f'schema.fieldKeys(bucket: "{bucket}")'
        )
    return [r["_value"] for r in _run_flux(flux)]


def list_tag_keys(bucket: str, measurement: str | None = None) -> list[str]:
    pred = f', predicate: (r) => r._measurement == "{measurement}"' if measurement else ""
    flux = (
        'import "influxdata/influxdb/schema"\n'
        f'schema.tagKeys(bucket: "{bucket}"{pred})'
    )
    return [r["_value"] for r in _run_flux(flux) if not r["_value"].startswith("_")]


def list_tag_values(bucket: str, tag: str, measurement: str | None = None) -> list[str]:
    pred = f', predicate: (r) => r._measurement == "{measurement}"' if measurement else ""
    flux = (
        'import "influxdata/influxdb/schema"\n'
        f'schema.tagValues(bucket: "{bucket}", tag: "{tag}"{pred})'
    )
    return [r["_value"] for r in _run_flux(flux)]


# --- Grafana (read-only) --------------------------------------------------

def _grafana(path: str, **kwargs):
    r = requests.get(
        config.GRAFANA_URL + path,
        headers={"Authorization": f"Bearer {config.GRAFANA_TOKEN}"},
        timeout=10,
        **kwargs,
    )
    r.raise_for_status()
    return r.json()


def search_dashboards(query: str = "") -> list[dict]:
    """List dashboards (uid + title), optionally filtered by a search string."""
    hits = _grafana("/api/search", params={"query": query, "type": "dash-db"})
    return [{"uid": h["uid"], "title": h["title"]} for h in hits]


def get_dashboard_queries(uid: str) -> list[dict]:
    """The actual queries each panel runs — so an answer can mirror a panel exactly."""
    dash = _grafana(f"/api/dashboards/uid/{uid}")["dashboard"]
    out: list[dict] = []

    def walk(panels):
        for p in panels:
            if p.get("type") == "row" and p.get("panels"):
                walk(p["panels"])
                continue
            for t in p.get("targets", []):
                q = t.get("query") or t.get("rawSql") or ""
                if q:
                    ds = p.get("datasource") or {}
                    out.append({
                        "panel": p.get("title"),
                        "datasource": ds.get("uid") if isinstance(ds, dict) else ds,
                        "query": q,
                    })

    walk(dash.get("panels", []))
    return out


def list_panels(uid: str) -> list[dict]:
    """The renderable panels of a dashboard: panel id + title + type (the id feeds render_panel)."""
    dash = _grafana(f"/api/dashboards/uid/{uid}")["dashboard"]
    out: list[dict] = []

    def walk(panels):
        for p in panels:
            if p.get("type") == "row":
                if p.get("panels"):
                    walk(p["panels"])
                continue
            out.append({
                "id": p.get("id"),
                "title": p.get("title") or "(untitled)",
                "type": p.get("type"),
            })

    walk(dash.get("panels", []))
    return out


# --- Grafana panel rendering (PNG via the image renderer) ------------------

_REL_TIME = re.compile(r"^(?:now-)?(\d+)([smhdw])$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _to_ms(t: str | int) -> int:
    """Grafana /render wants epoch ms. Accept 'now', a relative shorthand
    ('6h', '7d', 'now-30m'), or a raw epoch-ms integer."""
    t = str(t).strip()
    if t in ("", "now"):
        return int(time.time() * 1000)
    if t.lstrip("-").isdigit():
        return int(t)
    m = _REL_TIME.match(t)
    if m:
        return int((time.time() - int(m.group(1)) * _UNIT_S[m.group(2)]) * 1000)
    raise ValueError(
        f"unrecognized time {t!r} (use 'now', a shorthand like '6h'/'7d'/'now-30m', or epoch ms)"
    )


def render_panel(
    uid: str,
    panel_id: int,
    *,
    from_: str | int = "6h",
    to: str | int = "now",
    width: int = 1000,
    height: int = 500,
    tz: str | None = "America/Los_Angeles",
    theme: str = "dark",
) -> bytes:
    """Render one panel to a PNG via Grafana's image renderer. Returns the PNG bytes.

    Uses the same read-only GRAFANA_TOKEN as the rest of ask-dash (a Viewer can
    render panels it can see). Raises if the renderer doesn't return an image —
    e.g. an auth redirect (HTML) or the renderer being down."""
    params: dict = {
        "panelId": panel_id,
        "from": _to_ms(from_),
        "to": _to_ms(to),
        "width": width,
        "height": height,
        "theme": theme,
    }
    if tz:
        params["tz"] = tz
    r = requests.get(
        config.GRAFANA_URL + f"/render/d-solo/{uid}/x",
        headers={"Authorization": f"Bearer {config.GRAFANA_TOKEN}"},
        params=params,
        timeout=config.RENDER_TIMEOUT_S,
    )
    r.raise_for_status()
    ct = r.headers.get("Content-Type", "")
    if "image/png" not in ct:
        raise RuntimeError(
            f"renderer did not return a PNG (Content-Type: {ct or 'none'}); "
            "check GRAFANA_TOKEN/permissions, the uid/panel, or that the renderer is up"
        )
    return r.content


# --- Tool registry (shared by Anthropic tool-use and MCP) -----------------

# name -> (callable, anthropic-style JSON schema)
REGISTRY: dict[str, tuple] = {
    "list_buckets": (
        list_buckets,
        {"type": "object", "properties": {}, "required": []},
        "List the InfluxDB buckets available to query.",
    ),
    "list_measurements": (
        list_measurements,
        {"type": "object",
         "properties": {"bucket": {"type": "string"}},
         "required": ["bucket"]},
        "List the measurements in a bucket.",
    ),
    "list_fields": (
        list_fields,
        {"type": "object",
         "properties": {"bucket": {"type": "string"},
                        "measurement": {"type": "string"}},
         "required": ["bucket"]},
        "List field keys in a bucket (optionally for one measurement).",
    ),
    "list_tag_keys": (
        list_tag_keys,
        {"type": "object",
         "properties": {"bucket": {"type": "string"},
                        "measurement": {"type": "string"}},
         "required": ["bucket"]},
        "List tag keys in a bucket (optionally for one measurement).",
    ),
    "list_tag_values": (
        list_tag_values,
        {"type": "object",
         "properties": {"bucket": {"type": "string"},
                        "tag": {"type": "string"},
                        "measurement": {"type": "string"}},
         "required": ["bucket", "tag"]},
        "List the distinct values of a tag (optionally for one measurement).",
    ),
    "query_influx": (
        query_influx,
        {"type": "object",
         "properties": {"flux": {"type": "string",
                                 "description": "A read-only Flux query."}},
         "required": ["flux"]},
        "Run a read-only Flux query and return the rows. Prefer a bounded range().",
    ),
    "search_dashboards": (
        search_dashboards,
        {"type": "object",
         "properties": {"query": {"type": "string"}},
         "required": []},
        "Search Grafana dashboards by title; returns uid + title.",
    ),
    "get_dashboard_queries": (
        get_dashboard_queries,
        {"type": "object",
         "properties": {"uid": {"type": "string"}},
         "required": ["uid"]},
        "Return the queries each panel of a dashboard runs, so an answer can mirror it.",
    ),
    "list_panels": (
        list_panels,
        {"type": "object",
         "properties": {"uid": {"type": "string"}},
         "required": ["uid"]},
        "List a dashboard's panels (id + title + type); the id feeds panel rendering.",
    ),
}


def anthropic_tools() -> list[dict]:
    """Tool schemas in Anthropic Messages-API shape."""
    return [
        {"name": name, "description": desc, "input_schema": schema}
        for name, (_fn, schema, desc) in REGISTRY.items()
    ]


def dispatch(name: str, arguments: dict) -> str:
    """Execute a tool by name; return a JSON string (errors are returned, not raised)."""
    fn, _schema, _desc = REGISTRY[name]
    try:
        result = fn(**(arguments or {}))
        return json.dumps(result, default=str)
    except Exception as e:  # surface the error to the model so it can recover
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
