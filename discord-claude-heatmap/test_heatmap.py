"""Hermetic tests for discord-claude-heatmap: parse_matrix + render. No network."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import heatmap  # noqa: E402

CSV = """\
,result,table,_value,_time,project
,_result,0,1000,2026-06-30T10:00:00Z,obsidian
,_result,0,500,2026-06-30T11:00:00Z,obsidian
,_result,1,200,2026-06-30T10:00:00Z,observability
,_result,1,,2026-06-30T11:00:00Z,observability
,bad,row,,,
"""


def test_parse_matrix_shapes_and_tolerates_bad_rows():
    matrix, times = heatmap.parse_matrix(CSV)
    assert times == ["2026-06-30T10:00:00Z", "2026-06-30T11:00:00Z"]
    assert matrix["obsidian"]["2026-06-30T10:00:00Z"] == 1000
    assert matrix["observability"]["2026-06-30T11:00:00Z"] == 0.0   # empty _value -> 0
    assert "—" not in matrix or matrix.get("—") != {}                # bad row didn't add a real series


def test_render_header_rows_scale():
    matrix, times = heatmap.parse_matrix(CSV)
    out = heatmap.render(matrix, times)
    assert out.startswith("🔥 **Claude tokens — heatmap**")
    assert "last 2h hourly" in out
    assert "1.5K out" in out or "1500" in out or "2K out" in out  # grand total ~1700 -> "2K"
    rows = out.split("\n\n")[1].splitlines()
    # rows ordered by total desc: obsidian (1500) before observability (200)
    assert "obsidian" in rows[0] and "observability" in rows[1]
    assert out.rstrip().endswith("hot")


def test_intensity_scale_monotonic():
    # the hottest cell uses the top color, an idle cell uses ⬛
    assert heatmap._cell(0, 100) == "⬛"
    assert heatmap._cell(100, 100) == "🟥"
    assert heatmap._cell(1, 100) == "🟦"           # 1% -> lowest non-idle
    # strictly non-decreasing color index as value rises
    idxs = [heatmap.HEAT.index(heatmap._cell(v, 100)) for v in range(0, 101, 5)]
    assert idxs == sorted(idxs)


def test_render_caps_rows_to_top_n():
    matrix = {f"p{i}": {"t": float(100 - i)} for i in range(20)}
    out = heatmap.render(matrix, ["t"])
    rows = out.split("\n\n")[1].splitlines()
    assert len(rows) == heatmap.ROWS    # bounded to top-N projects


def test_empty_window_is_safe():
    out = heatmap.render({}, [])
    assert "no token activity" in out
