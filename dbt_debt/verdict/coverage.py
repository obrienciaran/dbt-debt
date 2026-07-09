"""Test and documentation coverage — pure counting over the manifest (and catalog columns).

Three hygiene figures, one sentence each in the report: how many buildable nodes have at least
one test, how many carry a description, and how many columns do. The column denominator prefers
the catalog's physical column list (the real universe, from `dbt docs generate`); without a
catalog it falls back to the YAML-declared columns, and the report says which was used.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from dbt_debt.domain import Manifest


@dataclass(frozen=True)
class Coverage:
    """Coverage counts, ready for one sentence each. `column_source` is "catalog" or "manifest"."""

    tested_models: int
    documented_models: int
    total_models: int
    documented_columns: int
    total_columns: int
    column_source: str


def coverage(manifest: Manifest, catalog_columns: Mapping[str, Sequence[str]] | None) -> Coverage:
    """Count tested and documented models, and documented columns, across the buildable nodes.

    A model counts as tested when any test attaches to it or depends on it (generic tests carry
    `attached_node`; singular tests only `depends_on`). With a catalog, documented columns are
    the YAML-documented names that physically exist; without one, the declared columns stand in
    as the universe.
    """

    tested: set[str] = set()
    for test in manifest.tests.values():
        if test.attached_node in manifest.models:
            tested.add(test.attached_node)
        tested.update(dep for dep in test.depends_on if dep in manifest.models)

    documented_models = sum(1 for model in manifest.models.values() if model.has_description)

    if catalog_columns is not None:
        total_columns = 0
        documented_columns = 0
        for unique_id, model in manifest.models.items():
            physical = set(catalog_columns.get(unique_id, ()))
            total_columns += len(physical)
            documented_columns += len(physical & set(model.documented_columns))
        column_source = "catalog"
    else:
        total_columns = sum(len(model.columns) for model in manifest.models.values())
        documented_columns = sum(
            len(model.documented_columns) for model in manifest.models.values()
        )
        column_source = "manifest"

    return Coverage(
        tested_models=len(tested),
        documented_models=documented_models,
        total_models=len(manifest.models),
        documented_columns=documented_columns,
        total_columns=total_columns,
        column_source=column_source,
    )
