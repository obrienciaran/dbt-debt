"""Tests for the progress spinner's pure pieces and its non-TTY no-op behaviour.

The thread loop runs only behind a real-TTY guard; everything testable without a terminal — the
frame string, the line-clear string, and the fact that `status` starts no thread when disabled —
is covered here.
"""

from __future__ import annotations

import threading

from dbt_debt.report.spinner import _FRAMES, _clear, _frame, status


def test_frame_cycles_through_the_ascii_frames() -> None:
    assert _frame("Working", 0) == f"\r{_FRAMES[0]} Working"
    assert _frame("Working", 1) == f"\r{_FRAMES[1]} Working"
    # Wraps around past the end of the frame string.
    assert _frame("Working", len(_FRAMES)) == f"\r{_FRAMES[0]} Working"


def test_clear_blanks_the_whole_line() -> None:
    cleared = _clear("Working")
    assert cleared.startswith("\r")
    assert cleared.endswith("\r")
    # Long enough to cover the frame char, the space, and the message.
    assert cleared.count(" ") >= len("Working") + 2


def test_disabled_status_starts_no_thread() -> None:
    before = threading.active_count()
    with status("Working", enabled=False):
        assert threading.active_count() == before


def test_enabled_status_runs_and_cleans_up() -> None:
    before = threading.active_count()
    with status("Working", enabled=True):
        # The spinner thread is alive inside the block.
        assert threading.active_count() == before + 1
    # And joined on exit, so nothing leaks.
    assert threading.active_count() == before
