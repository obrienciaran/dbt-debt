"""Interactive terminal viewer: tabbed Summary / Detail / JSON / Export over one `Scorecard`.

Stdlib-only, no third-party dependency. A Unix backend (`termios`/`tty`) and a Windows backend
(`msvcrt`, with VT enabled via `ctypes`) feed a shared, pure draw routine; ANSI escapes do the
rendering. Switching a tab is a re-render of the same in-memory scorecard — no re-scan. When no
interactive terminal is available (piped, CI, or a terminal we can't drive) the caller falls back
to plain output, so every environment still gets the full report.

The pure pieces (`build_views`, `export_pane`, `write_report`, `render_frame`, key normalisation)
are unit-tested; the small IO loop and the Windows backend are exercised on a real terminal.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from dbt_debt.config import Config
from dbt_debt.report.render_json import render_json
from dbt_debt.report.render_text import render_text
from dbt_debt.report.scorecard import Scorecard

try:
    import select
    import termios
    import tty

    _HAS_TERMIOS = True
except ImportError:  # pragma: no cover - Windows has no termios
    _HAS_TERMIOS = False

_ALT_ON, _ALT_OFF = "\x1b[?1049h", "\x1b[?1049l"
_HIDE, _SHOW = "\x1b[?25l", "\x1b[?25h"
_CLEAR = "\x1b[H\x1b[2J"
_REVERSE, _RESET, _DIM = "\x1b[7m", "\x1b[0m", "\x1b[2m"

EXPORT_FILE = "dbt_debt_report.json"

_UNIX_KEYS: dict[bytes, str] = {
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"k": "up",
    b"j": "down",
    b"\x1b[5~": "pgup",
    b"\x1b[6~": "pgdn",
    b" ": "pgdn",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
    b"g": "home",
    b"G": "end",
    b"\t": "tab",
    b"\x1b[Z": "backtab",
    b"\r": "enter",
    b"\n": "enter",
    b"w": "write",
    b"1": "1",
    b"2": "2",
    b"3": "3",
    b"4": "4",
    b"q": "quit",
    b"\x1b": "quit",
    b"\x03": "quit",
}

# Windows: arrow/function keys arrive as a 0x00/0xe0 prefix then a code char; others as the char.
_WIN_SPECIAL: dict[str, str] = {
    "H": "up",
    "P": "down",
    "I": "pgup",
    "Q": "pgdn",
    "G": "home",
    "O": "end",
}
_WIN_CHARS: dict[str, str] = {
    "k": "up",
    "j": "down",
    " ": "pgdn",
    "g": "home",
    "G": "end",
    "\t": "tab",
    "\r": "enter",
    "\n": "enter",
    "w": "write",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    "q": "quit",
    "\x1b": "quit",
    "\x03": "quit",
}


class _SetupError(Exception):
    """Raised when the terminal cannot be put into interactive mode, so the caller falls back."""


def build_views(scorecard: Scorecard, top_n: int) -> list[tuple[str, list[str]]]:
    """The three view tabs, each a (label, lines) pair over the same scorecard."""

    return [
        ("Summary", render_text(scorecard, top_n=top_n).splitlines()),
        ("Detail", render_text(scorecard, detail=True, top_n=top_n).splitlines()),
        ("JSON", render_json(scorecard).splitlines()),
    ]


def export_pane(json_text: str, written_to: str | None) -> list[str]:
    """The 4th tab: an action pane that writes the JSON report on confirm."""

    lines = [
        "",
        "  Export the full report",
        "",
        "  Format:  JSON (every unused model and column, with file paths)",
        f"  File:    {EXPORT_FILE}   (current directory)",
        f"  Size:    {json_text.count(chr(10)) + 1} lines",
        "",
        "  Press  w  or  Enter  to write the file.",
        "",
    ]
    if written_to is not None:
        lines.append(f"  ✓ wrote report to {written_to}")
    return lines


def write_report(json_text: str, filename: str = EXPORT_FILE) -> str:
    """Write the JSON report to `filename`; return its absolute path."""

    Path(filename).write_text(json_text + "\n")
    return str(Path(filename).resolve())


def _tab_bar(views: list[tuple[str, list[str]]], active: int) -> str:
    cells = []
    for i, (label, _) in enumerate(views):
        cell = f" [{i + 1}] {label} "
        cells.append(f"{_REVERSE}{cell}{_RESET}" if i == active else cell)
    return "  ".join(cells)


def render_frame(
    views: list[tuple[str, list[str]]], active: int, offset: int, width: int, height: int
) -> str:
    """Build one full-screen frame: tab bar, the visible window of the active view, a status footer.

    Pure — given the views and a terminal size it returns the exact string to write, so the layout
    and scroll windowing are testable without a terminal.
    """

    body_h = max(1, height - 4)
    label, lines = views[active]
    total = len(lines)
    offset = max(0, min(offset, max(0, total - body_h)))
    window = lines[offset : offset + body_h]

    out = [_CLEAR, _tab_bar(views, active), _DIM + "─" * width + _RESET]
    out += [line[:width] for line in window]
    out += [""] * (body_h - len(window))  # pad short pages so the footer stays put
    out.append(_DIM + "─" * width + _RESET)
    end = min(offset + body_h, total)
    pos = f"{label}: rows {offset + 1}-{end} of {total}" if total else f"{label}: empty"
    keys = "1-4/Tab switch · ↑↓/jk scroll · PgUp/PgDn · g/G · q quit"
    out.append(_DIM + f"{pos}   {keys}"[:width] + _RESET)
    return "\n".join(out)


def interactive_supported() -> bool:
    """True when stdin/stdout are a terminal we can drive (a real TTY, with a backend available)."""

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if sys.platform == "win32":
        return True
    return _HAS_TERMIOS


def _enable_windows_vt() -> bool:
    """Turn on ANSI/VT processing for the Windows console; return whether it succeeded."""

    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    global _win_prev_mode
    _win_prev_mode = mode.value
    return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # VIRTUAL_TERMINAL_PROCESSING


_win_prev_mode: int | None = None


def _restore_windows_vt() -> None:
    if sys.platform != "win32" or _win_prev_mode is None:
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), _win_prev_mode)


class _UnixReader:
    """Raw-mode keypress reader on the alternate screen; restores the terminal on exit."""

    def __enter__(self) -> _UnixReader:
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        sys.stdout.write(_ALT_ON + _HIDE)
        sys.stdout.flush()
        return self

    def __exit__(self, *exc: object) -> None:
        sys.stdout.write(_SHOW + _ALT_OFF)
        sys.stdout.flush()
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def read_key(self) -> str | None:
        raw = os.read(self._fd, 1)
        if raw == b"\x1b":  # an escape sequence (arrows, PgUp…) — pull the rest if present
            while select.select([self._fd], [], [], 0.0006)[0]:
                raw += os.read(self._fd, 1)
        return _UNIX_KEYS.get(raw)


class _WindowsReader:
    """Same contract as `_UnixReader`, backed by `msvcrt` with VT enabled."""

    def __enter__(self) -> _WindowsReader:
        if not _enable_windows_vt():
            raise _SetupError
        sys.stdout.write(_ALT_ON + _HIDE)
        sys.stdout.flush()
        return self

    def __exit__(self, *exc: object) -> None:
        sys.stdout.write(_SHOW + _ALT_OFF)
        sys.stdout.flush()
        _restore_windows_vt()

    def read_key(self) -> str | None:
        if sys.platform != "win32":
            return None
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return _WIN_SPECIAL.get(msvcrt.getwch())
        return _WIN_CHARS.get(ch)


@contextmanager
def _terminal() -> Iterator[Callable[[], str | None]]:
    if sys.platform == "win32":
        reader: _UnixReader | _WindowsReader = _WindowsReader()
    elif _HAS_TERMIOS:
        reader = _UnixReader()
    else:  # pragma: no cover - no backend
        raise _SetupError
    with reader:
        yield reader.read_key


def run_viewer(scorecard: Scorecard, config: Config) -> bool:
    """Drive the interactive viewer. Returns False if the terminal can't be set up (then fall back)."""

    json_text = render_json(scorecard)
    base_views = build_views(scorecard, config.top_n)
    try:
        with _terminal() as read_key:
            _loop(base_views, json_text, read_key)
    except _SetupError:
        return False
    return True


