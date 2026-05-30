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
    assert "set -eu" in text, "should fail fast on errors/unset vars"
    assert "influx backup" in text, "should actually run an influx backup"
    # dump is streamed out via a zsh redirect (not docker cp) so a single /bin/zsh
    # Full Disk Access grant covers every /Volumes access
    assert 'tar' in text and '> "$HOST_TARBALL"' in text, "should stream the dump out via redirect"
    # retention pruning must be present so backups don't grow unbounded
    assert "RETENTION_DAYS" in text and "-mtime" in text, "should prune old backups"
    # must read secrets from the env file, never hardcode a token
    assert "INFLUX_ADMIN_TOKEN" in text and "$INFLUX_ADMIN_TOKEN" in text
