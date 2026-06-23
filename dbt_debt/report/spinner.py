"""A minimal progress spinner for the slow warehouse phases of a scan.

Stdlib-only and degradable, the same way the viewer is: it animates on **stderr** only when
stderr is a real terminal, so piped stdout, JSON, and `-o` output are never touched. A daemon
thread redraws a single line with a carriage return (`\\r`) and plain ASCII frames — no ANSI
escapes — so it works on a Windows console without VT just as well as on Unix. When stderr is not
a TTY (a pipe, a file, CI) `status` is a no-op and starts no thread.

The pure frame helper (`_frame`) is unit-tested without a terminal; the thread loop is exercised
behind the TTY guard.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_FRAMES = "|/-\\"
_INTERVAL = 0.1


def _frame(message: str, index: int) -> str:
    """The line drawn for animation step `index`: carriage return, a frame char, the message."""

    return f"\r{_FRAMES[index % len(_FRAMES)]} {message}"


def _clear(message: str) -> str:
    """Blank the spinner line and return the cursor to its start (no ANSI, just spaces)."""

    return "\r" + " " * (len(message) + 2) + "\r"


@contextmanager
def status(message: str, *, enabled: bool | None = None) -> Iterator[None]:
    """Animate `message` on stderr for the duration of the `with` block.

    A no-op (no thread, no output) unless stderr is a TTY; pass `enabled` explicitly to override
    the auto-detection in tests. The line is cleared on exit so the next output starts clean.
    """

    if enabled is None:
        enabled = sys.stderr.isatty()
    if not enabled:
        yield
        return

    stop = threading.Event()

    def spin() -> None:
        index = 0
        while not stop.wait(0 if index == 0 else _INTERVAL):
            sys.stderr.write(_frame(message, index))
            sys.stderr.flush()
            index += 1

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        sys.stderr.write(_clear(message))
        sys.stderr.flush()
