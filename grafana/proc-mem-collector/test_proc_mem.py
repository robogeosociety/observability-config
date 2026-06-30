"""Hermetic unit tests for the proc-mem collector's pure functions.

No subprocess, no InfluxDB — just parse_ps / parse_vmstat / summary / build_lines
against documented `ps`/`vm_stat` shapes. Mirrors the claude-usage-collector approach.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import proc_mem as pm  # noqa: E402

PS = """\
  1024 /System/Library/Frameworks/Foo.app/launchd
524288 /Applications/OrbStack.app/Contents/MacOS/OrbStack Helper
262144 /opt/homebrew/bin/nomad
131072 OrbStack Helper
 65536 claude
   bad line should be skipped
"""

VMSTAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                          1000.
Pages active:                      200000.
Pages speculative:                    500.
Pages wired down:                   50000.
Pages occupied by compressor:      120000.
"""


def test_parse_ps_aggregates_by_basename_and_sorts():
    procs = pm.parse_ps(PS)
    d = dict(procs)
    # two OrbStack Helper lines (524288 + 131072) fold under the RENAME -> "OrbStack"
    assert d["OrbStack"] == 524288 + 131072
    assert d["nomad"] == 262144
    assert d["claude"] == 65536
    assert d["launchd"] == 1024
    # sorted descending by rss
    assert [n for n, _ in procs] == sorted(d, key=lambda k: -d[k])
    # the malformed line was skipped
    assert "bad" not in d and "line" not in d


def test_parse_vmstat_and_summary():
    vm = pm.parse_vmstat(VMSTAT)
    assert vm["Pages free"] == 1000
    total = 8 * 1024**3
    s = pm.summary(vm, total)
    free = (1000 + 500) * pm.PG
    assert s["free_bytes"] == free
    assert s["total_bytes"] == total
    assert s["used_bytes"] == total - free
    assert s["wired_bytes"] == 50000 * pm.PG
    assert s["comp_bytes"] == 120000 * pm.PG


def test_build_lines_format_and_units():
    procs = [("claude", 100)]  # 100 KiB
    summ = {"used_bytes": 5, "total_bytes": 9, "wired_bytes": 1, "comp_bytes": 2, "free_bytes": 4}
    lines = pm.build_lines(procs, summ, top_n=15, ts_ns=1234)
    assert lines[0] == "proc_mem,name=claude rss_bytes=102400i 1234"   # KiB -> bytes
    assert lines[-1].startswith("mem_summary ")
    assert "used_bytes=5i" in lines[-1] and "total_bytes=9i" in lines[-1]
    assert lines[-1].endswith(" 1234")


def test_build_lines_cardinality_is_bounded_to_top_n():
    procs = [(f"p{i}", 1000 - i) for i in range(50)]
    lines = pm.build_lines(procs, {"used_bytes": 1}, top_n=15)
    proc_lines = [l for l in lines if l.startswith("proc_mem,")]
    assert len(proc_lines) == 15                      # not 50 — series stay bounded
    assert lines[-1].startswith("mem_summary ")       # exactly one summary series
    assert proc_lines[0].startswith("proc_mem,name=p0 ")


def test_zero_rss_processes_dropped():
    lines = pm.build_lines([("a", 0), ("b", 10)], {"used_bytes": 1}, ts_ns=None)
    proc_lines = [l for l in lines if l.startswith("proc_mem,")]
    assert proc_lines == ["proc_mem,name=b rss_bytes=10240i"]   # no timestamp suffix when ts_ns=None


def test_tag_escaping_for_spaces_and_commas():
    lines = pm.build_lines([("Some App, v2", 1)], {"x": 1})
    assert lines[0].startswith("proc_mem,name=Some\\ App\\,\\ v2 rss_bytes=")
