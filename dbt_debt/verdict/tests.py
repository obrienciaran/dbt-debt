"""Removable-tests verdict — a pure manifest traversal, no warehouse needed.

A test becomes removable once the asset it guards is removed: the model it is attached to is
dead, or the specific column it guards is dead. This drives the "tests removable" line.

Note the interplay with `blockers`: a test that backs a *column* also makes that column
*blocked* (not trivially removable). These are consistent — the test is removable only
*conditional* on deciding to remove the asset it guards.
"""

from __future__ import annotations

from collections.abc import Set

from dbt_debt.domain import ColumnRef, Manifest, Test


def removable_tests(
    manifest: Manifest,
    dead_models: Set[str],
    dead_columns: Set[ColumnRef] = frozenset(),
) -> list[Test]:
    """Tests that become removable once the given dead assets are removed."""

    return [
        test
        for test in manifest.tests.values()
        if _is_removable(test, manifest, dead_models, dead_columns)
    ]


def _is_removable(
    test: Test,
    manifest: Manifest,
    dead_models: Set[str],
    dead_columns: Set[ColumnRef],
) -> bool:
    node = test.attached_node
    if node is not None:
        if node in dead_models:
            return True
        if test.column_name is not None and (node, test.column_name) in dead_columns:
            return True
        return False

    # Tests with no attachment (relationship/singular tests) fall back to their model
    # dependencies: removable only if every model they depend on is dead.
    model_deps = [dep for dep in test.depends_on if dep in manifest.models]
    return bool(model_deps) and all(dep in dead_models for dep in model_deps)
