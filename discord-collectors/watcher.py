#!/usr/bin/env python3
"""Deployment-status watcher -> Discord (long-running launchd daemon).

Polls the dev-status server every 30s and posts to Discord when a deployment
transitions UP<->DOWN (debounced) or when dev-status itself goes away/recovers.

## The dev-status schema gotcha (same bug the digest had)

dev-status returns `{"total":…, "up":…, "down":…, "summary":[…],
"deployments":[{"name":…, "up":1|0, "port":…}, …]}`. The old `_parse_services`
iterated the top-level dict and called `.get()` on the `summary` int, crashing.
The fix reads `data["deployments"]` and each entry's `up` (1/0) field.

## Why a launchd plist points at an internal-disk copy

This is a launchd KeepAlive daemon, but **macOS TCC blocks launchd from reading
`/Volumes`** — so it can't run this file in-repo and can't read grafana/.env
there. Deploy step (documented in README): copy this file to
`~/.local/share/discord-collectors/watcher.py` and run it from there. The webhook
is injected via the plist's `EnvironmentVariables` (a placeholder in the
committed plist — the real secret is filled in at deploy time, never committed).
The uv cache stays on the internal disk (no UV_CACHE_DIR=/Volumes — launchd
can't write there).

    uv run --with httpx watcher.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _read_dotenv(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    p = Path(path).expanduser()
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("\"'")
    return env


def load_config() -> dict:
    # On the internal-disk deploy this .env isn't readable (TCC), so the webhook
    # normally comes from the plist EnvironmentVariables; the .env is just a
    # convenience for running in-repo during testing.
    grafana_env = _read_dotenv("~/dev/observability/grafana/.env")
    return {
        "dev_status_url": os.environ.get("DEV_STATUS_URL", "http://localhost:8077"),
        "discord_webhook_url": os.environ.get("DISCORD_WEBHOOK_URL", grafana_env.get("DISCORD_WEBHOOK_URL", "")),
    }


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------

def parse_services(data) -> dict[str, str]:
    """Turn the dev-status payload into {name: "UP"|"DOWN"} (deduped, UP wins)."""
    services: dict[str, str] = {}
    if not isinstance(data, dict):
        return services
    for entry in data.get("deployments", []) or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        is_up = entry.get("up") == 1 or entry.get("up") is True
        if services.get(name) == "UP":
            continue  # UP wins over a duplicate DOWN
        services[name] = "UP" if is_up else "DOWN"
    return services


def fetch_services(url: str) -> Optional[dict[str, str]]:
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        return parse_services(resp.json())
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_ORANGE = 0xE67E22
COLOR_GREY = 0x95A5A6


def _post_embed(webhook_url: str, title: str, description: str, color: int, dry_run: bool = False) -> None:
    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "ops-watcher"},
        }]
    }
    if dry_run:
        print(f"[dry-run] {json.dumps(payload)}")
        return
    try:
        resp = httpx.post(webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
        print(f"[ok] Discord {resp.status_code}: {title}")
    except Exception as exc:  # noqa: BLE001
        print(f"[error] Discord post failed: {exc}", file=sys.stderr)


def notify_service_up(webhook, name, dry_run):
    _post_embed(webhook, f"✅ {name} is UP", f"Service **{name}** is now healthy.", COLOR_GREEN, dry_run)


def notify_service_down(webhook, name, dry_run):
    _post_embed(webhook, f"\U0001F534 {name} is DOWN",
                f"Service **{name}** has been down for 2 consecutive polls.", COLOR_RED, dry_run)


def notify_service_disappeared(webhook, name, dry_run):
    _post_embed(webhook, f"\U0001F7E0 {name} disappeared",
                f"Service **{name}** is no longer reported by dev-status.", COLOR_ORANGE, dry_run)


def notify_server_unreachable(webhook, dry_run):
    _post_embed(webhook, "⚠️ dev-status unreachable",
                "The dev-status server at `localhost:8077` is not responding.", COLOR_GREY, dry_run)


def notify_server_recovered(webhook, dry_run):
    _post_embed(webhook, "✅ dev-status recovered",
                "The dev-status server is responding again.", COLOR_GREEN, dry_run)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class WatcherState:
    def __init__(self) -> None:
        self.services: dict[str, str] = {}
        self.down_counter: dict[str, int] = {}
        self.server_unreachable_alerted = False
        self.initialised = False

    def process(self, current: Optional[dict[str, str]], webhook: str, dry_run: bool) -> None:
        if current is None:
            if not self.server_unreachable_alerted:
                notify_server_unreachable(webhook, dry_run)
                self.server_unreachable_alerted = True
            return

        if self.server_unreachable_alerted:
            notify_server_recovered(webhook, dry_run)
            self.server_unreachable_alerted = False

        if not self.initialised:
            self.services = dict(current)
            self.down_counter = {}
            self.initialised = True
            up_count = sum(1 for s in current.values() if s == "UP")
            print(f"[init] Tracking {len(current)} services "
                  f"({up_count} up, {len(current) - up_count} down)")
            return

        current_names = set(current)
        known_names = set(self.services)

        for name in current_names - known_names:
            self.services[name] = current[name]
            if current[name] == "UP":
                notify_service_up(webhook, name, dry_run)
            else:
                self.down_counter[name] = 1

        for name in known_names - current_names:
            notify_service_disappeared(webhook, name, dry_run)
            del self.services[name]
            self.down_counter.pop(name, None)

        for name in current_names & known_names:
            prev = self.services[name]
            now = current[name]
            if prev == "UP" and now == "DOWN":
                self.down_counter[name] = self.down_counter.get(name, 0) + 1
                if self.down_counter[name] >= 2:
                    notify_service_down(webhook, name, dry_run)
                    self.services[name] = "DOWN"
                    self.down_counter.pop(name, None)
            elif prev == "UP" and now == "UP":
                self.down_counter.pop(name, None)
            elif prev == "DOWN" and now == "UP":
                notify_service_up(webhook, name, dry_run)
                self.services[name] = "UP"
                self.down_counter.pop(name, None)


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    print(f"\n[*] Received {signal.Signals(signum).name}, shutting down ...")
    _shutdown = True


def run_loop(cfg: dict, interval: int, dry_run: bool) -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    webhook = cfg["discord_webhook_url"]
    url = cfg["dev_status_url"]
    state = WatcherState()

    print(f"[*] ops-watcher starting — polling {url} every {interval}s")
    if dry_run:
        print("[*] DRY-RUN mode: embeds will be printed, not posted")

    while not _shutdown:
        current = fetch_services(url)
        state.process(current, webhook, dry_run)
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and not _shutdown:
            time.sleep(0.5)

    print("[*] ops-watcher stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deployment Status Watcher for Discord")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print embeds instead of posting")
    parser.add_argument("--once", action="store_true",
                        help="Run a single poll and exit (for verification)")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval seconds")
    args = parser.parse_args()

    cfg = load_config()
    if not args.dry_run and not cfg["discord_webhook_url"]:
        print("[error] DISCORD_WEBHOOK_URL not set and not found in grafana/.env", file=sys.stderr)
        sys.exit(1)

    if args.once:
        current = fetch_services(cfg["dev_status_url"])
        if current is None:
            print("[once] dev-status unreachable")
        else:
            up = sum(1 for s in current.values() if s == "UP")
            print(f"[once] {len(current)} deployments ({up} up, {len(current) - up} down)")
            for name, status in sorted(current.items()):
                print(f"  {'UP  ' if status == 'UP' else 'DOWN'}  {name}")
        return

    run_loop(cfg, args.interval, args.dry_run)


if __name__ == "__main__":
    main()
