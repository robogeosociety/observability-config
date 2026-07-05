"""Hermetic unit tests for the CI/CD collector's pure functions.

No network, no InfluxDB — run_to_point / inventory_points / window_start / lp
against the documented GitHub API shapes. Mirrors the claude-usage-collector
shape-test approach.
"""
import importlib.util
import pathlib
from datetime import datetime, timezone

# Load under a unique module name: claude-usage-collector's tests also `import
# collector`, and pytest shares one sys.modules across the whole run.
_spec = importlib.util.spec_from_file_location(
    "cicd_collector", pathlib.Path(__file__).parent / "collector.py")
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)


RUN = {
    "id": 16123456789,
    "name": "tests",
    "workflow_id": 42,
    "status": "completed",
    "conclusion": "success",
    "event": "push",
    "head_branch": "main",
    "run_attempt": 1,
    "created_at": "2026-07-01T12:00:00Z",
    "run_started_at": "2026-07-01T12:00:20Z",
    "updated_at": "2026-07-01T12:03:20Z",
    "repository": {"name": "observability-config"},
}


def test_run_to_point_success():
    line = collector.run_to_point(RUN, "robogeosociety")
    assert line.startswith("workflow_run,")
    assert "org=robogeosociety" in line
    assert "repo=observability-config" in line
    assert "workflow=tests" in line
    assert "branch=main" in line and "event=push" in line
    assert "conclusion=success" in line
    assert "ok=1i" in line
    assert "duration_s=180i" in line          # 12:00:20 -> 12:03:20
    assert "queue_s=20i" in line              # created -> started
    assert "run_id=16123456789i" in line
    assert line.endswith(str(collector.iso_to_epoch("2026-07-01T12:03:20Z")))


def test_run_to_point_prefers_inventory_workflow_name():
    # `run-name:` overrides repaint run["name"] per run; the file-level name
    # from the inventory keeps the tag stable.
    run = {**RUN, "name": "Deploy #123 by tommy"}
    line = collector.run_to_point(run, "rgs", workflow_names={42: "deploy"})
    assert "workflow=deploy" in line


def test_run_to_point_failure_and_escaping():
    run = {**RUN, "conclusion": "failure", "name": "build and test"}
    line = collector.run_to_point(run, "rgs")
    assert "ok=0i" in line and "conclusion=failure" in line
    assert "workflow=build\\ and\\ test" in line


def test_run_to_point_skips_incomplete():
    assert collector.run_to_point({**RUN, "status": "in_progress", "conclusion": None}, "o") is None
    assert collector.run_to_point({**RUN, "status": "queued", "conclusion": None}, "o") is None


def test_run_to_point_clamps_negative_intervals():
    # updated_at can lag run_started_at on odd API states — never negative fields
    run = {**RUN, "updated_at": "2026-07-01T12:00:00Z",
           "run_started_at": "2026-07-01T12:00:20Z", "created_at": "2026-07-01T12:00:30Z"}
    line = collector.run_to_point(run, "o")
    assert "duration_s=0i" in line and "queue_s=0i" in line


def test_run_to_point_omits_missing_branch_tag():
    line = collector.run_to_point({**RUN, "head_branch": None}, "o")
    assert "branch=" not in line


def test_inventory_points():
    wfs = [{"id": 42, "name": "tests", "state": "active", "path": ".github/workflows/test.yml"},
           {"id": 43, "name": "deploy", "state": "disabled_manually", "path": ".github/workflows/deploy.yml"}]
    lines = collector.inventory_points(wfs, "rgs", "tommybot", 1751000000)
    assert len(lines) == 2
    assert "workflow_inventory," in lines[0]
    assert "state=active" in lines[0] and "present=1i" in lines[0]
    assert "path=.github/workflows/test.yml" in lines[0]
    assert "state=disabled_manually" in lines[1]
    assert lines[0].endswith("1751000000")


def test_window_start_first_run_backfills():
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    start = collector.window_start({}, now, backfill_days=30, overlap_min=30)
    assert (now - start).days == 30


def test_window_start_overlaps_last_poll():
    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    state = {"last_poll": "2026-07-05T11:55:00+00:00"}
    start = collector.window_start(state, now, backfill_days=30, overlap_min=30)
    assert start == datetime(2026, 7, 5, 11, 25, tzinfo=timezone.utc)


def test_window_start_never_exceeds_backfill_floor():
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    state = {"last_poll": "2020-01-01T00:00:00+00:00"}  # stale cursor
    start = collector.window_start(state, now, backfill_days=30, overlap_min=30)
    assert (now - start).days == 30


def test_lp_int_float_and_none_fields():
    line = collector.lp("m", {"t": "a b"}, {"i": 3, "f": 1.5, "skip": None}, 99)
    assert line == "m,t=a\\ b i=3i,f=1.5 99"


class FakeGH:
    """Paginate against canned pages, no network."""
    def __init__(self, pages):
        self.pages, self.calls, self.rate_remaining = pages, 0, 4999

    def get(self, path, params=None):
        self.calls += 1
        return self.pages[params["page"] - 1]

    paginate = collector.GitHub.paginate


def test_paginate_stops_on_short_page():
    full_page = {"workflows": [{"id": i} for i in range(collector.PER_PAGE)]}
    short_page = {"workflows": [{"id": "last"}]}
    gh = FakeGH([full_page, short_page, {"workflows": []}])
    items = list(gh.paginate("/x", key="workflows"))
    assert len(items) == collector.PER_PAGE + 1
    assert gh.calls == 2  # never fetched page 3
