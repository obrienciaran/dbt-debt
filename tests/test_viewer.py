"""Tests for the interactive viewer's pure pieces and the CLI's open-the-viewer gating.

The IO loop and the platform key backends need a real terminal; everything testable without one
(view building, frame windowing, the export pane/write, key maps, and `interactive_supported`,
which is false under pytest's non-TTY stdout) is covered here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def test_should_view_consults_the_terminal_when_intent_is_plain() -> None:
    # With no competing output flag the decision falls to the terminal itself, which under
    # pytest is not a TTY.
    assert _should_view(Config(output_format="text"), _args()) is False


class _TTY:
    """A stand-in stream that claims to be a terminal."""

    def __init__(self, fd: int = 0) -> None:
        self._fd = fd

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd


def test_interactive_supported_true_on_a_unix_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    from dbt_debt.report import viewer

    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr(sys, "stdout", _TTY())
    # On a POSIX platform the answer is whether the termios backend imported.
    assert viewer.interactive_supported() is viewer._HAS_TERMIOS


def test_interactive_supported_true_on_a_windows_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    from dbt_debt.report import viewer

    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr(sys, "stdout", _TTY())
    monkeypatch.setattr(sys, "platform", "win32")
    assert viewer.interactive_supported() is True


class _FakeKernel32:
    def __init__(self, mode: int = 7, readable: bool = True, settable: bool = True) -> None:
        self.mode = mode
        self.readable = readable
        self.settable = settable
        self.set_modes: list[int] = []

    def GetStdHandle(self, code: int) -> str:
        return "handle"

    def GetConsoleMode(self, handle: str, ref: object) -> int:
        if not self.readable:
            return 0
        ref.value = self.mode  # type: ignore[attr-defined]
        return 1

    def SetConsoleMode(self, handle: str, mode: int) -> int:
        self.set_modes.append(mode)
        return 1 if self.settable else 0


def _install_ctypes(monkeypatch: pytest.MonkeyPatch, kernel32: _FakeKernel32) -> None:
    stub = ModuleType("ctypes")

    class _CUint:
        def __init__(self) -> None:
            self.value = 0

    stub.c_uint32 = _CUint  # type: ignore[attr-defined]
    stub.byref = lambda obj: obj  # type: ignore[attr-defined]
    stub.windll = SimpleNamespace(kernel32=kernel32)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ctypes", stub)


def test_windows_vt_is_a_no_op_off_windows() -> None:
    from dbt_debt.report import viewer

    assert viewer._enable_windows_vt() is False
    viewer._restore_windows_vt()  # nothing to restore, and must not raise


def test_windows_vt_enable_and_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    from dbt_debt.report import viewer

    monkeypatch.setattr(sys, "platform", "win32")
    kernel32 = _FakeKernel32(mode=7)
    _install_ctypes(monkeypatch, kernel32)
    monkeypatch.setattr(viewer, "_win_prev_mode", None)
    assert viewer._enable_windows_vt() is True
    assert kernel32.set_modes[-1] == 7 | 0x0004
    assert viewer._win_prev_mode == 7
    viewer._restore_windows_vt()
    assert kernel32.set_modes[-1] == 7


def test_windows_vt_fails_when_the_console_mode_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dbt_debt.report import viewer

    monkeypatch.setattr(sys, "platform", "win32")
    _install_ctypes(monkeypatch, _FakeKernel32(readable=False))
    assert viewer._enable_windows_vt() is False


def test_unix_reader_sets_and_restores_the_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dbt_debt.report import viewer

    saved = ["saved-attrs"]
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        viewer,
        "termios",
        SimpleNamespace(
            tcgetattr=lambda fd: saved,
            tcsetattr=lambda fd, when, attrs: calls.append(("restore", attrs)),
            TCSADRAIN="drain",
        ),
    )
    monkeypatch.setattr(
        viewer, "tty", SimpleNamespace(setcbreak=lambda fd: calls.append(("cbreak", fd)))
    )
    monkeypatch.setattr(sys, "stdin", _TTY(fd=5))
    with viewer._UnixReader():
        pass
    out = capsys.readouterr().out
    assert viewer._ALT_ON in out and viewer._ALT_OFF in out
    assert ("cbreak", 5) in calls and ("restore", saved) in calls


def test_unix_reader_assembles_escape_sequences(monkeypatch: pytest.MonkeyPatch) -> None:
    from dbt_debt.report import viewer

    reader = viewer._UnixReader.__new__(viewer._UnixReader)
    reader._fd = 0
    stream = [b"\x1b", b"[", b"A"]
    monkeypatch.setattr(viewer, "os", SimpleNamespace(read=lambda fd, n: stream.pop(0)))
    monkeypatch.setattr(
        viewer,
        "select",
        SimpleNamespace(select=lambda r, w, x, t: (([0] if stream else []), [], [])),
    )
    assert reader.read_key() == "up"
    stream[:] = [b"q"]
    assert reader.read_key() == "quit"
    stream[:] = [b"z"]
    assert reader.read_key() is None


def test_windows_reader_refuses_without_vt(monkeypatch: pytest.MonkeyPatch) -> None:
    # Off Windows (or when the console refuses VT) enabling fails, so entering the reader must
    # raise the setup error the caller turns into a plain-output fallback.
    from dbt_debt.report import viewer

    with pytest.raises(viewer._SetupError):
        with viewer._WindowsReader():
            pass


def test_windows_reader_enters_and_exits_with_vt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dbt_debt.report import viewer

    restored: list[bool] = []
    monkeypatch.setattr(viewer, "_enable_windows_vt", lambda: True)
    monkeypatch.setattr(viewer, "_restore_windows_vt", lambda: restored.append(True))
    with viewer._WindowsReader():
        pass
    assert restored == [True]
    out = capsys.readouterr().out
    assert viewer._ALT_ON in out and viewer._ALT_OFF in out


def test_windows_reader_read_key_maps_special_and_plain_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dbt_debt.report import viewer

    reader = viewer._WindowsReader()
    assert reader.read_key() is None  # off Windows there is nothing to read

    monkeypatch.setattr(sys, "platform", "win32")
    keys = iter(["\xe0", "H", "q"])
    msvcrt = ModuleType("msvcrt")
    msvcrt.getwch = lambda: next(keys)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "msvcrt", msvcrt)
    assert reader.read_key() == "up"
    assert reader.read_key() == "quit"


class _FakeReader:
    """A reader double for `_terminal`: enters, hands out one key, exits."""

    entered: list[str] = []

    def __enter__(self) -> _FakeReader:
        _FakeReader.entered.append("enter")
        return self

    def __exit__(self, *exc: object) -> None:
        _FakeReader.entered.append("exit")

    def read_key(self) -> str | None:
        return "quit"


def test_terminal_uses_the_platform_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    from dbt_debt.report import viewer

    _FakeReader.entered = []
    monkeypatch.setattr(viewer, "_UnixReader", _FakeReader)
    with viewer._terminal() as read_key:
        assert read_key() == "quit"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(viewer, "_WindowsReader", _FakeReader)
    with viewer._terminal() as read_key:
        assert read_key() == "quit"
    assert _FakeReader.entered == ["enter", "exit", "enter", "exit"]


class _TerminalStub:
    """Replaces `_terminal` in run_viewer tests: yields a canned key feed or fails setup."""

    def __init__(self, keys: list[str] | None = None, error: Exception | None = None) -> None:
        self._keys = iter(keys or [])
        self._error = error

    def __enter__(self) -> object:
        if self._error:
            raise self._error
        return lambda: next(self._keys)

    def __exit__(self, *exc: object) -> bool:
        return False


def test_run_viewer_runs_the_loop_and_reports_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dbt_debt.report import viewer

    monkeypatch.setattr(viewer, "_terminal", lambda: _TerminalStub(keys=["quit"]))
    assert viewer.run_viewer(_card(), Config()) is True
    assert "[1] Summary" in capsys.readouterr().out


def test_run_viewer_falls_back_when_the_terminal_cannot_be_set_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dbt_debt.report import viewer

    monkeypatch.setattr(viewer, "_terminal", lambda: _TerminalStub(error=viewer._SetupError()))
    assert viewer.run_viewer(_card(), Config()) is False


def test_run_viewer_treats_ctrl_c_as_quit(monkeypatch: pytest.MonkeyPatch) -> None:
    from dbt_debt.report import viewer

    monkeypatch.setattr(viewer, "_terminal", lambda: _TerminalStub(error=KeyboardInterrupt()))
    assert viewer.run_viewer(_card(), Config()) is True


def test_loop_switches_tabs_and_scrolls(capsys: pytest.CaptureFixture[str]) -> None:
    from dbt_debt.report import viewer

    lines = [f"line {i}" for i in range(50)]
    keys = iter(
        ["down", "down", "up", "pgdn", "pgup", "home", "end", None, "tab", "backtab", "quit"]
    )
    viewer._loop([("Summary", lines)], "{}", lambda: next(keys))
    out = capsys.readouterr().out
    # "end" lands the window on the last row, and tab/backtab cycle through Export and back.
    assert "line 49" in out
    assert "Export the full report" in out
