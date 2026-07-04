"""Hermetic tests for the coordinator's queue + mutex + worker plumbing.

No git, no docker, no live stack: every test runs against a tmp COORD_HOME with
COORD_DEPLOY=0 (the worker skips the git/rsync/docker/curl side effects and just
exercises the lock + queue lifecycle). Mirrors the dev-status/collector approach.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
ZSH = shutil.which("zsh") or "/bin/zsh"

pytestmark = pytest.mark.skipif(
    not (shutil.which("zsh") or Path("/bin/zsh").exists()),
    reason="zsh not available — coordination scripts are zsh",
)


def _run(script, *args, coord_home, deploy="0", extra_env=None, check=True):
    env = dict(os.environ, COORD_HOME=str(coord_home), COORD_DEPLOY=deploy)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [ZSH, str(HERE / script), *args],
        capture_output=True, text=True, env=env, check=check,
    )


def test_enqueue_is_atomic_and_fifo_named(tmp_path):
    _run("enqueue.sh", "deploy", "first reason", coord_home=tmp_path)
    jobs = list((tmp_path / "queue").glob("*.job"))
    assert len(jobs) == 1
    # no half-written temp left behind
    assert not list((tmp_path / "queue").glob(".tmp.*"))
    body = jobs[0].read_text()
    assert "type: deploy" in body
    assert "reason: first reason" in body
    # filename is timestamp-prefixed for FIFO draining
    assert jobs[0].name.split("-")[0].isdigit()


def test_worker_drains_queue_to_done(tmp_path):
    _run("enqueue.sh", "deploy", "x", coord_home=tmp_path)
    _run("enqueue.sh", "deploy", "y", coord_home=tmp_path)
    res = _run("worker.sh", coord_home=tmp_path)
    assert "draining 2 job(s)" in res.stdout
    assert not list((tmp_path / "queue").glob("*.job"))      # queue emptied
    assert len(list((tmp_path / "done").glob("*.job"))) == 2  # both succeeded (deploy skipped)
    assert not (tmp_path / "LOCK").exists()                   # lock released


def test_worker_empty_queue_is_noop(tmp_path):
    res = _run("worker.sh", coord_home=tmp_path)
    assert "queue empty" in res.stdout
    assert not (tmp_path / "LOCK").exists()


def test_worker_exits_when_lock_held_by_live_owner(tmp_path):
    # Hold the lock with a real, living process so the worker can't reclaim it.
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        lock = tmp_path / "LOCK"
        lock.mkdir(parents=True)
        (lock / "owner").write_text(f"{sleeper.pid} {int(time.time())}\n")
        _run("enqueue.sh", "deploy", "blocked", coord_home=tmp_path)
        res = _run("worker.sh", coord_home=tmp_path)
        assert "lock held" in res.stdout
        # job stays queued, nothing processed
        assert len(list((tmp_path / "queue").glob("*.job"))) == 1
        assert not list((tmp_path / "done").glob("*.job"))
    finally:
        sleeper.terminate()
        sleeper.wait()


def test_mutex_reclaims_stale_lock(tmp_path):
    # A dead owner PID with an old timestamp is reclaimable; the worker should run.
    lock = tmp_path / "LOCK"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"999999 {int(time.time()) - 100000}\n")  # dead pid, very old
    _run("enqueue.sh", "deploy", "stale", coord_home=tmp_path)
    res = _run("worker.sh", coord_home=tmp_path, extra_env={"COORD_LOCK_TTL": "900"})
    assert "draining 1 job(s)" in res.stdout
    assert len(list((tmp_path / "done").glob("*.job"))) == 1


def test_fresh_lock_not_reclaimed_even_if_pid_dead(tmp_path):
    # Dead PID but NOT old → must be left alone (TTL guards against premature reclaim).
    lock = tmp_path / "LOCK"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"999999 {int(time.time())}\n")  # dead pid, fresh
    _run("enqueue.sh", "deploy", "fresh", coord_home=tmp_path)
    res = _run("worker.sh", coord_home=tmp_path, extra_env={"COORD_LOCK_TTL": "900"})
    assert "lock held" in res.stdout
    assert len(list((tmp_path / "queue").glob("*.job"))) == 1


def test_mutex_reclaims_orphaned_lock_with_no_owner(tmp_path):
    # LOCK dir with NO owner file — a run died between `mkdir LOCK` and writing
    # `owner`. Once the dir is older than the orphan grace it MUST be reclaimable;
    # otherwise an owner-less lock wedges deploys forever (2026-06-29 incident:
    # ~12h of missed deploys, including a retired-alert PR that kept paging).
    lock = tmp_path / "LOCK"
    lock.mkdir(parents=True)                       # no owner file written
    old = time.time() - 100000
    os.utime(lock, (old, old))                     # age the dir past the grace
    _run("enqueue.sh", "deploy", "orphan", coord_home=tmp_path)
    res = _run("worker.sh", coord_home=tmp_path, extra_env={"COORD_LOCK_ORPHAN_GRACE": "30"})
    assert "draining 1 job(s)" in res.stdout
    assert len(list((tmp_path / "done").glob("*.job"))) == 1


def test_fresh_orphaned_lock_not_reclaimed(tmp_path):
    # Owner-less but freshly created → a live run may be mid-acquire (sub-second
    # window before it writes owner). Must NOT yank it until past the grace.
    lock = tmp_path / "LOCK"
    lock.mkdir(parents=True)                       # no owner file, mtime = now
    _run("enqueue.sh", "deploy", "fresh-orphan", coord_home=tmp_path)
    res = _run("worker.sh", coord_home=tmp_path, extra_env={"COORD_LOCK_ORPHAN_GRACE": "3600"})
    assert "lock held" in res.stdout
    assert len(list((tmp_path / "queue").glob("*.job"))) == 1


# ── CI gate (ci_gate.sh) ────────────────────────────────────────────────────────
# Hermetic: the CI status source is stubbed via COORD_CI_CHECK_CMD (a command taking
# <sha> and echoing a raw "<status>:<conclusion>"), so no gh / network is touched. The
# trailing `#` comments out the <sha> ci_gate.sh appends, letting the stub emit a fixed
# status. Verifies the map: only an affirmative green deploys; a broken source degrades
# OPEN (deploy) so the gate can never freeze the coordinator.

def _gate(sha, *, check_cmd, require_ci="1"):
    env = {"COORD_CI_CHECK_CMD": check_cmd, "COORD_REQUIRE_CI": require_ci}
    return subprocess.run(
        [ZSH, str(HERE / "ci_gate.sh"), sha],
        capture_output=True, text=True, env=dict(os.environ, **env), check=True,
    ).stdout.strip()


def test_gate_deploys_on_green():
    assert _gate("abc123", check_cmd="print -- completed:success #") == "deploy"


def test_gate_withholds_on_ci_failure():
    assert _gate("abc123", check_cmd="print -- completed:failure #") == "skip:ci-failed"
    # any non-success completed conclusion is a hold
    assert _gate("abc123", check_cmd="print -- completed:cancelled #") == "skip:ci-failed"


def test_gate_defers_while_pending():
    assert _gate("abc123", check_cmd="print -- in_progress:null #") == "skip:ci-pending"
    assert _gate("abc123", check_cmd="print -- queued:null #") == "skip:ci-pending"
    # no run reported yet for this SHA → defer, don't deploy blind
    assert _gate("abc123", check_cmd="print -- none #") == "skip:ci-pending"


def test_gate_degrades_open_when_source_errors():
    # A failing status source (gh error / auth) must NOT freeze deploys — degrade open.
    assert _gate("abc123", check_cmd="false #") == "deploy"


def test_gate_bypass_when_disabled():
    # Break-glass: COORD_REQUIRE_CI=0 green-lights regardless of CI (manual deploy).
    assert _gate("abc123", check_cmd="print -- completed:failure #", require_ci="0") == "deploy"
