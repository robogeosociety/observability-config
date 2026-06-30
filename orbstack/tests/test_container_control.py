"""Static guards for the orbstack container-control service jobs (hermetic — no
Nomad or Docker). They encode the design that makes the Nomad UI a real container
console: one service job per container, a supervisor that mirrors + controls the
container lifecycle, and HCL that points at the real script."""

from pathlib import Path

import pytest

NOMAD = Path(__file__).resolve().parent.parent / "nomad"
SUPERVISE = NOMAD / "supervise.sh"
DEPLOY = NOMAD / "deploy-jobs.sh"
CTL = NOMAD / "ctl.sh"
JOB_FILES = sorted(NOMAD.glob("ctl-*.hcl"))

# Canonical live path the Nomad agent reads at run time (NOT a worktree path).
LIVE_SCRIPT = "/Volumes/dev/observability/orbstack/nomad/supervise.sh"

# Containers that must each have a supervisor job.
EXPECTED = {
    "grafana",
    "influxdb",
    "transit-tracker",
    "grafana-renderer",
    "realitycapture-viewer",
    "discord-mini-mem",
    "discord-orbstack-mem",
    "discord-claude-heatmap",
}


def test_core_files_exist():
    for p in (SUPERVISE, DEPLOY, CTL):
        assert p.is_file(), f"missing {p}"
    assert JOB_FILES, "no ctl-*.hcl service jobs found"


def test_old_parameterized_job_is_gone():
    # The bad approach was replaced, not left lying around.
    assert not (NOMAD / "container-control.hcl").exists()
    assert not (NOMAD / "container-control.sh").exists()


def test_expected_containers_have_jobs():
    have = {p.stem.removeprefix("ctl-") for p in JOB_FILES}
    assert EXPECTED <= have, f"missing job(s) for {EXPECTED - have}"


@pytest.mark.parametrize("hcl", JOB_FILES, ids=lambda p: p.name)
def test_job_is_a_service_supervisor(hcl):
    body = hcl.read_text()
    container = hcl.stem.removeprefix("ctl-")
    assert 'type        = "service"' in body or 'type = "service"' in body, (
        "must be a service job so the UI shows live Running/Dead state"
    )
    assert f'job "ctl-{container}"' in body, "job name must match the file/container"
    # Grouped under the orbstack namespace (the "parent" grouping).
    assert 'namespace   = "orbstack"' in body or 'namespace = "orbstack"' in body
    assert 'driver       = "raw_exec"' in body or 'driver = "raw_exec"' in body
    # Reflect state, don't fight a redeploy.
    assert "attempts = 0" in body
    # SIGTERM is what the supervisor traps to docker-stop the container.
    assert "SIGTERM" in body
    # Points at the canonical script (via the supervise var default) + names the container.
    assert LIVE_SCRIPT in body
    assert f'"{container}"' in body


def test_supervisor_lifecycle():
    body = SUPERVISE.read_text()
    assert "docker start" in body, "must start the container"
    assert "docker wait" in body, "must block while the container runs"
    assert "docker stop" in body, "must stop the container on signal"
    assert "trap " in body and "TERM" in body, "must trap SIGTERM to stop cleanly"
    assert "docker inspect" in body, "must verify the container exists"


def test_deploy_and_ctl_drive_nomad_jobs():
    deploy = DEPLOY.read_text()
    assert "nomad job run" in deploy and "nomad job stop" in deploy
    # `up` must create the namespace before registering into it.
    assert "nomad namespace apply" in deploy
    ctl = CTL.read_text()
    assert "nomad job run" in ctl
    assert "nomad job stop" in ctl
    assert "nomad job restart" in ctl


def test_scripts_scope_to_orbstack_namespace():
    for p in (DEPLOY, CTL):
        assert 'NOMAD_NAMESPACE="orbstack"' in p.read_text(), f"{p.name} must scope to the namespace"
