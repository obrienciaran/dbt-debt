"""End-to-end column-stage tests: report assembly and the CLI scan with lineage on."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.catalog import load_catalog, parse_catalog
from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.domain import Manifest, Model
from dbt_debt.cli import _scan
from dbt_debt.config import Config
from dbt_debt.consumption.columns import consumed_model_columns
from dbt_debt.lineage.sqlglot_source import SqlglotLineage
from dbt_debt.report.scorecard import build_column_report
from dbt_debt.sqlparse import build_schema
from tests.fakes import FakeWarehouseClient

FIXTURES = Path(__file__).parent / "fixtures"

STG_KEY = "my-gcp-project.jaffle_shop.stg_orders"
FCT_KEY = "my-gcp-project.jaffle_shop.fct_orders"
CONSUMER_QUERY = "SELECT order_id FROM `my-gcp-project`.`jaffle_shop`.`fct_orders`"
STORAGE = {STG_KEY: 4096, FCT_KEY: 8192}


def _config() -> Config:
    return Config(
        project_dir=FIXTURES.parent.parent,
        target_path=Path("tests/fixtures"),
        columns=True,
    )


def test_build_column_report_counts_and_blockers() -> None:
    manifest = load_manifest(FIXTURES / "manifest.json")
    catalog = load_catalog(FIXTURES / "catalog.json")
    schema = build_schema(catalog.relation_columns())
    relation_to_id = {m.relation_key: uid for uid, m in manifest.models.items()}

    consumed = consumed_model_columns([CONSUMER_QUERY], schema, relation_to_id)
    edges = SqlglotLineage(manifest, catalog).edges()
    report = build_column_report(manifest, catalog, consumed, edges, STORAGE)

    # 5 physical columns, 3 dead (fct.amount, stg.amount, stg.status); stg.order_id stays alive
    # because consuming fct.order_id propagates up the lineage edge.
    assert (report.active, report.unused) == (2, 3)
    # fct.amount is dead but blocked by fct's enforced contract, so only 2 are removable.
    assert report.removable == 2
    # Ranked by owning-model bytes: fct (8192) before stg (4096).
    assert report.dead_columns[0].model_name == "fct_orders"
    assert report.dead_columns[0].column == "amount"
    assert report.dead_columns[0].blocked is True


def test_scan_with_lineage_populates_column_section() -> None:
    client = FakeWarehouseClient(query_texts=[CONSUMER_QUERY])
    card = _scan(_config(), client)
    assert card.columns is not None
    assert (card.columns.active, card.columns.unused) == (2, 3)


def test_scan_without_lineage_has_no_column_section() -> None:
    config = Config(project_dir=FIXTURES.parent.parent, target_path=Path("tests/fixtures"))
    card = _scan(config, FakeWarehouseClient())
    assert card.columns is None


def test_mixed_case_column_is_not_reported_dead_when_queried() -> None:
    # Regression: a column declared `UserID` used to be reported dead even when queried
    # directly, because the catalog kept its original case while reads were lowercased.
    manifest = Manifest(project_name="p", dbt_schema_version="v", dbt_version="1")
    manifest.models["model.p.orders"] = Model(
        unique_id="model.p.orders", name="orders", database="proj", schema="mart"
    )
    catalog = parse_catalog(
        {
            "nodes": {
                "model.p.orders": {
                    "metadata": {"database": "proj", "schema": "mart", "name": "orders"},
                    "columns": {"UserID": {}, "amount": {}},
                }
            }
        }
    )
    schema = build_schema(catalog.relation_columns())
    consumed = consumed_model_columns(
        ["SELECT UserID FROM proj.mart.orders"], schema, manifest.relation_to_id()
    )
    report = build_column_report(manifest, catalog, consumed, [], {})
    assert [c.column for c in report.dead_columns] == ["amount"]
