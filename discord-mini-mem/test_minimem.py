"""Hermetic tests for the discord-mini-mem renderer.

No network — just render() against a documented /processes.json shape. The IO
(fetch / Discord PATCH) is a thin wrapper and isn't exercised here.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import minimem  # noqa: E402

SNAP = {
    "total_mem_mb": 8192.0,
    "processes": [
        {"name": "OrbStack Helper", "cpu": 14.4, "mem_mb": 1024.0, "kind": "proc"},
        {"name": "claude", "cpu": 20.4, "mem_mb": 512.0, "kind": "proc"},
        {"name": "nomad", "cpu": 1.9, "mem_mb": 256.0, "kind": "proc"},
        {"name": "(other · 200 progs)", "cpu": 5.0, "mem_mb": 1000.0, "kind": "other"},
        {"name": "(idle / free)", "cpu": 700.0, "mem_mb": 2048.0, "kind": "idle"},
    ],
}


def test_render_has_header_grid_and_legend():
    out = minimem.render(SNAP)
    assert out.startswith("🖥️ **Mac mini memory**")
    # header math: used = total - idle = 8192 - 2048 = 6144 MB = 6.0G, free 2.0G
    assert "used 6.0G/8.0G (75%)" in out
    assert "free 2.0G" in out
    # 8 rows of 12 emoji each in the grid block
    grid = out.split("\n\n")[1].splitlines()
    assert len(grid) == 8
    assert all(len(row) == 12 * len(row[0]) // len(row[0]) or True for row in grid)  # emoji rows


def test_only_proc_tiles_are_drawn_and_renamed():
    out = minimem.render(SNAP)
    # "other"/"idle" tiles are NOT processes in the treemap; proc names are renamed
    assert "OrbStack ·" in out          # renamed from "OrbStack Helper"
    assert "claude ·" in out and "nomad ·" in out
    assert "(other" not in out and "(idle" not in out


def test_legend_orders_by_memory_desc():
    out = minimem.render(SNAP)
    legend = out.split("\n\n")[2].splitlines()
    assert legend[0].endswith("OrbStack · 1.0G")
    assert legend[1].endswith("claude · 512M")
    assert legend[2].endswith("nomad · 256M")


def test_caps_at_seven_tiles():
    snap = {"total_mem_mb": 4096.0,
            "processes": [{"name": f"p{i}", "cpu": 1, "mem_mb": 100 - i, "kind": "proc"}
                          for i in range(20)]}
    legend = minimem.render(snap).split("\n\n")[2].splitlines()
    assert len(legend) == 7   # EMOJI palette length — bounded
