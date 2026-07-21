"""Tests for the sqlglot lineage source over the fixture project."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.catalog import load_catalog
from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.domain import ColumnEdge
from dbt_debt.lineage.base import LineageSource
from dbt_debt.lineage.sqlglot_source import SqlglotLineage

FIXTURES = Path(__file__).parent / "fixtures"

STG = "model.jaffle_shop.stg_orders"
FCT = "model.jaffle_shop.fct_orders"


def test_sqlglot_source_satisfies_the_lineage_seam() -> None:
    # `LineageSource` is the seam a second lineage source would plug into; nothing else
    # references it, so this is what keeps the baseline conforming.
    assert issubclass(SqlglotLineage, LineageSource)


def test_edges_are_model_to_model_only() -> None:
    manifest = load_manifest(FIXTURES / "manifest.json")
    catalog = load_catalog(FIXTURES / "catalog.json")
    edges = set(SqlglotLineage(manifest, catalog).edges())

    # fct selects order_id, amount from stg -> two model edges. stg reads a raw source, whose
    # edges are dropped because the upstream is not a model.
    assert edges == {
        ColumnEdge(upstream=(STG, "order_id"), downstream=(FCT, "order_id")),
        ColumnEdge(upstream=(STG, "amount"), downstream=(FCT, "amount")),
    }


def test_models_without_compiled_sql_are_skipped() -> None:
    manifest = load_manifest(FIXTURES / "manifest.json")
    catalog = load_catalog(FIXTURES / "catalog.json")
    for model in manifest.models.values():
        model.compiled_code = None
    assert SqlglotLineage(manifest, catalog).edges() == []
