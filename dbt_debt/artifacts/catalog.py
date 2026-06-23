"""Load dbt's catalog.json into the full physical column list per relation.

The manifest only carries columns documented in YAML; the real, complete column universe comes
from catalog.json (produced by `dbt docs generate`). Each node also carries its warehouse stats,
so this is where per-relation byte sizes come from when no live BigQuery query is run.

Read as plain JSON, like the manifest — no dbt import.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbt_debt.artifacts._json import as_dict
from dbt_debt.domain import relation_key


@dataclass(frozen=True)
class CatalogNode:
    """One relation's physical schema and size as catalogued by the warehouse."""

    unique_id: str
    relation_key: str
    columns: tuple[str, ...]
    num_bytes: int


@dataclass(frozen=True)
class Catalog:
    """Parsed catalog.json: every model and source relation the warehouse reported."""

    nodes: dict[str, CatalogNode]

    def model_columns(self, unique_id: str) -> tuple[str, ...]:
        """Physical column names for a node, or empty if it is absent from the catalog."""

        node = self.nodes.get(unique_id)
        return node.columns if node is not None else ()

    def relation_columns(self) -> dict[str, tuple[str, ...]]:
        """relation_key -> columns across all nodes, for building the SQL parser's schema."""

        return {node.relation_key: node.columns for node in self.nodes.values()}


def load_catalog(path: str | Path) -> Catalog:
    """Read catalog.json from disk and parse it into a Catalog."""

    return parse_catalog(json.loads(Path(path).read_text()))


def parse_catalog(data: dict[str, Any]) -> Catalog:
    """Parse an already-loaded catalog dict (its `nodes` and `sources`) into a Catalog."""

    nodes: dict[str, CatalogNode] = {}
    for section in ("nodes", "sources"):
        for unique_id, node in as_dict(data.get(section)).items():
            nodes[unique_id] = _parse_node(unique_id, node)
    return Catalog(nodes=nodes)


def _parse_node(unique_id: str, node: dict[str, Any]) -> CatalogNode:
    metadata = as_dict(node.get("metadata"))
    key = relation_key(metadata.get("database"), metadata.get("schema"), metadata.get("name"))
    columns = tuple(as_dict(node.get("columns")).keys())
    return CatalogNode(
        unique_id=unique_id,
        relation_key=key,
        columns=columns,
        num_bytes=_stat_bytes(node),
    )


def _stat_bytes(node: dict[str, Any]) -> int:
    """Best-effort `num_bytes` from the node's stats; 0 when the adapter did not report it."""

    stat = as_dict(as_dict(node.get("stats")).get("num_bytes"))
    value = stat.get("value")
    try:
        return int(float(value)) if value is not None else 0
    except (TypeError, ValueError):
        return 0
