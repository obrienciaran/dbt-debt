"""Documentation-drift verdict — declared YAML columns missing from the physical table.

A column documented in a model's YAML that no longer exists in the built relation (per
catalog.json) is stale documentation to delete. Both sides are lowercased at parse time, so
the comparison is direct. Nodes absent from the catalog are skipped: an unknown physical
schema is not drift. A stale catalog can false-positive (a column added to YAML after the
last `dbt docs generate`), which is why the report points at regenerating docs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from dbt_debt.domain import ColumnRef, Model


def phantom_columns(
    models: Mapping[str, Model],
    catalog_columns: Mapping[str, Sequence[str]],
) -> list[ColumnRef]:
    """Declared (model, column) refs with no matching physical column, sorted."""

    refs: list[ColumnRef] = []
    for unique_id, model in models.items():
        physical = set(catalog_columns.get(unique_id, ()))
        if not physical:
            continue
        refs.extend((unique_id, column) for column in model.columns if column not in physical)
    return sorted(refs)
