"""Tests for scorecard assembly and the orchestration seam, driven by the fake client."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.graph import Graph
from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.cli import _emit, _infer_project, _scan
from dbt_debt.config import Config
from dbt_debt.domain import UsageRow
from dbt_debt.report.scorecard import ColumnReport, DeadColumn, build_scorecard
from tests.fakes import FakeBigQueryClient

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

STG_KEY = "my-gcp-project.jaffle_shop.stg_orders"
FCT_KEY = "my-gcp-project.jaffle_shop.fct_orders"
TEST_ID = "test.jaffle_shop.not_null_fct_orders_order_id.a1b2c3"
EXPOSURE_ID = "exposure.jaffle_shop.orders_dashboard"


def _config() -> Config:
    return Config(project_dir=FIXTURE.parent.parent, target_path=FIXTURE.parent.name)


def test_all_dead_yields_removable_tests_and_affected_exposure() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    storage = {STG_KEY: 1024, FCT_KEY: 2048}
    card = build_scorecard(manifest, graph, [], storage, _config())

    assert (card.active_models, card.unused_models) == (0, 2)
    # Both dead tables' storage is reclaimable (1024 + 2048).
    assert card.reclaimable_bytes == 3072
    assert card.removable_tests == (TEST_ID,)
    assert card.unaffected_exposures == ()
    assert card.affected_exposures == (EXPOSURE_ID,)
    # Ranked by reclaimable bytes, descending.
    assert [a.name for a in card.dead_models] == ["fct_orders", "stg_orders"]
    assert card.dead_models[0].total_bytes == 2048


def test_queried_mart_keeps_everything_active() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    usage = [UsageRow(relation_key=FCT_KEY, query_count=4)]
    card = build_scorecard(manifest, graph, usage, {}, _config())

    assert (card.active_models, card.unused_models) == (2, 0)
    assert card.removable_tests == ()
    assert card.unaffected_exposures == (EXPOSURE_ID,)
    assert card.dead_models == ()


def test_test_on_dead_column_counts_as_removable() -> None:
    # The fixture test guards fct_orders.order_id. With every model alive but that column dead,
    # the test is still removable — the column stage's dead refs reach the tests verdict.
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    usage = [UsageRow(relation_key=FCT_KEY, query_count=4)]
    columns = ColumnReport(
        active=4,
        unused=1,
        removable=0,
        dead_columns=(DeadColumn("model.jaffle_shop.fct_orders", "fct_orders", "order_id", True),),
    )
    card = build_scorecard(manifest, graph, usage, {}, _config(), column_report=columns)

    assert card.unused_models == 0
    assert card.removable_tests == (TEST_ID,)


def test_scan_orchestration_via_fake_client() -> None:
    client = FakeBigQueryClient(usage=[UsageRow(relation_key=FCT_KEY, query_count=1)])
    card = _scan(_config(), client)
    assert card.project_name == "jaffle_shop"
    assert (card.active_models, card.unused_models) == (2, 0)


def test_infer_project_from_model_database() -> None:
    manifest = load_manifest(FIXTURE)
    # Fixture models live in database "my-gcp-project"; that is the project to query.
    assert _infer_project(manifest) == "my-gcp-project"


def test_emit_writes_report_to_file_when_output_given(tmp_path: Path) -> None:
    out = tmp_path / "debt.json"
    _emit('{"unused_models": 2}', str(out))
    assert out.read_text() == '{"unused_models": 2}\n'
