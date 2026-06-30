#!/usr/bin/env python3
"""thread_keepalive.py — keep the #ops "Live Dashboards" thread from auto-archiving.

Discord threads auto-archive after N days of no *new* messages; the dashboard
containers only EDIT their messages (edits don't reset the timer), so the thread
would collapse. This posts one tiny heartbeat per run (a new message resets the
timer) and deletes the previous heartbeat, so exactly one keepalive line exists at
a time. Run daily by Nomad — well under the 7-day archive window.

  THREAD_ID         the thread (channel) to keep alive
  DISCORD_BOT_TOKEN bot token (run wrapper sources it); must own the thread messages
  KEEPALIVE_STATE   state file holding the previous heartbeat id (default below)

  python3 thread_keepalive.py --dry-run   # print the message, post/delete nothing
"""
import json, os, sys, urllib.request, urllib.error, pathlib, datetime

THREAD_ID = os.environ.get("THREAD_ID", "1521534944827150366")
STATE = pathlib.Path(os.environ.get(
    "KEEPALIVE_STATE", os.path.expanduser("~/.local/share/thread-keepalive/state.json")))


def heartbeat_text(now: datetime.datetime) -> str:
    """The keepalive message. Pure (testable) — small + useful, not noise."""
    return f"🟢 dashboards live · keepalive {now:%Y-%m-%d %H:%M %Z} — this thread stays open"


def _token() -> str:
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if not tok:
        raise SystemExit("DISCORD_BOT_TOKEN not set")
    return tok


def _api(method, path, tok, body=None):
    req = urllib.request.Request(
        "https://discord.com/api/v10" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json",
                 "User-Agent": "thread-keepalive/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, (json.loads(r.read()) if r.status not in (204,) else {})


def main():
    now = datetime.datetime.now().astimezone()
    text = heartbeat_text(now)
    if "--dry-run" in sys.argv:
        print(text)
        return
    tok = _token()
    # 1) post a fresh heartbeat (resets the auto-archive timer)
    _, msg = _api("POST", f"/channels/{THREAD_ID}/messages", tok, {"content": text})
    new_id = msg["id"]
    # 2) delete the previous heartbeat so only one ever lingers
    prev = None
    if STATE.exists():
        prev = json.loads(STATE.read_text()).get("message_id")
    if prev and prev != new_id:
        try:
            _api("DELETE", f"/channels/{THREAD_ID}/messages/{prev}", tok)
        except urllib.error.HTTPError as e:
            sys.stderr.write(f"prev delete failed ({e.code}) — leaving it\n")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"message_id": new_id}))
    sys.stderr.write(f"keepalive posted {new_id}, deleted {prev}\n")


if __name__ == "__main__":
    main()
