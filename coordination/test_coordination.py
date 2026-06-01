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
