"""Hermetic tests for github_discord pure logic (no network, no gh)."""
import github_discord as gh


def test_api_path_strips_host():
    assert gh.api_path("https://api.github.com/repos/o/r/pulls/7") == "repos/o/r/pulls/7"
    assert gh.api_path("http://api.github.com/repos/o/r/issues/3") == "repos/o/r/issues/3"
    # already a path
    assert gh.api_path("/repos/o/r/pulls/7") == "repos/o/r/pulls/7"


def test_build_pr_embed_uses_full_pr_title_not_untitled():
    full = {"title": "Real title", "user": {"login": "tommyroar"},
            "html_url": "https://github.com/o/r/pull/7", "number": 7, "merged": False}
    embed = gh.build_pr_embed("opened", "o/r", full)
    assert "Real title" in embed["title"]
    assert "Untitled" not in embed["title"]
    assert embed["color"] == gh.COLOR_PR_OPEN
    assert "tommyroar" in embed["description"]
    assert embed["url"] == "https://github.com/o/r/pull/7"


def test_build_pr_embed_merge_is_purple():
    full = {"title": "T", "user": {"login": "u"}, "html_url": "x", "number": 1, "merged": True}
    assert gh.build_pr_embed("merged", "o/r", full)["color"] == gh.COLOR_MERGE


def test_build_pr_embed_missing_title_falls_back():
    embed = gh.build_pr_embed("opened", "o/r", {})
    assert "Untitled PR" in embed["title"]


def _event(action, api_url="https://api.github.com/repos/o/r/pulls/7"):
    return {"type": "PullRequestEvent", "repo": {"name": "o/r"},
            "payload": {"action": action, "pull_request": {"url": api_url}}}


def test_event_to_embed_opened_fetches_full_pr():
    captured = {}

    def fetcher(path):
        captured["path"] = path
        return {"title": "Fetched", "user": {"login": "u"}, "html_url": "h", "number": 7, "merged": False}

    embed = gh.event_to_embed(_event("opened"), fetcher)
    # It fetched the trimmed PR's REST path, not the event payload.
    assert captured["path"] == "repos/o/r/pulls/7"
    assert "Fetched" in embed["title"]


def test_event_to_embed_merged_close_is_deduped():
    # action=closed with merged==true is dropped (the synthetic 'merged' covers it).
    fetcher = lambda path: {"title": "T", "user": {"login": "u"}, "html_url": "h", "number": 7, "merged": True}
    assert gh.event_to_embed(_event("closed"), fetcher) is None


def test_event_to_embed_closed_unmerged_posts_grey():
    fetcher = lambda path: {"title": "T", "user": {"login": "u"}, "html_url": "h", "number": 7, "merged": False}
    embed = gh.event_to_embed(_event("closed"), fetcher)
    assert embed["color"] == gh.COLOR_PR_CLOSED


def test_event_to_embed_ignores_other_actions():
    assert gh.event_to_embed(_event("synchronize"), lambda p: {}) is None


def test_event_to_embed_issue_opened():
    ev = {"type": "IssuesEvent", "repo": {"name": "o/r"},
          "payload": {"action": "opened",
                      "issue": {"title": "Bug", "user": {"login": "u"},
                                "html_url": "h", "number": 3}}}
    embed = gh.event_to_embed(ev, lambda p: None)
    assert "Bug" in embed["title"]
    assert embed["color"] == gh.COLOR_ISSUE_OPEN


def test_event_to_embed_push_event_ignored():
    assert gh.event_to_embed({"type": "PushEvent", "payload": {}}, lambda p: {}) is None
