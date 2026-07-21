"""Fixtures shared across the suite.

The warehouse SDKs are optional extras, so a developer doing live validation against a demo
project will have them installed while CI does not. Tests that pin the "extra is missing"
behaviour therefore simulate the absence instead of relying on it, so they hold either way.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import pytest


@pytest.fixture
def without_connector(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Make `import <name>` raise `ModuleNotFoundError`, however the environment is set up.

    Putting `None` in `sys.modules` is what the import machinery reads to mean "this module is
    known to be unavailable", so only the named module is affected and everything else imports
    normally. `monkeypatch` restores the original entry when the test ends.
    """

    def hide(name: str) -> None:
        monkeypatch.setitem(sys.modules, name, None)

    return hide
