"""Hermetic test for the thread keepalive's pure message builder (no network)."""
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import thread_keepalive as tk  # noqa: E402


def test_heartbeat_text_is_stable_and_dated():
    now = datetime.datetime(2026, 6, 30, 6, 23, tzinfo=datetime.timezone.utc)
    out = tk.heartbeat_text(now)
    assert out.startswith("🟢 dashboards live · keepalive 2026-06-30 06:23")
    assert "thread stays open" in out
