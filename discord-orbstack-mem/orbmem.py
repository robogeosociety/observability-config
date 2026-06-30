#!/usr/bin/env python3
"""discord-orbstack-mem — live OrbStack per-container memory treemap in #ops.

The container sibling of discord-mini-mem: same self-editing-message + emoji-treemap
approach, but the data is per-container memory from InfluxDB (`docker_container_mem`
in the `system` bucket — Telegraf's docker input), the SAME source as the Grafana
"Memory map — container usage" panel on the OrbStack dashboard. So the Discord and
Grafana container treemaps can't disagree.

Pure render() + parse_rows() are hermetically tested; the rest is thin IO. Env:
  INFLUX_URL (default http://localhost:8086), INFLUX_ORG (home), INFLUX_READ_TOKEN
  DISCORD_CHANNEL_ID, DISCORD_BOT_TOKEN   (both may come from STATE/.env)
  STATE_DIR (default /state) — holds state.json {message_id}
  INTERVAL  seconds between edits (default 60)
"""
import json, os, sys, time, urllib.request, pathlib

W, H = 12, 8
EMOJI = ["🟧", "🟦", "🟩", "🟪", "🟥", "🟨", "🟫"]

INFLUX_URL = os.environ.get("INFLUX_URL", "http://localhost:8086").rstrip("/")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
CHANNEL = os.environ.get("DISCORD_CHANNEL_ID", "1520850187105341710")
STATE_DIR = pathlib.Path(os.environ.get("STATE_DIR", "/state"))
INTERVAL = int(os.environ.get("INTERVAL", "60"))

FLUX = (
    'from(bucket:"system") |> range(start:-5m) '
    '|> filter(fn:(r)=>r._measurement=="docker_container_mem" and r._field=="usage") '
    '|> last() |> keep(columns:["container_name","_value"]) '
    '|> group() |> sort(columns:["_value"], desc:true)'
)


def squarify(items, x, y, w, h):
    rects = []
    items = sorted([i for i in items if i[1] > 0], key=lambda z: -z[1])

    def worst(row, length, scale):
        s = sum(v for _, v in row) * scale
        if s <= 0:
            return float("inf")
        mx = max(v for _, v in row) * scale
        mn = min(v for _, v in row) * scale
        return max(length * length * mx / (s * s), s * s / (length * length * mn))

    def place(row, x, y, w, h, horiz, scale):
        rs = sum(v for _, v in row) * scale
        if horiz:
            rw = rs / h if h > 0 else 0
            oy = y
            for lab, v in row:
                rh = (v * scale) / rw if rw > 0 else 0
                rects.append([lab, v, x, oy, rw, rh]); oy += rh
        else:
            rh = rs / w if w > 0 else 0
            ox = x
            for lab, v in row:
                rw = (v * scale) / rh if rh > 0 else 0
                rects.append([lab, v, ox, y, rw, rh]); ox += rw

    def lay(items, x, y, w, h):
        if not items or w * h <= 0:
            return
        scale = (w * h) / sum(v for _, v in items)
        row = []; i = 0; horiz = w >= h; length = h if horiz else w
        while i < len(items):
            it = items[i]
            if not row:
                row = [it]; i += 1; continue
            if worst(row, length, scale) >= worst(row + [it], length, scale):
                row.append(it); i += 1
            else:
                place(row, x, y, w, h, horiz, scale)
                rs = sum(v for _, v in row) * scale
                if horiz:
                    x += rs / length; w -= rs / length
                else:
                    y += rs / length; h -= rs / length
                row = []; rem = sum(v for _, v in items[i:])
                if rem > 0 and w * h > 0:
                    scale = (w * h) / rem
                horiz = w >= h; length = h if horiz else w
        if row:
            place(row, x, y, w, h, horiz, scale)

    lay(items, x, y, w, h)
    return rects


def human_mb(mb):
    if mb >= 1024:
        return f"{mb / 1024:.1f}G"
    return f"{mb:.0f}M"


def parse_rows(csv_text: str) -> list[tuple[str, float]]:
    """InfluxDB CSV (annotated or plain) -> [(container_name, usage_mb)] desc. Pure."""
    out = []
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
        name = row.get("container_name", "").strip()
        val = row.get("_value", "").strip()
        if not name or not val:
            continue
        try:
            out.append((name, float(val) / 1048576.0))   # bytes -> MiB
        except ValueError:
            continue
    out.sort(key=lambda kv: -kv[1])
    return out


def render(rows: list[tuple[str, float]]) -> str:
    """[(container, usage_mb)] -> Discord message string. Pure."""
    items = rows[:len(EMOJI)]
    total_used = sum(v for _, v in rows)
    count = len(rows)
    rects = squarify(items, 0, 0, W, H)
    idxmap = {lab: i for i, (lab, _) in enumerate(items)}
    grid = [[None] * W for _ in range(H)]
    for lab, v, x, y, w, h in rects:
        i = idxmap[lab]
        x0, y0 = int(round(x)), int(round(y))
        x1, y1 = int(round(x + w)), int(round(y + h))
        x0 = max(0, x0); y0 = max(0, y0); x1 = min(W, x1); y1 = min(H, y1)
        if x1 <= x0:
            x1 = min(W, x0 + 1)
        if y1 <= y0:
            y1 = min(H, y0 + 1)
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                if grid[yy][xx] is None:
                    grid[yy][xx] = i
    for yy in range(H):
        for xx in range(W):
            if grid[yy][xx] is None:
                grid[yy][xx] = grid[yy][xx - 1] if xx > 0 else 0
    body = "\n".join("".join(EMOJI[grid[yy][xx]] for xx in range(W)) for yy in range(H))
    legend = "\n".join(f"{EMOJI[i]} {lab} · {human_mb(v)}" for i, (lab, v) in enumerate(items))
    head = f"{count} containers · {human_mb(total_used)} used"
    return f"🐳 **OrbStack containers — memory** — {head}\n\n{body}\n\n{legend}"


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


def fetch_rows():
    token = _from_env("INFLUX_READ_TOKEN")
    if not token:
        raise SystemExit("INFLUX_READ_TOKEN not set")
    req = urllib.request.Request(
        f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}", data=FLUX.encode(), method="POST",
        headers={"Authorization": f"Token {token}", "Accept": "application/csv",
                 "Content-Type": "application/vnd.flux"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return parse_rows(r.read().decode())


def _api(method, path, tok, body=None):
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json",
                 "User-Agent": "discord-orbstack-mem/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _message_id(tok):
    state = STATE_DIR / "state.json"
    if state.exists():
        mid = json.loads(state.read_text()).get("message_id")
        if mid:
            return mid
    msg = _api("POST", f"/channels/{CHANNEL}/messages", tok, {"content": render(fetch_rows())})
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"message_id": msg["id"]}))
    return msg["id"]


def main():
    tok = _from_env("DISCORD_BOT_TOKEN") or sys.exit("DISCORD_BOT_TOKEN not set")
    mid = _message_id(tok)
    sys.stderr.write(f"discord-orbstack-mem: editing message {mid} every {INTERVAL}s\n")
    once = "--once" in sys.argv
    while True:
        try:
            _api("PATCH", f"/channels/{CHANNEL}/messages/{mid}", tok, {"content": render(fetch_rows())})
        except Exception as e:  # noqa: BLE001 — a transient InfluxDB/Discord blip shouldn't kill the loop
            sys.stderr.write(f"update failed: {e}\n")
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
