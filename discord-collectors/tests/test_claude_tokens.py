"""Hermetic tests for the claude_tokens milestone logic (no network/InfluxDB)."""
import claude_tokens as ct


def test_milestone_index():
    assert ct.milestone_index(0) == 0
    assert ct.milestone_index(999_999) == 0
    assert ct.milestone_index(1_000_000) == 1
    assert ct.milestone_index(79_770_977) == 79


def test_first_run_seeds_no_notify():
    d = ct.decide(79_770_977, None)
    assert d["seed"] is True
    assert d["notify"] is False
    assert d["current"] == 79


def test_no_new_milestone():
    d = ct.decide(79_900_000, 79)
    assert d["notify"] is False
    assert d["crossed"] == 0


def test_single_crossing():
    d = ct.decide(80_010_000, 79)
    assert d["notify"] is True
    assert d["crossed"] == 1
    assert d["current"] == 80


def test_multiple_crossings_since_last():
    d = ct.decide(82_300_000, 79)
    assert d["notify"] is True
    assert d["crossed"] == 3
    assert d["current"] == 82


def test_human():
    assert ct._human(80_000_000) == "80M"
    assert ct._human(1_500_000) == "1.5M"


def test_build_embed_shape():
    p = ct.build_embed(80_010_000, 80, 1)
    e = p["embeds"][0]
    assert "80M" in e["title"]
    assert e["color"] == ct.GOLD
    assert any(f["value"] == "80,010,000" for f in e["fields"])
