#!/usr/bin/env python3
"""discord-claude-heatmap — live Claude token-usage heatmap in #ops.

Replaces the cumulative milestone notifier (discord-claude-tokens) with a dynamic,
tick-updated view: a GitHub-contributions-style heatmap of output tokens, rows =
projects, columns = the last 12 hourly buckets, color = intensity (⬛ idle → 🟦🟩🟨🟧
→ 🟥 hot). Data is the `tokens` measurement in the `claude_code` bucket (the same
series the claude-usage dashboard reads). One self-editing Discord message every tick.

Pure parse_matrix() + render() are hermetically tested; the rest is thin IO. Env:
  INFLUX_URL (default http://localhost:8086), INFLUX_ORG (home), INFLUX_READ_TOKEN
  DISCORD_CHANNEL_ID, DISCORD_BOT_TOKEN   (both may come from STATE/.env)
  STATE_DIR (default /state), INTERVAL (default 60), HEATMAP_HOURS (default 12)
"""
import json, os, sys, time, urllib.request, pathlib, datetime

HEAT = ["⬛", "🟦", "🟩", "🟨", "🟧", "🟥"]   # 0 .. hot
ROWS = 8                                       # top-N projects shown
HOURS = int(os.environ.get("HEATMAP_HOURS", "12"))

INFLUX_URL = os.environ.get("INFLUX_URL", "http://localhost:8086").rstrip("/")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
CHANNEL = os.environ.get("DISCORD_CHANNEL_ID", "1520850187105341710")
STATE_DIR = pathlib.Path(os.environ.get("STATE_DIR", "/state"))
INTERVAL = int(os.environ.get("INTERVAL", "60"))

FLUX = (
    'from(bucket:"claude_code") |> range(start:-%dh) '
    '|> filter(fn:(r)=>r._measurement=="tokens" and r._field=="output_tokens") '
    '|> group(columns:["project"]) '
    '|> aggregateWindow(every:1h, fn:sum, createEmpty:true) '
    '|> keep(columns:["project","_time","_value"])'
) % HOURS


def parse_matrix(csv_text: str):
    """InfluxDB CSV -> (matrix {project:{time:val}}, sorted_times list). Pure."""
    matrix, times = {}, set()
    header = None
    for raw in csv_text.splitlines():
        line = raw.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        cols = line.split(",")
        if header is None:
            header = cols
            continue
        row = dict(zip(header, cols))
        proj = (row.get("project") or "").strip() or "—"
        t = (row.get("_time") or "").strip()
        if not t:
            continue
        v = (row.get("_value") or "").strip()
        try:
            val = float(v) if v else 0.0
        except ValueError:
            val = 0.0
        matrix.setdefault(proj, {})[t] = val
        times.add(t)
    return matrix, sorted(times)


def _human(n: float) -> str:
    for u in ("", "K", "M"):
        if n < 1000:
            return f"{n:.0f}{u}"
        n /= 1000
    return f"{n:.0f}B"


def _cell(v: float, mx: float) -> str:
    if v <= 0 or mx <= 0:
        return HEAT[0]
    frac = v / mx
    for i, thr in enumerate([0.02, 0.1, 0.3, 0.6]):
        if frac <= thr:
            return HEAT[i + 1]
    return HEAT[5]


def render(matrix: dict, times: list) -> str:
    """(matrix, times) -> Discord heatmap message. Pure."""
    times = times[-HOURS:]
    totals = {p: sum(matrix[p].get(t, 0) for t in times) for p in matrix}
    top = sorted(totals, key=lambda p: -totals[p])[:ROWS]
    mx = max((matrix[p].get(t, 0) for p in top for t in times), default=0)
    lines = []
    for p in top:
        grid = "".join(_cell(matrix[p].get(t, 0), mx) for t in times)
        lines.append(f"{grid} {p[:22]} {_human(totals[p])}")
    grand = sum(totals.values())
    span = len(times)
    head = f"🔥 **Claude tokens — heatmap** · last {span}h hourly · {_human(grand)} out"
    scale = "scale ⬛ idle  " + "".join(HEAT[1:]) + "  hot"
    body = "\n".join(lines) if lines else "(no token activity in window)"
    return f"{head}\n\n{body}\n\n{scale}"


# --- IO ---------------------------------------------------------------------

def _from_env(key):
    v = os.environ.get(key)
    if v:
        return v
    envf = STATE_DIR / ".env"
    if envf.exists():
        for ln in envf.read_text().splitlines():
            if ln.strip().startswith(key + "="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def fetch():
    token = _from_env("INFLUX_READ_TOKEN")
    if not token:
        raise SystemExit("INFLUX_READ_TOKEN not set")
    req = urllib.request.Request(
        f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}", data=FLUX.encode(), method="POST",
        headers={"Authorization": f"Token {token}", "Accept": "application/csv",
                 "Content-Type": "application/vnd.flux"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return parse_matrix(r.read().decode())


def _api(method, path, tok, body=None):
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json",
                 "User-Agent": "discord-claude-heatmap/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _message_id(tok):
    state = STATE_DIR / "state.json"
    if state.exists():
        mid = json.loads(state.read_text()).get("message_id")
        if mid:
            return mid
    m, t = fetch()
    msg = _api("POST", f"/channels/{CHANNEL}/messages", tok, {"content": render(m, t)})
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"message_id": msg["id"]}))
    return msg["id"]


def main():
    tok = _from_env("DISCORD_BOT_TOKEN") or sys.exit("DISCORD_BOT_TOKEN not set")
    mid = _message_id(tok)
    sys.stderr.write(f"discord-claude-heatmap: editing message {mid} every {INTERVAL}s\n")
    once = "--once" in sys.argv
    while True:
        try:
            m, t = fetch()
            _api("PATCH", f"/channels/{CHANNEL}/messages/{mid}", tok, {"content": render(m, t)})
        except Exception as e:  # noqa: BLE001 — a transient InfluxDB/Discord blip shouldn't kill the loop
            sys.stderr.write(f"update failed: {e}\n")
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
