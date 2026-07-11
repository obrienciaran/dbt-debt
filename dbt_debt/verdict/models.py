"""Unused-model verdict: usage plus DAG propagation. Pure.

The rule is *not* "no one queried this model". A staging model with zero direct queries is
still alive if a queried mart descends from it. So a model is dead only when neither it nor any
descendant was queried.

Equivalently, a model is *alive* when it is a queried model or an ancestor of one. Propagating
ancestors up from the queried set is cheaper and clearer than testing every model's descendants,
and gives the same answer. The graph is passed in (built from the manifest) so this layer does
no I/O and never imports `artifacts` or `consumption` at runtime.
"""

from __future__ import annotations

from collections.abc import Set
from typing import TYPE_CHECKING

from dbt_debt.domain import Manifest

if TYPE_CHECKING:
    from dbt_debt.artifacts.graph import Graph


def dead_models(manifest: Manifest, graph: Graph, queried_models: Set[str]) -> set[str]:
    """Model unique_ids with no user query on themselves or any descendant."""

    alive: set[str] = set()
    for unique_id in queried_models:
        if unique_id in manifest.models:
            alive.add(unique_id)
            alive |= graph.ancestors(unique_id)
    return set(manifest.models) - alive
