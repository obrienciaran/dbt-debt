"""Tests for the interactive viewer's pure pieces and the CLI's open-the-viewer gating.

The IO loop and the platform key backends need a real terminal; everything testable without one
(view building, frame windowing, the export pane/write, key maps, and `interactive_supported`,
which is false under pytest's non-TTY stdout) is covered here.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from dbt_debt.cli import _should_view
from dbt_debt.config import Config
from dbt_debt.report.scorecard import ColumnReport, DeadColumn, Scorecard
from dbt_debt.report.viewer import (
    _UNIX_KEYS,
    _WIN_CHARS,
    _WIN_SPECIAL,
    _BANNER,
    _RAINBOW,
    build_views,
    colorize_line,
    export_pane,
    help_pane,
    interactive_supported,
    render_frame,
    write_report,
)


def _card() -> Scorecard:
    dead = tuple(
        DeadColumn("model.p.fct_orders", "fct_orders", f"c{i}", i == 0, "models/fct.sql")
        for i in range(30)
    )
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


def test_summary_tab_opens_with_the_banner() -> None:
    views = build_views(_card(), top_n=10)
    _, summary = views[0]
    assert summary[: len(_BANNER)] == list(_BANNER)
    assert "dbt-debt scorecard — demo" in summary
    # The Detail and JSON tabs stay banner-free.
    assert views[1][1][0].startswith("dbt-debt scorecard")
    assert views[2][1][0] == "{"


def test_colorize_line_targets_banner_headers_and_glyphs() -> None:
    assert colorize_line("Models:") == "\x1b[1mModels:\x1b[0m"
    # Each banner row gets its own rainbow colour, and a truncated row keeps its colour.
    assert colorize_line(_BANNER[0]) == f"{_RAINBOW[0]}{_BANNER[0]}\x1b[0m"
    assert colorize_line(_BANNER[7]) == f"{_RAINBOW[7]}{_BANNER[7]}\x1b[0m"
    assert colorize_line(_BANNER[2][:40]) == f"{_RAINBOW[2]}{_BANNER[2][:40]}\x1b[0m"
    assert colorize_line("  ✓ 1 active") == "  \x1b[32m✓\x1b[0m 1 active"
    assert colorize_line("  ✗ 2 unused") == "  \x1b[31m✗\x1b[0m 2 unused"
    # The review glyphs are all neutral, so all three are yellow.
    assert colorize_line("  ! 1 source stale") == "  \x1b[33m!\x1b[0m 1 source stale"
    assert colorize_line("  ~ 2 rarely used") == "  \x1b[33m~\x1b[0m 2 rarely used"
    assert colorize_line("  ? 1 too new to judge") == "  \x1b[33m?\x1b[0m 1 too new to judge"
    # Indented lines, JSON keys, and Help flags pass through untouched.
    assert colorize_line("  - t1") == "  - t1"
    assert colorize_line('  "unused_models": 1,') == '  "unused_models": 1,'
    assert colorize_line("    --columns  analyse columns") == "    --columns  analyse columns"


def test_colorize_line_traffic_lights_coverage_percentages() -> None:
    green = colorize_line("  - tests: 8 of 10 models have at least one test (80%)")
    assert "\x1b[32m80%\x1b[0m" in green
    yellow = colorize_line("  - model docs: 1 of 3 models have a description (33%)")
    assert "\x1b[33m33%\x1b[0m" in yellow
    red = colorize_line(
        "  - column docs: 1 of 10 columns have a description (10%, catalog columns)"
    )
    assert "\x1b[31m10%\x1b[0m" in red


def test_render_frame_colours_lines_after_truncation() -> None:
    views = [("Summary", ["Models:", "  ✓ 1 active"])]
    frame = render_frame(views, active=0, offset=0, width=80, height=10)
    assert "\x1b[1mModels:\x1b[0m" in frame
    assert "\x1b[32m✓\x1b[0m" in frame


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


def test_export_pane_shows_a_write_error() -> None:
    pane = export_pane('{"x": 1}', written_to=None, error="Permission denied")
    assert any("could not write the report: Permission denied" in line for line in pane)


def test_loop_survives_a_failing_export_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A read-only cwd must not crash the viewer: the error lands in the export pane and the
    # loop keeps running until quit.
    from dbt_debt.report import viewer

    def _refuse(json_text: str, filename: str = viewer.EXPORT_FILE) -> str:
        raise OSError("read-only file system")

    monkeypatch.setattr(viewer, "write_report", _refuse)
    # With one base view the Export tab is [2] (Help now sits last, so digits no longer clamp
    # onto Export).
    keys = iter(["2", "enter", "quit"])
    viewer._loop([("Summary", ["ok"])], "{}", lambda: next(keys))
    assert "could not write the report" in capsys.readouterr().out


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
    # Both backends reach every tab, Help included.
    assert _UNIX_KEYS[b"5"] == "5" and _WIN_CHARS["5"] == "5"


def test_help_pane_lists_flags_and_examples() -> None:
    pane = help_pane()
    text = "\n".join(pane)
    assert "--min-age-days" in text and "--columns" in text and "--no-cache" in text
    assert "dbt-debt scan --format json -o debt.json" in text
    # Fits an ordinary terminal without wrapping.
    assert all(len(line) <= 100 for line in pane)


def test_loop_switches_to_the_help_tab(capsys: pytest.CaptureFixture[str]) -> None:
    from dbt_debt.report import viewer

    keys = iter(["5", "quit"])
    viewer._loop([("Summary", ["ok"])], "{}", lambda: next(keys))
    out = capsys.readouterr().out
    assert "[3] Help" in out and "--lookback-days" in out


def test_interactive_supported_false_without_a_tty() -> None:
    # pytest captures stdout, so it is not a TTY: the viewer must not engage.
    assert interactive_supported() is False


def _args(**kw: object) -> argparse.Namespace:
    defaults = {"print_report": False, "orphans": False, "output": None}
    return argparse.Namespace(**{**defaults, **kw})


def test_should_view_is_false_for_explicit_output_intent() -> None:
    text = Config(output_format="text")
    json_fmt = Config(output_format="json")
    # Any of these means "give me plain output", so the viewer stays closed regardless of TTY.
    assert _should_view(text, _args(output="debt.json")) is False
    assert _should_view(text, _args(print_report=True)) is False
    assert _should_view(text, _args(orphans=True)) is False
    assert _should_view(json_fmt, _args()) is False
