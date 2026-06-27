#!/usr/bin/env python3
"""OneBusAway service alerts -> Discord, for a handful of watched routes.

## The endpoint gotcha (why this polls per-route)

The old version called `situations-for-agency/{agency}` — **not a real
OneBusAway REST method** (it 404s with a valid key). OBA has no "all situations
for an agency" call. The working approach is to poll **per route**:

    GET /api/where/trips-for-route/{routeId}.json?key=KEY
        &includeStatus=false&includeSchedule=false

and read the alerts from `data.references.situations[]` (the situations are in
the shared *references* block, NOT under `entry.situations`). We sweep every
watched route, dedupe situations by `id` (one situation often affects several
routes), space requests ~1.5s apart (OBA 429s if hammered), and diff against a
state file to post new alerts and clear resolved ones.

Severity is OBA's camelCase enum (noImpact / unknown / verySlight / slight /
moderate / severe / verySevere) — NOT the GTFS-RT effect enums (DETOUR/…) the
old colour map keyed on, which never matched. severe/verySevere -> red,
moderate -> orange, everything else -> yellow.

OBA key: env OBA_API_KEY (run-transit.sh sources it from the transit_tracker
app config). Discord webhook: env -> grafana/.env.

    OBA_API_KEY=… uv run --with httpx transit_discord.py --dry
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

OBA_API_BASE = "https://api.pugetsound.onebusaway.org/api/where"

# routeId -> display name
WATCHED_ROUTES: dict[str, str] = {
    "1_100252": "Route 7",
    "1_100228": "Route 8",
    "1_100113": "Route 14",
    "1_102574": "Route 554",
    "40_100479": "1 Line",
    "40_2LINE": "2 Line",
}

STATE_DIR = Path.home() / ".local" / "share" / "transit-discord"
STATE_FILE = STATE_DIR / "state.json"
STALE_DAYS = 7

# OBA spaces requests to avoid 429s.
REQUEST_SPACING_S = 1.5

# Severity -> colour. Keys are OBA's camelCase severity enum.
COLOR_RED = 0xE74C3C
COLOR_ORANGE = 0xE67E22
COLOR_YELLOW = 0xF1C40F
CLEARED_COLOR = 0x2ECC71  # green


def color_for_severity(severity: str | None) -> int:
    """Map OBA camelCase severity to an embed colour."""
    s = (severity or "").strip()
    if s in ("severe", "verySevere"):
        return COLOR_RED
    if s == "moderate":
        return COLOR_ORANGE
    return COLOR_YELLOW


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _get_oba_api_key() -> str:
    return os.environ.get("OBA_API_KEY", "TEST")


def _get_discord_webhook_url() -> str | None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        return url
    env_path = Path.home() / "dev" / "observability" / "grafana" / ".env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("DISCORD_WEBHOOK_URL="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return None


# ---------------------------------------------------------------------------
# Situation helpers (pure)
# ---------------------------------------------------------------------------

def _value(field) -> str:
    """OBA wraps translatable strings as {lang, value}."""
    if isinstance(field, dict):
        return str(field.get("value", "")).strip()
    return str(field or "").strip()


def _truncate(text: str, length: int = 400) -> str:
    text = (text or "").strip()
    if len(text) <= length:
        return text
    return text[: length - 1] + "…"


def affected_route_names(situation: dict) -> list[str]:
    """Watched-route display names this situation touches, via allAffects[]."""
    names: list[str] = []
    for affect in situation.get("allAffects", []) or []:
        rid = affect.get("routeId")
        if rid in WATCHED_ROUTES and WATCHED_ROUTES[rid] not in names:
            names.append(WATCHED_ROUTES[rid])
    return names


def _format_active_window(situation: dict) -> str:
    windows = situation.get("activeWindows") or []
    if not windows:
        return ""
    w = windows[0]
    parts = []
    for key in ("from", "to"):
        ms = w.get(key)
        if ms:
            dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            parts.append(dt.strftime("%Y-%m-%d %H:%M UTC"))
        else:
            parts.append("—")
    return f"{parts[0]} → {parts[1]}"


def build_alert_embed(situation: dict) -> dict:
    """Build a Discord embed for a new/active situation (pure)."""
    summary_text = _value(situation.get("summary"))
    desc_text = _value(situation.get("description"))
    url_text = _value(situation.get("url"))
    severity = situation.get("severity") or "unknown"
    reason = situation.get("reason") or ""
    affected = affected_route_names(situation)
    route_label = ", ".join(affected) if affected else "Watched routes"

    embed: dict = {
        "title": f"Transit Alert — {route_label}",
        "color": color_for_severity(situation.get("severity")),
        "fields": [],
    }
    if url_text:
        embed["url"] = url_text
    if summary_text:
        embed["fields"].append({"name": "Summary", "value": _truncate(summary_text, 256), "inline": False})
    if desc_text:
        embed["fields"].append({"name": "Details", "value": _truncate(desc_text), "inline": False})
    if affected:
        embed["fields"].append({"name": "Affected", "value": ", ".join(affected), "inline": True})
    embed["fields"].append({"name": "Severity", "value": severity, "inline": True})
    if reason:
        embed["fields"].append({"name": "Reason", "value": reason, "inline": True})
    window = _format_active_window(situation)
    if window:
        embed["fields"].append({"name": "Active", "value": window, "inline": False})
    return embed


def build_cleared_embed(state_entry: dict) -> dict:
    route_label = state_entry.get("route_label", "Transit")
    summary = state_entry.get("summary", "")
    return {
        "title": f"Cleared — {route_label}",
        "description": summary or "A previous service alert has been resolved.",
        "color": CLEARED_COLOR,
    }


# ---------------------------------------------------------------------------
# OBA fetching
# ---------------------------------------------------------------------------

def fetch_situations_for_route(client: httpx.Client, route_id: str, api_key: str) -> list[dict]:
    """Return situations from trips-for-route for one route (data.references.situations)."""
    url = f"{OBA_API_BASE}/trips-for-route/{route_id}.json"
    params = {
        "key": api_key,
        "includeStatus": "false",
        "includeSchedule": "false",
    }
    for attempt in range(2):
        try:
            resp = client.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                time.sleep(2 + attempt)
                continue
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[warn] OBA request failed for {route_id}: {exc}", file=sys.stderr)
            return []
        data = resp.json()
        refs = data.get("data", {}).get("references", {})
        situations = refs.get("situations", [])
        return situations if isinstance(situations, list) else []
    return []


def collect_current_situations(client: httpx.Client, api_key: str, spacing: float = REQUEST_SPACING_S) -> dict[str, dict]:
    """Sweep every watched route, returning {situation_id: situation} deduped by id."""
    current: dict[str, dict] = {}
    routes = list(WATCHED_ROUTES)
    for i, route_id in enumerate(routes):
        for sit in fetch_situations_for_route(client, route_id, api_key):
            sid = sit.get("id")
            if sid and sid not in current:
                current[sid] = sit
        if spacing and i < len(routes) - 1:
            time.sleep(spacing)
    return current


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def prune_stale(state: dict) -> dict:
    cutoff = time.time() - STALE_DAYS * 86400
    return {k: v for k, v in state.items() if v.get("first_seen", 0) > cutoff}


def _state_entry(situation: dict) -> dict:
    affected = affected_route_names(situation)
    return {
        "first_seen": time.time(),
        "route_label": ", ".join(affected) if affected else "Transit",
        "summary": _value(situation.get("summary")),
    }


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def post_to_discord(client: httpx.Client, webhook_url: str, embeds: list[dict]) -> None:
    if not embeds:
        return
    for i in range(0, len(embeds), 10):
        batch = embeds[i: i + 10]
        try:
            resp = client.post(webhook_url, json={"embeds": batch}, timeout=10)
            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 2)
                time.sleep(retry_after)
                client.post(webhook_url, json={"embeds": batch}, timeout=10)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Discord post failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry: bool = False) -> None:
    api_key = _get_oba_api_key()
    if api_key == "TEST":
        print("[warn] OBA_API_KEY is the dud 'TEST' — results will be empty", file=sys.stderr)

    webhook_url = _get_discord_webhook_url()
    if not webhook_url and not dry:
        print("[error] No DISCORD_WEBHOOK_URL configured.", file=sys.stderr)
        sys.exit(1)

    state = prune_stale(load_state())

    with httpx.Client() as client:
        current = collect_current_situations(client, api_key)
        current_ids = set(current)
        previous_ids = set(state)

        new_ids = current_ids - previous_ids
        cleared_ids = previous_ids - current_ids

        new_embeds = [build_alert_embed(current[i]) for i in sorted(new_ids)]
        cleared_embeds = [build_cleared_embed(state[i]) for i in sorted(cleared_ids)]

        # Update state: add new, refresh active, drop cleared.
        for sid in new_ids:
            state[sid] = _state_entry(current[sid])
        for sid in current_ids:
            state.setdefault(sid, _state_entry(current[sid]))
        for sid in cleared_ids:
            state.pop(sid, None)

        all_embeds = new_embeds + cleared_embeds

        if dry:
            print(f"Active situations: {len(current_ids)} "
                  f"({', '.join(sorted(current_ids)) or 'none'})")
            print(f"New: {len(new_embeds)}  Cleared: {len(cleared_embeds)}")
            for e in all_embeds:
                print(json.dumps(e, indent=2))
        elif all_embeds and webhook_url:
            post_to_discord(client, webhook_url, all_embeds)

        # Don't mutate the live state on a dry run (verification stays side-effect free).
        if not dry:
            save_state(state)

    parts = []
    if new_embeds:
        parts.append(f"{len(new_embeds)} new")
    if cleared_embeds:
        parts.append(f"{len(cleared_embeds)} cleared")
    print(f"[info] {'Posted: ' + ', '.join(parts) if parts else 'No changes.'} "
          f"{len(current_ids)} active alert(s) tracked.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Transit service alerts -> Discord")
    parser.add_argument("--dry", action="store_true", help="Print embeds instead of posting")
    args = parser.parse_args()
    run(dry=args.dry)
