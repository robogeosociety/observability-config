"""CLI front-end — a local utility for the observability stack.

Subcommands:
  ask <question>          Ask the read-only agent (the default — bare
                          `ask-dash "..."` still works for cron/launchd jobs).
  dashboards [query]      List dashboards (uid + title).
  panels <uid>            List a dashboard's panels (id + title + type).
  render <uid> <panel>    Render a panel to a PNG and open it (Option B, but as a
                          local utility): keeps Grafana headless, no Tailscale UI.

The render path uses Grafana's image renderer with the same read-only token as
everything else here. (Alternatively, drive the agent from a Claude CLI session
via the MCP server — see README.)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from . import agent, tools

_SUBCOMMANDS = {"ask", "dashboards", "panels", "render"}


def _cmd_ask(args):
    print(agent.ask(" ".join(args.question)))


def _cmd_dashboards(args):
    for d in tools.search_dashboards(args.query or ""):
        print(f"{d['uid']}\t{d['title']}")


def _cmd_panels(args):
    for p in tools.list_panels(args.uid):
        print(f"{p['id']}\t{p['type']}\t{p['title']}")


def _cmd_render(args):
    png = tools.render_panel(
        args.uid, args.panel,
        from_=args.from_, to=args.to,
        width=args.width, height=args.height,
        tz=args.tz, theme=args.theme,
    )
    out = Path(args.out) if args.out else (
        Path(tempfile.gettempdir()) / f"ask-dash-{args.uid}-p{args.panel}.png"
    )
    out.write_bytes(png)
    print(out)
    if args.open:
        subprocess.run(["open", str(out)], check=False)


def main():
    # Backward compat: `ask-dash "free text question"` (no subcommand) → `ask`.
    argv = sys.argv[1:]
    if argv and argv[0] not in _SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["ask", *argv]

    ap = argparse.ArgumentParser(prog="ask-dash", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("ask", help="Ask the read-only agent.")
    a.add_argument("question", nargs="+", help="The question to ask (read-only).")
    a.set_defaults(func=_cmd_ask)

    d = sub.add_parser("dashboards", help="List dashboards (uid + title).")
    d.add_argument("query", nargs="?", help="Optional title filter.")
    d.set_defaults(func=_cmd_dashboards)

    p = sub.add_parser("panels", help="List a dashboard's panels (id + title + type).")
    p.add_argument("uid", help="Dashboard uid (from `dashboards`).")
    p.set_defaults(func=_cmd_panels)

    r = sub.add_parser("render", help="Render a panel to a PNG and open it.")
    r.add_argument("uid", help="Dashboard uid (from `dashboards`).")
    r.add_argument("panel", type=int, help="Panel id (from `panels`).")
    r.add_argument("--from", dest="from_", default="6h",
                   metavar="T", help="Start: 'now', '6h'/'7d'/'now-30m', or epoch ms (default 6h).")
    r.add_argument("--to", default="now",
                   metavar="T", help="End: same formats (default now).")
    r.add_argument("--width", type=int, default=1000)
    r.add_argument("--height", type=int, default=500)
    r.add_argument("--tz", default="America/Los_Angeles",
                   help="Display timezone (default America/Los_Angeles).")
    r.add_argument("--theme", default="dark", choices=["dark", "light"])
    r.add_argument("--out", metavar="PATH",
                   help="Write PNG here (default a temp file).")
    r.add_argument("--open", dest="open", action="store_true", default=True,
                   help="Open the PNG after rendering (default).")
    r.add_argument("--no-open", dest="open", action="store_false",
                   help="Just write the file; don't open it.")
    r.set_defaults(func=_cmd_render)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        raise SystemExit(2)
    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
