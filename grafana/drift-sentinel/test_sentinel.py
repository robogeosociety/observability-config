"""Hermetic tests for the sentinel's pure decision logic (no InfluxDB/gh/Discord)."""

import sentinel


def test_parse_cadence():
    assert sentinel.parse_cadence("15s") == 15
    assert sentinel.parse_cadence("5m") == 300
    assert sentinel.parse_cadence("24h") == 86400
    assert sentinel.parse_cadence("1d") == 86400
    assert sentinel.parse_cadence(None) is None
    assert sentinel.parse_cadence("weird") is None


def test_classify_stale():
    st, d = sentinel.classify(recent=0, recent_window_s=900, base=1000, base_window_s=86400)
    assert st == "stale" and d["recent"] == 0


def test_classify_ok_when_rate_matches_baseline():
    # 90 pts/15m ≈ baseline of 8640/24h (same rate) -> ok
    st, _ = sentinel.classify(recent=90, recent_window_s=900, base=8640, base_window_s=86400)
    assert st == "ok"


def test_classify_degraded_when_rate_drops():
    # recent rate is ~10% of baseline -> degraded
    st, d = sentinel.classify(recent=9, recent_window_s=900, base=8640, base_window_s=86400)
    assert st == "degraded" and d["ratio"] < 0.4


def test_classify_no_ratio_for_noncontinuous_bucket():
    # daily/bursty bucket: a low rate ratio must NOT flag degraded (staleness only)
    st, d = sentinel.classify(recent=2, recent_window_s=259200, base=8640,
                              base_window_s=86400, check_ratio=False)
    assert st == "ok" and "ratio" not in d


def test_classify_still_stale_without_ratio():
    st, _ = sentinel.classify(recent=0, recent_window_s=259200, base=5,
                              base_window_s=86400, check_ratio=False)
    assert st == "stale"


def test_tracked_sources_skips_monitor_skip():
    index = {"u": {"source": {"status": "active", "monitor": "skip",
                              "produces": {"bucket": "claude_code"}}}}
    assert list(sentinel.tracked_sources(index)) == []


def test_classify_ok_when_no_baseline():
    # brand-new bucket with no 24h history shouldn't false-degrade
    st, _ = sentinel.classify(recent=5, recent_window_s=900, base=0, base_window_s=86400)
    assert st == "ok"


def test_build_embed_includes_commits_and_repo():
    commits = [{"sha": "abcdef1234", "msg": "break ingest", "author": "Tommy"}]
    payload = sentinel.build_embed(
        "mac-system", "Mac System", "system", "stale", {"recent": 0},
        "robogeosociety/observability-config", commits)
    embed = payload["embeds"][0]
    assert "stale" in embed["title"]
    text = str(embed)
    assert "abcdef1" in text and "robogeosociety/observability-config" in text


def test_build_embed_degraded_shows_ratio():
    payload = sentinel.build_embed(
        "x", "X", "b", "degraded", {"ratio": 0.25}, None, [])
    assert "degraded" in payload["embeds"][0]["title"]
    assert any("25%" in f["value"] for f in payload["embeds"][0]["fields"])


def test_tracked_sources_filters_to_active_with_bucket():
    index = {
        "a": {"source": {"status": "active", "repo": "o/r",
                         "produces": {"bucket": "system"}}},
        "b": {"source": {"status": "external", "produces": {"bucket": "x"}}},   # external -> skip
        "c": {"source": {"status": "active", "produces": {"dataset": "ae"}}},   # no bucket -> skip
        "d": {},                                                                # no source -> skip
    }
    got = [uid for uid, _e, _s in sentinel.tracked_sources(index)]
    assert got == ["a"]
