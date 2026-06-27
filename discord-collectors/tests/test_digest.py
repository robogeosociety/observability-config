"""Hermetic tests for digest pure logic (dev-status parsing, embed formatting)."""
import digest


# A trimmed-down copy of the real dev-status :8077 payload.
DEV_STATUS = {
    "total": 3, "up": 1, "down": 2,
    "summary": [{"up": 1, "down": 2, "total": 3}],
    "deployments": [
        {"name": "grafana", "up": 1, "port": 3001},
        {"name": "api", "up": 0, "port": 8001},
        {"name": "classify", "up": 0, "port": 5173},
    ],
}


def test_parse_deployments_reads_up_field():
    parsed = digest.parse_deployments(DEV_STATUS)
    assert parsed == {"grafana": True, "api": False, "classify": False}


def test_parse_deployments_dedupes_up_wins():
    data = {"deployments": [
        {"name": "x", "up": 0}, {"name": "x", "up": 1},
    ]}
    assert digest.parse_deployments(data) == {"x": True}


def test_parse_deployments_handles_none_and_garbage():
    assert digest.parse_deployments(None) == {}
    assert digest.parse_deployments(42) == {}  # the old crash: int has no .get
    assert digest.parse_deployments({"deployments": [42, {"no": "name"}]}) == {}


def test_format_deployments_does_not_crash_on_summary_int():
    # Regression: the whole payload (with the int/list `summary`) must not crash.
    out = digest.format_deployments(DEV_STATUS)
    assert "1/3" in out
    assert "api" in out and "classify" in out
    assert "grafana" not in out  # up services aren't listed individually


def test_format_deployments_unreachable():
    assert "unreachable" in digest.format_deployments(None)


def test_format_deployments_all_up():
    data = {"deployments": [{"name": "a", "up": 1}, {"name": "b", "up": 1}]}
    out = digest.format_deployments(data)
    assert "2/2" in out
    assert "\U0001F534" not in out  # no down list


def test_buckets_are_real():
    # The old hardcoded telegraf/weather buckets don't exist.
    assert "telegraf" not in digest.BUCKETS
    assert "weather" not in digest.BUCKETS
    for b in ("home_assistant", "ops", "system"):
        assert b in digest.BUCKETS
