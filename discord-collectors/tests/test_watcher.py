"""Hermetic tests for watcher pure logic (dev-status parsing, state machine)."""
import watcher


DEV_STATUS = {
    "total": 3, "up": 1, "down": 2,
    "summary": [{"up": 1, "down": 2, "total": 3}],
    "deployments": [
        {"name": "grafana", "up": 1, "port": 3001},
        {"name": "api", "up": 0, "port": 8001},
        {"name": "classify", "up": 0, "port": 5173},
    ],
}


def test_parse_services_reads_deployments_up_field():
    assert watcher.parse_services(DEV_STATUS) == {
        "grafana": "UP", "api": "DOWN", "classify": "DOWN",
    }


def test_parse_services_no_crash_on_int_summary():
    # The old code iterated the top-level dict and called .get() on the int.
    assert watcher.parse_services(42) == {}
    assert watcher.parse_services(None) == {}


def test_parse_services_dedupes_up_wins():
    data = {"deployments": [{"name": "x", "up": 0}, {"name": "x", "up": 1}]}
    assert watcher.parse_services(data) == {"x": "UP"}


def test_state_machine_debounces_down_transition():
    st = watcher.WatcherState()
    # seed
    st.process({"svc": "UP"}, "wh", dry_run=True)
    assert st.initialised
    # first DOWN poll: debounce, no flip yet
    st.process({"svc": "DOWN"}, "wh", dry_run=True)
    assert st.services["svc"] == "UP"
    assert st.down_counter.get("svc") == 1
    # second DOWN poll: flips to DOWN
    st.process({"svc": "DOWN"}, "wh", dry_run=True)
    assert st.services["svc"] == "DOWN"


def test_state_machine_server_unreachable_then_recover():
    st = watcher.WatcherState()
    st.process(None, "wh", dry_run=True)
    assert st.server_unreachable_alerted
    st.process({"svc": "UP"}, "wh", dry_run=True)
    assert not st.server_unreachable_alerted
