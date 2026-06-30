"""Hermetic tests for discord-orbstack-mem: parse_rows + render. No network."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import orbmem  # noqa: E402

# Annotated InfluxDB CSV (the shape /api/v2/query returns), _value in bytes.
CSV = """\
,result,table,_value,container_name
,_result,0,818089984,influxdb
,_result,0,236015616,grafana
,_result,0,101842944,transit-tracker
,malformed,row,,
,_result,0,14680064,discord-mini-mem
"""


def test_parse_rows_bytes_to_mb_sorted_desc():
    rows = orbmem.parse_rows(CSV)
    names = [n for n, _ in rows]
    assert names == ["influxdb", "grafana", "transit-tracker", "discord-mini-mem"]
    assert abs(dict(rows)["influxdb"] - 818089984 / 1048576.0) < 1e-6
    assert "malformed" not in names and "" not in names   # bad row dropped


def test_parse_rows_tolerates_plain_or_empty():
    assert orbmem.parse_rows("") == []
    assert orbmem.parse_rows("#comment only\n") == []


def test_render_header_grid_legend():
    rows = [("influxdb", 780.0), ("grafana", 225.0), ("transit-tracker", 97.0)]
    out = orbmem.render(rows)
    assert out.startswith("🐳 **OrbStack containers — memory**")
    assert "3 containers" in out
    # total used 780+225+97 = 1102 MB -> 1.1G
    assert "1.1G used" in out
    grid = out.split("\n\n")[1].splitlines()
    assert len(grid) == 8
    legend = out.split("\n\n")[2].splitlines()
    assert legend[0].endswith("influxdb · 780M")
    assert legend[1].endswith("grafana · 225M")


def test_render_caps_at_seven_but_counts_all():
    rows = [(f"c{i}", 100 - i) for i in range(20)]
    out = orbmem.render(rows)
    legend = out.split("\n\n")[2].splitlines()
    assert len(legend) == 7          # only 7 tiles drawn
    assert "20 containers" in out    # but the count reflects all of them
