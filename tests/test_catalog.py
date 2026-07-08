"""Tests for the catalog.json loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbt_debt.artifacts.catalog import load_catalog, parse_catalog
from dbt_debt.artifacts.errors import ArtifactError

FIXTURE = Path(__file__).parent / "fixtures" / "catalog.json"

STG = "model.jaffle_shop.stg_orders"
SRC = "source.jaffle_shop.raw.orders"


def test_enumerates_full_physical_columns() -> None:
    catalog = load_catalog(FIXTURE)
    # status is physical but undocumented in the manifest — the catalog still sees it.
    assert catalog.model_columns(STG) == ("order_id", "amount", "status")


def test_relation_key_and_bytes() -> None:
    catalog = load_catalog(FIXTURE)
    node = catalog.nodes[STG]
    assert node.relation_key == "my-gcp-project.jaffle_shop.stg_orders"
    assert node.num_bytes == 4096


def test_sources_are_loaded_for_schema_resolution() -> None:
    catalog = load_catalog(FIXTURE)
    assert catalog.nodes[SRC].relation_key == "my-gcp-project.raw.orders"
    assert "my-gcp-project.raw.orders" in catalog.relation_columns()


def test_unknown_node_has_no_columns() -> None:
    catalog = load_catalog(FIXTURE)
    assert catalog.model_columns("model.x.missing") == ()


def test_column_names_are_lowercased() -> None:
    # Query text and lineage refs are lowercased when parsed, so the catalog's column universe
    # must be too — otherwise a mixed-case column could never join against its own reads.
    data = {
        "nodes": {
            "model.p.m": {
                "metadata": {"database": "proj", "schema": "mart", "name": "m"},
                "columns": {"UserID": {}, "amount": {}},
            }
        }
    }
    assert parse_catalog(data).model_columns("model.p.m") == ("userid", "amount")


def test_malformed_catalog_raises_artifact_error_with_the_path(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text("not json at all")
    with pytest.raises(ArtifactError, match=str(path)):
        load_catalog(path)
