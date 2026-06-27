#!/usr/bin/env python3
"""GitHub activity -> Discord.

Polls `gh api /users/tommyroar/events` and posts an embed for new pull requests
(opened / merged) and new issues (opened). Tracks seen event IDs in a state file
so a 30-minute Nomad cadence never double-posts.

## The trim gotcha (why this script fetches the full PR)

`/users/<u>/events` returns a **trimmed** `pull_request` object — only
`base/head/id/number/url`, with **no title, html_url, user, or merged**. The old
version read `pr["title"]` straight from the event, so every embed said
"Untitled PR". The fix: take `pull_request.url` (the api.github.com REST URL),
strip the host, and `gh api <path>` to fetch the *full* PR, then read
title / user.login / html_url / merged from that.

PR merges arrive as a **synthetic** `action == "merged"` event (GitHub emits it
alongside the `closed` event). To avoid double-posting a merge we render the
`merged` event (purple) and *skip* the matching `closed` event when the full PR
says `merged == true`; a genuinely-closed-unmerged PR still posts (grey).

WorkflowRunEvent / DeploymentEvent are **not** part of the user-events feed, so
those branches are gone — CI/deploy notifications come from Grafana alerting and
the dev-status watcher, not here.

Reads DISCORD_WEBHOOK_URL from the env, falling back to grafana/.env.

    uv run --with httpx github_discord.py --dry
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

GITHUB_USER = "tommyroar"
STATE_DIR = Path.home() / ".local" / "share" / "github-discord"
STATE_FILE = STATE_DIR / "state.json"
MAX_SEEN_IDS = 500
DISCORD_EMBEDS_PER_REQUEST = 10

# Embed colours
COLOR_PR_OPEN = 0x2ECC71   # green
COLOR_MERGE = 0x6F42C1     # purple
COLOR_PR_CLOSED = 0x95A5A6  # grey (closed without merge)
COLOR_ISSUE_OPEN = 0x1ABC9C  # teal


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_webhook_url() -> str | None:
    """Discord webhook from env, falling back to grafana/.env."""
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        return url
    fallback = Path.home() / "dev" / "observability" / "grafana" / ".env"
    if fallback.exists():
        for line in fallback.read_text().splitlines():
            line = line.strip()
            if line.startswith("DISCORD_WEBHOOK_URL="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_seen_ids() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text())
        return set(data.get("seen_ids", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen_ids(seen_ids: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if len(seen_ids) > MAX_SEEN_IDS:
        seen_ids = seen_ids[-MAX_SEEN_IDS:]
    STATE_FILE.write_text(json.dumps({"seen_ids": seen_ids}))


# ---------------------------------------------------------------------------
# GitHub fetching (subprocess `gh api`)
# ---------------------------------------------------------------------------

def fetch_events() -> list[dict]:
    """Fetch the user's recent events via `gh api`."""
    result = subprocess.run(
        ["gh", "api", f"/users/{GITHUB_USER}/events"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[github-discord] gh api failed: {result.stderr.strip()}", file=sys.stderr)
        return []
    try:
        data = json.loads(result.stdout)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        print("[github-discord] failed to parse gh api output", file=sys.stderr)
        return []


def api_path(api_url: str) -> str:
    """Turn a full api.github.com URL into a `gh api` path.

    >>> api_path("https://api.github.com/repos/o/r/pulls/7")
    'repos/o/r/pulls/7'
    """
    for prefix in ("https://api.github.com/", "http://api.github.com/"):
        if api_url.startswith(prefix):
            return api_url[len(prefix):]
    return api_url.lstrip("/")


def fetch_pr(path: str) -> dict | None:
    """Fetch the full PR/issue object via `gh api <path>`."""
    result = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[github-discord] gh api {path} failed: {result.stderr.strip()}", file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Embed building (pure)
# ---------------------------------------------------------------------------

def build_pr_embed(action: str, repo_name: str, full_pr: dict) -> dict:
    """Build a Discord embed from a *fully-fetched* PR object."""
    title = full_pr.get("title") or "Untitled PR"
    author = (full_pr.get("user") or {}).get("login", "unknown")
    html_url = full_pr.get("html_url", "")
    number = full_pr.get("number", "")

    if action == "merged":
        verb, color = "Merged", COLOR_MERGE
    elif action == "opened":
        verb, color = "Opened", COLOR_PR_OPEN
    else:  # closed (unmerged) — caller has confirmed merged is false
        verb, color = "Closed", COLOR_PR_CLOSED

    return {
        "title": f"PR {verb} #{number}: {title}",
        "description": f"**Repo:** {repo_name}\n**Author:** {author}",
        "url": html_url,
        "color": color,
    }


def build_issue_embed(repo_name: str, issue: dict) -> dict:
    title = issue.get("title") or "Untitled issue"
    author = (issue.get("user") or {}).get("login", "unknown")
    html_url = issue.get("html_url", "")
    number = issue.get("number", "")
    return {
        "title": f"Issue Opened #{number}: {title}",
        "description": f"**Repo:** {repo_name}\n**Author:** {author}",
        "url": html_url,
        "color": COLOR_ISSUE_OPEN,
    }


def event_to_embed(event: dict, pr_fetcher) -> dict | None:
    """Convert one event to an embed, or None if not relevant.

    `pr_fetcher(path)` fetches the full PR/issue object (injected for testing).
    """
    etype = event.get("type", "")
    payload = event.get("payload", {})
    repo_name = event.get("repo", {}).get("name", "unknown")

    if etype == "PullRequestEvent":
        action = payload.get("action", "")
        if action not in ("opened", "merged", "closed"):
            return None
        pr = payload.get("pull_request", {})
        api_url = pr.get("url")
        if not api_url:
            return None
        full = pr_fetcher(api_path(api_url))
        if full is None:
            return None
        # A merge emits BOTH `merged` (synthetic) and `closed`; render the merge,
        # drop the duplicate close.
        if action == "closed" and full.get("merged"):
            return None
        return build_pr_embed(action, repo_name, full)

    if etype == "IssuesEvent":
        if payload.get("action", "") != "opened":
            return None
        issue = payload.get("issue", {})
        if not issue.get("title"):
            return None
        return build_issue_embed(repo_name, issue)

    return None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def post_embeds(webhook_url: str, embeds: list[dict], dry: bool = False) -> None:
    for i in range(0, len(embeds), DISCORD_EMBEDS_PER_REQUEST):
        batch = embeds[i: i + DISCORD_EMBEDS_PER_REQUEST]
        if dry:
            print(f"[dry-run] would post {len(batch)} embed(s):")
            for embed in batch:
                print(f"  - {embed.get('title', '(no title)')}")
            continue
        try:
            resp = httpx.post(webhook_url, json={"embeds": batch}, timeout=10)
            if resp.status_code >= 400:
                print(f"[github-discord] Discord {resp.status_code}: {resp.text}", file=sys.stderr)
        except httpx.HTTPError as exc:
            print(f"[github-discord] Discord request failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub activity -> Discord")
    parser.add_argument("--dry", action="store_true", help="Print embeds instead of posting")
    args = parser.parse_args()

    webhook_url = get_webhook_url()
    if not webhook_url and not args.dry:
        print("[github-discord] No DISCORD_WEBHOOK_URL found", file=sys.stderr)
        sys.exit(1)

    events = fetch_events()
    if not events:
        print("[github-discord] No events fetched")
        return

    seen_ids = load_seen_ids()
    ordered_ids = [e.get("id", "") for e in reversed(events) if e.get("id")]
    new_embeds: list[dict] = []

    for event in events:
        eid = event.get("id", "")
        if not eid or eid in seen_ids:
            continue
        seen_ids.add(eid)
        embed = event_to_embed(event, fetch_pr)
        if embed:
            new_embeds.append(embed)

    # Persist in roughly chronological order so the cap keeps the newest IDs.
    # Don't mutate the live state on a dry run (verification stays side-effect free).
    if not args.dry:
        save_seen_ids([i for i in ordered_ids if i in seen_ids] or list(seen_ids))

    if not new_embeds:
        print("[github-discord] No new relevant events")
        return

    print(f"[github-discord] Posting {len(new_embeds)} embed(s)")
    post_embeds(webhook_url or "", new_embeds, dry=args.dry)


if __name__ == "__main__":
    main()
