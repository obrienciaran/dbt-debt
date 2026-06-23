"""Dependency-graph helpers over the manifest DAG.

Unused-model propagation needs descendants: a staging model with zero direct queries is not
dead if a queried mart descends from it. This builds the adjacency once and walks it.
"""

from __future__ import annotations

from collections import defaultdict

from dbt_debt.domain import Manifest


class Graph:
    """Directed dependency graph of model unique_ids built from `depends_on` edges.

    Only edges between models are kept; dependencies on sources, seeds, or snapshots are
    ignored here because model-usage propagation runs over the model DAG.
    """

    def __init__(self, parents: dict[str, set[str]], children: dict[str, set[str]]) -> None:
        self._parents = parents
        self._children = children

    @classmethod
    def from_manifest(cls, manifest: Manifest) -> Graph:
        parents: dict[str, set[str]] = defaultdict(set)
        children: dict[str, set[str]] = defaultdict(set)
        model_ids = set(manifest.models)
        for unique_id, model in manifest.models.items():
            for upstream in model.depends_on:
                if upstream in model_ids:
                    parents[unique_id].add(upstream)
                    children[upstream].add(unique_id)
        return cls(parents, children)

    def descendants(self, unique_id: str) -> set[str]:
        """Models reachable by following `depends_on` edges downstream (excludes self)."""

        return self._reach(unique_id, self._children)

    def ancestors(self, unique_id: str) -> set[str]:
        """Models this one transitively depends on (excludes self)."""

        return self._reach(unique_id, self._parents)

    @staticmethod
    def _reach(start: str, adjacency: dict[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        stack = list(adjacency.get(start, set()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adjacency.get(node, set()))
        return seen
