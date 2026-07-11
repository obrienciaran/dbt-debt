"""Unused-column verdict: external consumption plus column-lineage propagation. Pure.

The column-grain analogue of `verdict.models`. A column is dead when it has no external
consumption *and* none of its column-descendants do, so a staging column with zero direct
queries stays alive if a queried mart column is built from it.

Equivalently, a column is *alive* if it is externally consumed or an upstream of a consumed
column. We seed the alive set with the consumed columns and walk lineage edges upstream. The
conservative `SELECT *` policy lives upstream in the consumption layer (a `*` expands to every
column), so by the time columns reach here they are already counted as used.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Set

from dbt_debt.domain import ColumnEdge, ColumnRef


def dead_columns(
    all_columns: Set[ColumnRef],
    consumed: Set[ColumnRef],
    edges: Iterable[ColumnEdge],
) -> set[ColumnRef]:
    """Columns from `all_columns` with no external use on themselves or any descendant."""

    upstream: dict[ColumnRef, set[ColumnRef]] = defaultdict(set)
    for edge in edges:
        upstream[edge.downstream].add(edge.upstream)

    alive: set[ColumnRef] = set()
    stack = [column for column in consumed]
    while stack:
        column = stack.pop()
        if column in alive:
            continue
        alive.add(column)
        stack.extend(upstream.get(column, ()))

    return {column for column in all_columns if column not in alive}