def _loop(
    base_views: list[tuple[str, list[str]]], json_text: str, read_key: Callable[[], str | None]
) -> None:
    active, offsets, written_to = 0, [0, 0, 0, 0], None
    export_idx = len(base_views)
    while True:
        views = base_views + [("Export", export_pane(json_text, written_to))]
        size = shutil.get_terminal_size()
        sys.stdout.write(render_frame(views, active, offsets[active], size.columns, size.lines))
        sys.stdout.flush()
        body = max(1, size.lines - 4)
        total = len(views[active][1])
        key = read_key()

        if key == "quit":
            break
        elif key in ("1", "2", "3", "4"):
            active = min(int(key) - 1, len(views) - 1)
        elif key == "tab":
            active = (active + 1) % len(views)
        elif key == "backtab":
            active = (active - 1) % len(views)
        elif active == export_idx and key in ("write", "enter") and written_to is None:
            written_to = write_report(json_text)
        elif key == "down":
            offsets[active] += 1
        elif key == "up":
            offsets[active] -= 1
        elif key == "pgdn":
            offsets[active] += body
        elif key == "pgup":
            offsets[active] -= body
        elif key == "home":
            offsets[active] = 0
        elif key == "end":
            offsets[active] = total
        offsets[active] = max(0, min(offsets[active], max(0, total - body)))
