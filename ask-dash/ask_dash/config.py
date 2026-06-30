"""Configuration, loaded from environment (with a minimal .env loader).

Real environment variables always win over .env, so the Discord-bot container
can override INFLUX_URL/GRAFANA_URL (container-network names) while the host
tools (MCP server, CLI) fall back to the localhost values in .env.
"""

import os
from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env():
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

# InfluxDB — read-only token scoped to all buckets (no write/delete).
INFLUX_URL = os.environ.get("INFLUX_URL", "http://localhost:8086")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
INFLUX_READ_TOKEN = os.environ.get("INFLUX_READ_TOKEN", "")

# Grafana — Viewer service-account token (read dashboards/datasources only).
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3001")
GRAFANA_TOKEN = os.environ.get("GRAFANA_TOKEN", "")

# Anthropic — used by the Discord bot + CLI summary loop (not the MCP path,
# where the caller's Claude CLI session is the model).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Discord — gateway bot (outbound; no public endpoint needed).
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
ALLOWED_USER_IDS = {
    x.strip() for x in os.environ.get("DISCORD_ALLOWED_USER_IDS", "").split(",") if x.strip()
}

# Guard rails — a vague question must not trigger a full-history scan.
QUERY_TIMEOUT_S = int(os.environ.get("QUERY_TIMEOUT_S", "20"))
MAX_ROWS = int(os.environ.get("MAX_ROWS", "500"))

# Panel PNG rendering — headless Chromium is slow (~5–7s) and slower under host
# load, so allow generous headroom before giving up.
RENDER_TIMEOUT_S = int(os.environ.get("RENDER_TIMEOUT_S", "45"))
