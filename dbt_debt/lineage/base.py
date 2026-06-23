"""The lineage seam: a source of column-level dependency edges.

Keeping this a Protocol lets the `sqlglot` baseline and an optional Fusion reader be swapped
without touching the verdict layer, which only ever sees `ColumnEdge`s.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dbt_debt.domain import ColumnEdge


@runtime_checkable
class LineageSource(Protocol):
    """Anything that can produce the project's column-lineage edges."""

    def edges(self) -> list[ColumnEdge]:
        """All upstream→downstream column edges across the project's models."""
        ...
