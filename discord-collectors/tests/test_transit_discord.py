"""Hermetic tests for transit_discord pure logic (no OBA, no Discord)."""
import transit_discord as td


def test_color_for_severity_camelcase_mapping():
    assert td.color_for_severity("severe") == td.COLOR_RED
    assert td.color_for_severity("verySevere") == td.COLOR_RED
    assert td.color_for_severity("moderate") == td.COLOR_ORANGE
    # everything else -> yellow (including the values the old DETOUR/NO_SERVICE map missed)
    for s in ("noImpact", "unknown", "verySlight", "slight", "", None):
        assert td.color_for_severity(s) == td.COLOR_YELLOW


def test_affected_route_names_from_allaffects():
    sit = {"allAffects": [
        {"routeId": "40_2LINE"},
        {"routeId": "40_100479"},
        {"routeId": "99_unwatched"},
    ]}
    names = td.affected_route_names(sit)
    assert names == ["2 Line", "1 Line"]  # order preserved, unwatched dropped


def test_build_alert_embed_reads_value_wrapped_fields():
    sit = {
        "id": "40_18610",
        "severity": "severe",
        "reason": "CONSTRUCTION",
        "summary": {"lang": "en", "value": "Reduced service"},
        "description": {"lang": "en", "value": "Details here"},
        "url": {"lang": "en", "value": "https://example.com"},
        "allAffects": [{"routeId": "40_100479"}],
        "activeWindows": [{"from": 1781534160000, "to": 1783396800000}],
    }
    embed = td.build_alert_embed(sit)
    assert embed["color"] == td.COLOR_RED
    assert embed["url"] == "https://example.com"
    field_names = {f["name"]: f["value"] for f in embed["fields"]}
    assert field_names["Summary"] == "Reduced service"
    assert field_names["Details"] == "Details here"
    assert field_names["Affected"] == "1 Line"
    assert field_names["Severity"] == "severe"
    assert "Active" in field_names


def test_collect_current_dedupes_one_situation_across_routes():
    # The same situation id is returned by two different watched routes; it must
    # appear exactly once in the collected dict.
    shared = {"id": "S1", "severity": "moderate", "allAffects": [{"routeId": "40_100479"}, {"routeId": "40_2LINE"}]}

    class FakeClient:
        pass

    calls = []

    def fake_fetch(client, route_id, key):
        calls.append(route_id)
        # both 1-line routes return the same situation; others empty
        if route_id in ("40_100479", "40_2LINE"):
            return [shared]
        return []

    orig = td.fetch_situations_for_route
    td.fetch_situations_for_route = fake_fetch
    try:
        current = td.collect_current_situations(FakeClient(), "KEY", spacing=0)
    finally:
        td.fetch_situations_for_route = orig

    assert set(current) == {"S1"}
    assert len(current) == 1
    # swept every watched route
    assert set(calls) == set(td.WATCHED_ROUTES)


def test_build_cleared_embed():
    embed = td.build_cleared_embed({"route_label": "1 Line", "summary": "was bad"})
    assert embed["color"] == td.CLEARED_COLOR
    assert "1 Line" in embed["title"]
