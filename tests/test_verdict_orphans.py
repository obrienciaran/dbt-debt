"""Tests for the orphan and undeclared-source verdicts."""

from __future__ import annotations

from dbt_debt.domain import WarehouseRelation
from dbt_debt.verdict.orphans import orphaned_relations, undeclared_sources

DBT = {"p.d.stg", "p.d.fct", "p.d.raw_seed"}


def test_undeclared_sources_are_referenced_non_nodes() -> None:
    references = {"p.d.stg", "p.d.raw_events", "p.d.raw_seed"}
    # stg and raw_seed are dbt nodes; only raw_events is read without a definition.
    assert undeclared_sources(references, DBT) == ("p.d.raw_events",)


def test_orphans_exclude_nodes_and_referenced_relations() -> None:
    existing = [
        WarehouseRelation("p.d.stg", "BASE TABLE"),  # a dbt node
        WarehouseRelation("p.d.raw_events", "BASE TABLE"),  # referenced -> undeclared source
        WarehouseRelation("p.d.tmp_backfill", "BASE TABLE"),  # no node, unreferenced -> orphan
        WarehouseRelation("p.d.old_view", "VIEW"),  # orphan
    ]
    references = {"p.d.stg", "p.d.raw_events"}
    orphans = orphaned_relations(existing, references, DBT)
    # Sorted by relation_key; the referenced raw_events is a source, never an orphan.
    assert [o.relation_key for o in orphans] == ["p.d.old_view", "p.d.tmp_backfill"]
    assert [o.relation_type for o in orphans] == ["VIEW", "BASE TABLE"]


def test_clean_warehouse_has_no_orphans_or_sources() -> None:
    existing = [
        WarehouseRelation("p.d.stg", "BASE TABLE"),
        WarehouseRelation("p.d.fct", "BASE TABLE"),
    ]
    references = {"p.d.stg"}
    assert orphaned_relations(existing, references, DBT) == ()
    assert undeclared_sources(references, DBT) == ()
