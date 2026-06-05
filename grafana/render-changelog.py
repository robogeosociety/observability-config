#!/usr/bin/env python3
"""Render the dashboard changelog from git history — no shared file to conflict on.

The old `grafana/changelog.jsonl` was an append-only file every dashboard PR had to
edit, so concurrent PRs from different projects collided on its tail (and the
chronological-order test failed after a merge). Git history is *already* a
conflict-free, ordered, append-only log — each merged PR is a commit. So we derive
the changelog from `git log` on demand instead of maintaining a second copy of it
by hand.

Usage:
    python3 grafana/render-changelog.py            # markdown to stdout, newest first
    python3 grafana/render-changelog.py --limit 40 # cap entries

Reads commits that touched grafana/provisioning/dashboards/** and prints, per commit,
its date, subject (the human summary), short sha, and the dashboards it added/
modified/removed/renamed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DASH_DIR = "grafana/provisioning/dashboards"
REPO = Path(__file__).resolve().parent.parent
SEP = "\x1f"          # unit separator between commit fields
REC = "\x1e"          # record separator between commits
STATUS = {"A": "added", "M": "modified", "D": "removed", "R": "renamed", "C": "copied"}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout


def commits(limit: int | None):
    fmt = f"{REC}%H{SEP}%ad{SEP}%s"
    out = _git(
        "log", f"--pretty=format:{fmt}", "--name-status", "--date=short",
        "--", DASH_DIR,
    )
    for block in out.split(REC):
        block = block.strip("\n")
        if not block:
            continue
        head, _, rest = block.partition("\n")
        sha, date, subject = head.split(SEP)
        changes = []
        for line in rest.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            code = parts[0][0]                       # A/M/D/R (R100 → R)
            path = parts[-1]                         # new path for renames
            if not path.startswith(DASH_DIR):
                continue
            rel = path[len(DASH_DIR) + 1:]           # domain/uid.json
            changes.append((STATUS.get(code, code), rel))
        if changes:
            yield {"sha": sha[:7], "date": date, "subject": subject, "changes": changes}
            if limit and limit > 0:
                limit -= 1
                if limit == 0:
                    return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max entries (0 = all)")
    args = ap.parse_args()

    print("# Dashboard changelog\n")
    print("_Derived from git history — do not edit by hand. Regenerate with "
          "`python3 grafana/render-changelog.py`._\n")
    for c in commits(args.limit or None):
        print(f"## {c['date']} — {c['subject']} [`{c['sha']}`]")
        for action, rel in c["changes"]:
            print(f"- {action}: {rel}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
