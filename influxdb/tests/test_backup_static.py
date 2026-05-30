"""Static validation of the InfluxDB backup script (no running stack needed)."""

import pathlib
import shutil
import subprocess

import pytest

INFLUX_DIR = pathlib.Path(__file__).parent.parent
BACKUP = INFLUX_DIR / "backup.sh"


def test_backup_script_present_and_executable():
    assert BACKUP.exists(), "backup.sh missing"
    assert BACKUP.stat().st_mode & 0o111, "backup.sh should be executable"


def test_backup_script_syntax():
    """Parse-check with zsh (the script's shebang); fall back to bash; skip if neither."""
    shell = shutil.which("zsh") or shutil.which("bash")
    if not shell:
        pytest.skip("no zsh/bash available to syntax-check")
    r = subprocess.run([shell, "-n", str(BACKUP)], capture_output=True, text=True)
    assert r.returncode == 0, f"syntax error in backup.sh:\n{r.stderr}"


def test_backup_script_has_safety_and_core_steps():
    text = BACKUP.read_text()
    assert "set -u" in text and "pipefail" in text, "should fail on unset vars + pipe errors"
    assert "influx backup" in text, "should actually run an influx backup"
    # dump is streamed out via a zsh redirect (not docker cp) so a single /bin/zsh
    # Full Disk Access grant covers every /Volumes access
    assert 'tar' in text and '> "$HOST_TARBALL"' in text, "should stream the dump out via redirect"
    # emits a heartbeat to the ops bucket for the Backups dashboard
    assert "/api/v2/write" in text and "ops" in text, "should emit a backup heartbeat to influx"
    assert "success=1i" in text and "success=0i" in text, "should record success and failure"
    # offsite R2 upload is wired but optional (activated by R2_* in .env)
    assert "R2_ACCOUNT_ID" in text and "r2_upload.py" in text, "should support optional R2 upload"
    # retention pruning must be present so backups don't grow unbounded
    assert "RETENTION_DAYS" in text and "-mtime" in text, "should prune old backups"
    # must read secrets from the env file, never hardcode a token
    assert "INFLUX_ADMIN_TOKEN" in text and "$INFLUX_ADMIN_TOKEN" in text
