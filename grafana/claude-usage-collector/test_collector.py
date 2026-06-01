"""Hermetic unit tests for the Claude Code usage collector's pure functions.

No filesystem, no InfluxDB — just parse_line / build_line against the documented
transcript shape. Mirrors the campsites/backup shape-test approach.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import collector  # noqa: E402


ASSISTANT = {
    "type": "assistant",
    "timestamp": "2026-05-30T13:39:07.597Z",
    "cwd": "/Volumes/dev/RealityCapture",
    "sessionId": "6e6b1979-4ac9-4dea-8f90-e0c6569a320d",
    "message": {
        "model": "claude-opus-4-8",
        "usage": {
            "input_tokens": 4346,
            "output_tokens": 126,
            "cache_creation_input_tokens": 5945,
            "cache_read_input_tokens": 16197,
            "service_tier": "standard",
            "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
        },
    },
}


def test_parse_assistant_usage():
    rec = collector.parse_line(ASSISTANT)
    assert rec is not None
    assert rec["model"] == "claude-opus-4-8"
    assert rec["project"] == "RealityCapture"          # basename of cwd
    assert rec["session"] == "6e6b1979-4ac9-4dea-8f90-e0c6569a320d"
    assert rec["service_tier"] == "standard"
    assert rec["input_tokens"] == 4346
    assert rec["cache_read_tokens"] == 16197
    assert rec["total_tokens"] == 4346 + 126 + 5945 + 16197
    assert rec["web_search_requests"] == 2
    assert rec["web_fetch_requests"] == 1


def test_timestamp_is_epoch_ms():
    rec = collector.parse_line(ASSISTANT)
    # 2026-05-30T13:39:07.597Z -> ms, ends in 597, 13 digits
    assert rec["ts_ms"] % 1000 == 597
    assert len(str(rec["ts_ms"])) == 13


def test_skips_non_assistant_and_synthetic():
    assert collector.parse_line({"type": "user", "message": {}}) is None
    synthetic = {**ASSISTANT, "message": {**ASSISTANT["message"], "model": "<synthetic>"}}
    assert collector.parse_line(synthetic) is None


def test_skips_missing_or_zero_usage():
    no_usage = {"type": "assistant", "timestamp": "2026-05-30T13:39:07.597Z",
                "message": {"model": "claude-opus-4-8"}}
    assert collector.parse_line(no_usage) is None
    zero = {**ASSISTANT, "message": {**ASSISTANT["message"], "usage": {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}
    assert collector.parse_line(zero) is None


def test_missing_cwd_falls_back_to_unknown():
    rec = collector.parse_line({**ASSISTANT, "cwd": ""})
    assert rec["project"] == "unknown"


def test_build_line_shape():
    line = collector.build_line(collector.parse_line(ASSISTANT))
    assert line.startswith("tokens,")
    head, fields, ts = line.rsplit(" ", 2)
    assert "source=claude-cli" in head
    assert "model=claude-opus-4-8" in head
    assert "project=RealityCapture" in head
    assert "session=6e6b1979-4ac9-4dea-8f90-e0c6569a320d" in head
    assert "total_tokens=26614i" in fields
    assert "messages=1i" in fields
    assert ts.isdigit() and len(ts) == 13      # epoch ms


def test_tag_values_are_escaped():
    # a project path with spaces must not break the tag set
    rec = collector.parse_line({**ASSISTANT, "cwd": "/Users/x/My Project"})
    line = collector.build_line(rec)
    assert "project=My\\ Project" in line
