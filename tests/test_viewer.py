"""Tests for the interactive viewer's pure pieces and the CLI's open-the-viewer gating.

The IO loop and the platform key backends need a real terminal; everything testable without one
(view building, frame windowing, the export pane/write, key maps, and `interactive_supported`,
which is false under pytest's non-TTY stdout) is covered here.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dbt_debt.cli import _should_view
from dbt_debt.config import Config
from dbt_debt.report.scorecard import ColumnReport, DeadColumn, Scorecard
from dbt_debt.report.viewer import (
    _UNIX_KEYS,
    _WIN_CHARS,
    _WIN_SPECIAL,
    build_views,
    export_pane,
    interactive_supported,
    render_frame,
    write_report,
)


def _card() -> Scorecard:
    dead = tuple(DeadColumn("fct_orders", f"c{i}", i == 0, "models/fct.sql") for i in range(30))
    return Scorecard(
        project_name="demo",
        lookback_days=180,
        active_models=1,
        unused_models=0,
        columns=ColumnReport(
            active=5, unused=len(dead), removable=len(dead) - 1, dead_columns=dead
        ),
    )


def test_build_views_has_three_tabs() -> None:
    views = build_views(_card(), top_n=10)
    assert [label for label, _ in views] == ["Summary", "Detail", "JSON"]
    assert all(lines for _, lines in views)


def test_render_frame_windows_to_terminal_height() -> None:
    views = [("Summary", [f"line {i}" for i in range(100)])]
    # height 10 -> body of 6 rows; from offset 20 we see lines 20..25.
    frame = render_frame(views, active=0, offset=20, width=80, height=10)
    assert "line 20" in frame and "line 25" in frame
    assert "line 26" not in frame and "line 19" not in frame
    assert "[1] Summary" in frame
    assert "rows 21-26 of 100" in frame  # 1-based position readout


def test_render_frame_clamps_overscroll() -> None:
    views = [("Summary", [f"line {i}" for i in range(8)])]
    # 8 lines, a 6-row body (height 10 - 4): the furthest you can scroll still ends at the last row.
    frame = render_frame(views, active=0, offset=999, width=40, height=10)
    assert "rows 3-8 of 8" in frame
    assert "line 7" in frame and "line 1" not in frame


def test_export_pane_prompts_then_confirms() -> None:
    before = export_pane('{"x": 1}', written_to=None)
    assert any("Press  w  or  Enter" in line for line in before)
    assert not any("wrote report" in line for line in before)
    after = export_pane('{"x": 1}', written_to="/tmp/debt.json")
    assert any("wrote report to /tmp/debt.json" in line for line in after)


def test_write_report_writes_file_with_newline(tmp_path: Path) -> None:
    target = tmp_path / "debt.json"
    resolved = write_report('{"unused": 2}', str(target))
    assert target.read_text() == '{"unused": 2}\n'
    assert resolved == str(target.resolve())


def test_key_maps_cover_navigation() -> None:
    assert _UNIX_KEYS[b"\x1b[A"] == "up" and _UNIX_KEYS[b"\x1b[B"] == "down"
    assert _UNIX_KEYS[b"\t"] == "tab" and _UNIX_KEYS[b"q"] == "quit"
    assert _WIN_SPECIAL["H"] == "up" and _WIN_SPECIAL["P"] == "down"
    assert _WIN_CHARS["w"] == "write" and _WIN_CHARS["q"] == "quit"


def test_interactive_supported_false_without_a_tty() -> None:
    # pytest captures stdout, so it is not a TTY: the viewer must not engage.
    assert interactive_supported() is False


def _args(**kw: object) -> argparse.Namespace:
    defaults = {"no_interactive": False, "detail": False, "orphans": False, "output": None}
    return argparse.Namespace(**{**defaults, **kw})


def test_should_view_is_false_for_explicit_output_intent() -> None:
    text = Config(output_format="text")
    json_fmt = Config(output_format="json")
    # Any of these means "give me plain output", so the viewer stays closed regardless of TTY.
    assert _should_view(text, _args(output="debt.json")) is False
    assert _should_view(text, _args(detail=True)) is False
    assert _should_view(text, _args(no_interactive=True)) is False
    assert _should_view(text, _args(orphans=True)) is False
    assert _should_view(json_fmt, _args()) is False
