"""Hermetic tests for the anomaly detector's pure functions (no InfluxDB)."""
import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import detector


def test_zscores_basic():
    z = detector.z_scores([1, 2, 3, 4, 5])
    assert len(z) == 5
    assert abs(sum(z)) < 1e-9          # z-scores sum to ~0
    assert z[-1] > 0 and z[0] < 0


def test_zscores_degenerate():
    assert detector.z_scores([5, 5, 5]) == []   # zero variance
    assert detector.z_scores([1]) == []         # too few


def test_score_latest_flags_spike():
    vals = [10.0] * 40 + [10.0, 100.0]          # last point is a big spike
    ev = detector.score_latest(vals, z_warn=3.0, z_crit=4.0, min_points=30)
    assert ev is not None
    assert ev["direction"] == "high"
    assert ev["zscore"] > 3.0
    assert ev["severity"] == "crit"             # far out → crit


def test_score_latest_quiet_returns_none():
    vals = [10.0 + (i % 3) for i in range(60)]  # normal wiggle, last point unremarkable
    assert detector.score_latest(vals, 3.0, 4.0, 30) is None


def test_score_latest_needs_min_points():
    vals = [10.0] * 5 + [100.0]
    assert detector.score_latest(vals, 3.0, 4.0, 30) is None   # below min_points


def test_event_line_is_valid_line_protocol():
    ev = {"zscore": 3.4, "value": 82.1, "mean": 40.2, "stddev": 12.3,
          "severity": "warn", "direction": "high", "n": 360}
    line = detector.event_line("mac_cpu_usage", "system", ev, 1780000000000000000)
    assert line.startswith("anomaly,")
    assert "metric=mac_cpu_usage" in line and "bucket=system" in line
    assert "severity=warn" in line and "direction=high" in line
    assert "zscore=3.4" in line and "n=360i" in line
    assert line.endswith(" 1780000000000000000")
    # no spaces in the tag set (would corrupt line protocol)
    assert " " not in line.split(" ")[0]


def test_config_is_valid_and_well_formed():
    cfg = yaml.safe_load((pathlib.Path(__file__).parent / "anomaly-detection.yaml").read_text())
    assert cfg["defaults"]["z_warn"] <= cfg["defaults"]["z_crit"]
    names = [m["name"] for m in cfg["metrics"]]
    assert names and len(names) == len(set(names)), "metric names must be unique"
    for m in cfg["metrics"]:
        assert " " not in m["name"], f"{m['name']}: name can't contain spaces (line-protocol tag)"
        assert m.get("bucket"), f"{m['name']}: missing bucket"
        assert "__WINDOW__" in m["flux"], f"{m['name']}: flux must use __WINDOW__"
