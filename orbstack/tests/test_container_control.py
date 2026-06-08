"""Static guards for the orbstack container-control Nomad job (hermetic — no Nomad
or Docker needed). These encode the safety invariants the job relies on: it can't
act without explicit dispatch meta, the action is allowlisted, the target must
exist, and the HCL points at the real on-disk script."""

from pathlib import Path

import pytest

ORBSTACK = Path(__file__).resolve().parent.parent
HCL = ORBSTACK / "nomad" / "container-control.hcl"
TASK = ORBSTACK / "nomad" / "container-control.sh"
WRAPPER = ORBSTACK / "control.sh"

# Canonical live path the Nomad agent reads at dispatch time (NOT the worktree
# path) — the agent runs against the merged /Volumes checkout.
LIVE_SCRIPT = "/Volumes/dev/observability/orbstack/nomad/container-control.sh"


@pytest.fixture(scope="module")
def hcl() -> str:
    return HCL.read_text()


@pytest.fixture(scope="module")
def task() -> str:
    return TASK.read_text()


def test_files_exist():
    for p in (HCL, TASK, WRAPPER):
        assert p.is_file(), f"missing {p}"


def test_job_is_parameterized_batch(hcl):
    assert 'type        = "batch"' in hcl or 'type = "batch"' in hcl
    assert "parameterized {" in hcl, "must be a parameterized job (no bare run can act)"


def test_both_meta_required(hcl):
    # Without these, dispatch could omit a field and interpolate to empty.
    assert "meta_required" in hcl
    assert '"action"' in hcl and '"container"' in hcl


def test_raw_exec_via_zsh(hcl):
    assert 'driver = "raw_exec"' in hcl
    assert '"/bin/zsh"' in hcl


def test_hcl_points_at_real_script(hcl):
    assert LIVE_SCRIPT in hcl, "HCL must invoke the canonical live script path"
    assert TASK.exists(), "the referenced script must exist in the repo"


def test_meta_interpolated_into_args(hcl):
    assert "${NOMAD_META_action}" in hcl
    assert "${NOMAD_META_container}" in hcl


def test_no_retry_or_reschedule(hcl):
    # A one-shot op should die visibly, not loop against a flapping container.
    assert "attempts = 0" in hcl


def test_script_allowlists_action(task):
    assert "start|stop|restart)" in task, "action must be allowlisted"


def test_script_checks_container_exists(task):
    assert "docker inspect" in task, "must verify the container exists before acting"


def test_script_is_strict(task):
    assert "set -euo pipefail" in task


def test_script_execs_only_allowed_verb(task):
    # The exec'd docker verb comes from the validated $action, not free text.
    assert 'exec docker "$action" "$container"' in task


def test_wrapper_dispatches_the_job():
    body = WRAPPER.read_text()
    assert "nomad job dispatch" in body
    assert "container-control" in body
