#!/usr/bin/env python3
"""
Collect Claude Code token usage from ~/.claude transcripts → InfluxDB (`claude_code`).

Claude Code writes one JSONL transcript per session under
`~/.claude/projects/<slug>/<session-uuid>.jsonl`. Every `type:"assistant"` line
carries a `message.usage` block (input/output/cache-create/cache-read tokens,
server-tool counts, model, service tier). This reads those lines and writes one
time-series point per assistant message to the `claude_code` bucket, feeding the
"Claude Code — Token Usage" dashboard.

Cost is deliberately NOT derived — transcripts carry no cost, and a per-model price
table silently rots. We track raw token counts only.

Incremental + idempotent: a per-file byte-offset cursor means each run reads only
new lines, so the 5-minute launchd cadence never double-counts. See SPEC-claude-usage.md.

Run via uv (self-contained, no project):
    uv run --no-project python collector.py
    uv run --no-project python collector.py --dry-run        # print line protocol, don't write
    uv run --no-project python collector.py --reset          # ignore cursor, re-read everything
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

TRANSCRIPTS = Path.home() / ".claude" / "projects"
CURSOR_PATH = Path.home() / ".local" / "state" / "claude-usage-collector" / "cursor.json"

# --- parse ------------------------------------------------------------------

def _esc(v) -> str:
    """Escape an InfluxDB line-protocol tag value."""
    return str(v).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def parse_line(obj):
    """One transcript line (already JSON-decoded) -> a usage record, or None to skip.

    Pure and hermetic — the unit test exercises exactly this. Skips anything that
    isn't a real assistant message with non-empty usage (user lines, `<synthetic>`
    UI messages, all-zero usage).
    """
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message") or {}
    model = msg.get("model")
    if not model or model == "<synthetic>":
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    ts = obj.get("timestamp")
    if not ts:
        return None

    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cc = int(usage.get("cache_creation_input_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    stu = usage.get("server_tool_use") or {}
    ws = int(stu.get("web_search_requests") or 0)
    wf = int(stu.get("web_fetch_requests") or 0)
    if inp == 0 and out == 0 and cc == 0 and cr == 0 and ws == 0 and wf == 0:
        return None

    cwd = (obj.get("cwd") or "").rstrip("/")
    return {
        "ts_ms": _iso_to_ms(ts),
        "model": model,
        "project": os.path.basename(cwd) or "unknown",
        "service_tier": usage.get("service_tier") or "unknown",
        "session": obj.get("sessionId") or "unknown",
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_tokens": cc,
        "cache_read_tokens": cr,
        "total_tokens": inp + out + cc + cr,
        "web_search_requests": ws,
        "web_fetch_requests": wf,
    }


def _iso_to_ms(ts: str) -> int:
    """ISO8601 (e.g. 2026-05-30T13:39:07.597Z) -> epoch milliseconds."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def build_line(rec) -> str:
    """A usage record -> one InfluxDB line-protocol string (measurement `tokens`)."""
    tags = (
        "source=claude-cli"
        f",model={_esc(rec['model'])}"
        f",project={_esc(rec['project'])}"
        f",service_tier={_esc(rec['service_tier'])}"
        f",session={_esc(rec['session'])}"
    )
    fields = (
        f"input_tokens={rec['input_tokens']}i"
        f",output_tokens={rec['output_tokens']}i"
        f",cache_creation_tokens={rec['cache_creation_tokens']}i"
        f",cache_read_tokens={rec['cache_read_tokens']}i"
        f",total_tokens={rec['total_tokens']}i"
        f",web_search_requests={rec['web_search_requests']}i"
        f",web_fetch_requests={rec['web_fetch_requests']}i"
        ",messages=1i"
    )
    return f"tokens,{tags} {fields} {rec['ts_ms']}"


# --- cursor / scan ----------------------------------------------------------

def load_cursor():
    try:
        return json.loads(CURSOR_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_cursor(cursor):
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(json.dumps(cursor))


def scan(cursor, reset=False):
    """Read new lines from every transcript since the cursor; return (lines, new_cursor)."""
    lines = []
    new_cursor = {} if reset else dict(cursor)
    for path in sorted(TRANSCRIPTS.glob("**/*.jsonl")):
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        start = 0 if reset else int(cursor.get(key, 0))
        if start > size:  # file was rewritten/compacted — re-read from the top
            start = 0
        if start == size:
            new_cursor[key] = size
            continue
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(start)
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = parse_line(json.loads(raw))
                except (ValueError, KeyError, TypeError):
                    continue
                if rec:
                    lines.append(build_line(rec))
            new_cursor[key] = fh.tell()
    return lines, new_cursor


# --- write ------------------------------------------------------------------

def influx_write(lines):
    url = os.environ.get("INFLUX_URL", "http://localhost:8086").rstrip("/")
    org = os.environ.get("INFLUX_ORG", "home")
    bucket = os.environ.get("INFLUX_CLAUDE_USAGE_BUCKET", "claude_code")
    token = os.environ.get("INFLUX_CLAUDE_USAGE_TOKEN") or os.environ.get("INFLUX_ADMIN_TOKEN")
    if not token:
        raise SystemExit("No InfluxDB token (set INFLUX_CLAUDE_USAGE_TOKEN in influxdb/.env)")
    if not lines:
        return 0
    endpoint = f"{url}/api/v2/write?org={org}&bucket={bucket}&precision=ms"
    for i in range(0, len(lines), 5000):
        chunk = "\n".join(lines[i : i + 5000]).encode()
        req = urllib.request.Request(
            endpoint, data=chunk, method="POST",
            headers={"Authorization": f"Token {token}",
                     "Content-Type": "text/plain; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status not in (200, 204):
                raise SystemExit(f"InfluxDB write HTTP {resp.status}")
    return len(lines)


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print line protocol, don't write or advance cursor")
    ap.add_argument("--reset", action="store_true", help="ignore the cursor and re-read every transcript")
    args = ap.parse_args()

    t0 = time.time()
    cursor = {} if args.reset else load_cursor()
    lines, new_cursor = scan(cursor, reset=args.reset)

    if args.dry_run:
        for ln in lines:
            print(ln)
        print(f"# {len(lines)} points (dry-run, cursor not advanced)", file=sys.stderr)
        return

    n = influx_write(lines)
    save_cursor(new_cursor)
    print(f"wrote {n} points in {time.time() - t0:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
