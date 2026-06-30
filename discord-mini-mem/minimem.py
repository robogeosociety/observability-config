#!/usr/bin/env python3
"""discord-mini-mem — live Mac-mini memory treemap as a self-editing #ops message.

Pulls the SAME per-process snapshot that feeds the Grafana mac-system treemap
(dev-status collector at :8077/processes.json, reached from the container via
host.docker.internal), renders it as colored-square emoji (mobile-safe — Discord
ANSI color is desktop-only), and PATCHes one Discord message every INTERVAL seconds.

Pure `render()` is hermetically tested; the rest is thin IO. Env:
  PROCESSES_URL   default http://host.docker.internal:8077/processes.json
  DISCORD_CHANNEL_ID, DISCORD_BOT_TOKEN   (token may instead come from STATE/.env)
  STATE_DIR       default /state — holds state.json {message_id} so a restart
                  re-uses the same message instead of posting a new one
  INTERVAL        seconds between edits (default 60)
"""
import json, os, sys, time, urllib.request, urllib.error, pathlib

W, H = 12, 8
EMOJI = ["🟧", "🟦", "🟩", "🟪", "🟥", "🟨", "🟫"]   # 7 distinct, render in color on every client
NAMES = {"OrbStack Helper": "OrbStack", "Google Chrome Helper": "Chrome",
         "Obsidian Helper (Renderer)": "Obsidian", "com.apple.WebKit.WebContent": "WebKit"}

PROCESSES_URL = os.environ.get("PROCESSES_URL", "http://host.docker.internal:8077/processes.json")
CHANNEL = os.environ.get("DISCORD_CHANNEL_ID", "1520850187105341710")
STATE_DIR = pathlib.Path(os.environ.get("STATE_DIR", "/state"))
INTERVAL = int(os.environ.get("INTERVAL", "60"))


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


def render(data: dict) -> str:
    """processes.json dict -> the Discord message string. Pure."""
    procs = [(p["name"], float(p["mem_mb"])) for p in data.get("processes", [])
             if p.get("kind") == "proc"]
    procs.sort(key=lambda kv: -kv[1])
    items = [(NAMES.get(n, n), v) for n, v in procs[:len(EMOJI)]]
    total = float(data.get("total_mem_mb", 0)) or 1.0
    idle = sum(float(p["mem_mb"]) for p in data.get("processes", []) if p.get("kind") == "idle")
    used = max(0.0, total - idle)

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
    pct = 100 * used / total
    head = f"used {human_mb(used)}/{human_mb(total)} ({pct:.0f}%) · free {human_mb(idle)}"
    return f"🖥️ **Mac mini memory** — {head}\n\n{body}\n\n{legend}"


# --- IO ---------------------------------------------------------------------

def _token():
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if tok:
        return tok
    envf = STATE_DIR / ".env"
    if envf.exists():
        for ln in envf.read_text().splitlines():
            if ln.strip().startswith("DISCORD_BOT_TOKEN="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("DISCORD_BOT_TOKEN not set (env or STATE_DIR/.env)")


def fetch():
    with urllib.request.urlopen(PROCESSES_URL, timeout=15) as r:
        return json.loads(r.read())


def _api(method, path, tok, body=None):
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json",
                 "User-Agent": "discord-mini-mem/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _message_id(tok):
    state = STATE_DIR / "state.json"
    if state.exists():
        mid = json.loads(state.read_text()).get("message_id")
        if mid:
            return mid
    msg = _api("POST", f"/channels/{CHANNEL}/messages", tok, {"content": render(fetch())})
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"message_id": msg["id"]}))
    return msg["id"]


def main():
    tok = _token()
    mid = _message_id(tok)
    sys.stderr.write(f"discord-mini-mem: editing message {mid} every {INTERVAL}s from {PROCESSES_URL}\n")
    once = "--once" in sys.argv
    while True:
        try:
            _api("PATCH", f"/channels/{CHANNEL}/messages/{mid}", tok, {"content": render(fetch())})
        except Exception as e:  # noqa: BLE001 — a transient :8077/Discord blip shouldn't kill the loop
            sys.stderr.write(f"update failed: {e}\n")
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
