"""Validate grafana/changelog.jsonl — the append-only log of dashboard updates.

Every dashboard change appends one entry. These checks keep the log well-formed
and chronological so it stays a trustworthy history.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

CHANGELOG = Path(__file__).parent.parent / "changelog.jsonl"
VALID_ACTIONS = {"added", "modified", "removed", "renamed", "moved"}


def _entries():
    lines = [ln for ln in CHANGELOG.read_text().splitlines() if ln.strip()]
    return [(i, json.loads(ln)) for i, ln in enumerate(lines, 1)]


def test_changelog_exists_and_nonempty():
    assert CHANGELOG.exists(), "grafana/changelog.jsonl is missing"
    assert _entries(), "changelog has no entries"


def test_every_line_is_valid_json_with_required_fields():
    for i, e in _entries():
        assert isinstance(e.get("ts"), str), f"line {i}: missing ts"
        datetime.fromisoformat(e["ts"])  # raises if unparseable
        assert isinstance(e.get("summary"), str) and e["summary"], f"line {i}: missing summary"
        assert isinstance(e.get("changes"), list) and e["changes"], f"line {i}: empty changes"
        for c in e["changes"]:
            assert isinstance(c.get("dashboard"), str) and c["dashboard"], f"line {i}: change missing dashboard"
            assert c.get("action") in VALID_ACTIONS, \
                f"line {i}: bad action {c.get('action')!r} (allowed: {sorted(VALID_ACTIONS)})"


def test_entries_are_chronological():
    ts = [datetime.fromisoformat(e["ts"]) for _i, e in _entries()]
    assert ts == sorted(ts), "changelog entries must be append-only (oldest first)"
