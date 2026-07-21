"""Fixtures shared across the suite.

The warehouse SDKs are optional extras, so a developer doing live validation against a demo
project will have them installed while CI does not. Tests that pin the "extra is missing"
behaviour therefore simulate the absence instead of relying on it, so they hold either way.
"""

from __future__ import annotations

import builtins
import sys
from collections.abc import Callable
from typing import Any

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


@pytest.fixture
def without_package(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Make importing `<name>` or any of its submodules raise, however the environment is set up.

    `without_connector`'s `sys.modules` entry is enough for a plain module, but not for the
    google SDK: `google` is a namespace package, and once the real SDK has been imported its
    cached parents carry the submodule attributes, so `from google.cloud import bigquery` can
    succeed without consulting `sys.modules` for the hidden leaf. Failing the import call
    itself holds either way.
    """

    def hide(name: str) -> None:
        original_import = builtins.__import__

        def missing(module: str, *args: Any, **kwargs: Any) -> Any:
            if module == name or module.startswith(name + "."):
                raise ModuleNotFoundError(f"No module named {module!r}", name=module)
            return original_import(module, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", missing)

    return hide
