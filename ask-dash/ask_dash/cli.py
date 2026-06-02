"""CLI front-end — `ask-dash "question"` for scripted summaries/trends.

Drives the same agent loop as the Discord bot, so a launchd/cron weekly job can
do `ask-dash "summarize transit_tracker trends over the last 7 days"` and capture
the answer. (Alternatively, drive it from a Claude CLI session via the MCP
server — see README.)
"""

from __future__ import annotations

import argparse
import sys

from . import agent


def main():
    ap = argparse.ArgumentParser(prog="ask-dash", description=__doc__)
    ap.add_argument("question", nargs="+", help="The question to ask (read-only).")
    args = ap.parse_args()
    try:
        print(agent.ask(" ".join(args.question)))
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
