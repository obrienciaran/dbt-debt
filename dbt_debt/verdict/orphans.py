"""Orphan and undeclared-source verdicts — pure subtractions over relation-key sets.

Two findings fall out of comparing the warehouse against the dbt relation set:

- *undeclared sources*: a relation a model reads that dbt has no node for (`references − dbt`).
  Recoverable from the manifest alone, so always available.
- *orphaned relations*: a table physically present in a managed dataset that dbt neither defines
  nor reads (`existing − dbt − references`). Needs the warehouse inventory.

Subtracting `references` from the orphans keeps the two lists mutually exclusive: a referenced but
undefined relation is reported as an undeclared source, never as an orphan.
"""

from __future__ import annotations

from collections.abc import Iterable, Set

from dbt_debt.domain import WarehouseRelation


def undeclared_sources(references: Set[str], dbt_relation_keys: Set[str]) -> tuple[str, ...]:
    """Relations a model reads that have no dbt node, sorted by relation_key."""

    return tuple(sorted(references - dbt_relation_keys))


def orphaned_relations(
    existing: Iterable[WarehouseRelation],
    references: Set[str],
    dbt_relation_keys: Set[str],
) -> tuple[WarehouseRelation, ...]:
    """Warehouse relations with no dbt node that nothing in dbt reads, sorted by relation_key."""

    orphans = [
        relation
        for relation in existing
        if relation.relation_key not in dbt_relation_keys
        and relation.relation_key not in references
    ]
    return tuple(sorted(orphans, key=lambda relation: relation.relation_key))
