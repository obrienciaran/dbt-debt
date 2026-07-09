"""Tests for scorecard assembly and the orchestration seam, driven by the fake client."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.graph import Graph
from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.cli import _emit, _infer_database, _scan
from dbt_debt.config import Config
from dbt_debt.domain import UsageRow
from dbt_debt.report.scorecard import ColumnReport, DeadColumn, build_scorecard
from tests.fakes import FakeWarehouseClient

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

STG_KEY = "my-gcp-project.jaffle_shop.stg_orders"
FCT_KEY = "my-gcp-project.jaffle_shop.fct_orders"
SEED_KEY = "my-gcp-project.jaffle_shop.country_codes"
TEST_ID = "test.jaffle_shop.not_null_fct_orders_order_id.a1b2c3"
EXPOSURE_ID = "exposure.jaffle_shop.orders_dashboard"


def _config() -> Config:
    return Config(project_dir=FIXTURE.parent.parent, target_path=FIXTURE.parent.name)


def test_all_dead_yields_removable_tests_and_affected_exposure() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    storage = {STG_KEY: 1024, FCT_KEY: 2048, SEED_KEY: 512}
    card = build_scorecard(manifest, graph, [], storage, _config())

    assert (card.active_models, card.unused_models) == (0, 3)
    # All three dead relations' storage is reclaimable (1024 + 2048 + 512) — the seed counts.
    assert card.reclaimable_bytes == 3584
    assert card.removable_tests == (TEST_ID,)
    assert card.unaffected_exposures == ()
    # Affected exposures carry their name so the report can say which dashboard is at risk.
    assert [(e.kind, e.name, e.unique_id) for e in card.affected_exposures] == [
        ("exposure", "orders_dashboard", EXPOSURE_ID)
    ]
    # Ranked by reclaimable bytes, descending, with the seed tagged by kind.
    assert [a.name for a in card.dead_models] == ["fct_orders", "stg_orders", "country_codes"]
    assert card.dead_models[0].total_bytes == 2048
    assert card.dead_models[2].resource_type == "seed"


def test_queried_mart_keeps_everything_active() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    usage = [UsageRow(relation_key=FCT_KEY, query_count=4)]
    card = build_scorecard(manifest, graph, usage, {}, _config())

    # The seed feeding the queried mart stays alive too (the model→seed edge fix).
    assert (card.active_models, card.unused_models) == (3, 0)
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


def test_too_new_node_is_set_aside_from_every_unused_figure() -> None:
    from datetime import datetime, timedelta, timezone

    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    # Nothing queried; fct first appeared two days ago, the others are window-old.
    first_seen = {
        FCT_KEY: now - timedelta(days=2),
        STG_KEY: now - timedelta(days=170),
        SEED_KEY: now - timedelta(days=170),
    }
    storage = {STG_KEY: 1024, FCT_KEY: 2048, SEED_KEY: 512}
    card = build_scorecard(manifest, graph, [], storage, _config(), first_seen=first_seen, now=now)

    assert [m.name for m in card.too_new_models] == ["fct_orders"]
    assert (card.active_models, card.unused_models) == (0, 2)
    # Everything derived from "unused" excludes the too-new node: fct's test is not
    # removable, the dashboard over fct is not affected, and its bytes are not reclaimable.
    assert card.removable_tests == ()
    assert card.affected_exposures == ()
    assert card.reclaimable_bytes == 1536
    assert [m.name for m in card.dead_models] == ["stg_orders", "country_codes"]


def test_min_age_zero_disables_the_guard() -> None:
    from datetime import datetime, timedelta, timezone

    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    first_seen = {FCT_KEY: now - timedelta(days=2)}
    config = Config(
        project_dir=FIXTURE.parent.parent, target_path=FIXTURE.parent.name, min_age_days=0
    )
    card = build_scorecard(manifest, graph, [], {}, config, first_seen=first_seen, now=now)
    assert card.too_new_models == ()
    assert card.unused_models == 3


def test_dead_model_flags_its_semantic_consumers() -> None:
    from dbt_debt.domain import SemanticConsumer

    manifest = load_manifest(FIXTURE)
    manifest.semantic_consumers["semantic_model.jaffle_shop.orders"] = SemanticConsumer(
        unique_id="semantic_model.jaffle_shop.orders",
        name="orders",
        kind="semantic_model",
        depends_on=("model.jaffle_shop.fct_orders",),
    )
    graph = Graph.from_manifest(manifest)
    card = build_scorecard(manifest, graph, [], {}, _config())
    assert [(c.kind, c.name) for c in card.affected_semantic] == [("semantic_model", "orders")]

    # With the mart queried nothing is dead, so nothing is flagged.
    usage = [UsageRow(relation_key=FCT_KEY, query_count=1)]
    alive = build_scorecard(manifest, graph, usage, {}, _config())
    assert alive.affected_semantic == ()


def test_rarely_used_band_reports_usage_and_bytes_without_touching_unused_figures() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    from datetime import datetime, timezone

    when = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    usage = [UsageRow(relation_key=FCT_KEY, query_count=2, last_queried=when)]
    storage = {FCT_KEY: 2048}
    card = build_scorecard(manifest, graph, usage, storage, _config())

    # fct is queried (so alive, keeping its ancestors alive) but lands in the review band
    # with the evidence attached; nothing unused-derived changes.
    assert (card.active_models, card.unused_models) == (3, 0)
    assert card.rare_threshold == 5
    assert [(r.name, r.query_count, r.total_bytes) for r in card.rarely_used] == [
        ("fct_orders", 2, 2048)
    ]
    assert card.rarely_used[0].last_queried == when.isoformat()
    assert card.removable_tests == ()
    assert card.reclaimable_bytes == 0


def test_rare_threshold_zero_disables_the_band() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    usage = [UsageRow(relation_key=FCT_KEY, query_count=1)]
    config = Config(
        project_dir=FIXTURE.parent.parent, target_path=FIXTURE.parent.name, rare_threshold=0
    )
    card = build_scorecard(manifest, graph, usage, {}, config)
    assert card.rarely_used == ()
    assert card.rare_threshold == 0


def test_busy_models_stay_out_of_the_band() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    usage = [UsageRow(relation_key=FCT_KEY, query_count=100)]
    card = build_scorecard(manifest, graph, usage, {}, _config())
    assert card.rarely_used == ()


def test_too_new_models_are_excluded_from_the_band_too() -> None:
    from datetime import datetime, timedelta, timezone

    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    # fct was created two days ago and queried twice — it has not had a full window to
    # accumulate queries, so calling it rarely used would be as false-confident as calling a
    # too-new model unused.
    usage = [UsageRow(relation_key=FCT_KEY, query_count=2)]
    first_seen = {FCT_KEY: now - timedelta(days=2)}
    card = build_scorecard(manifest, graph, usage, {}, _config(), first_seen=first_seen, now=now)
    assert card.rarely_used == ()


def test_scan_orchestration_via_fake_client() -> None:
    client = FakeWarehouseClient(usage=[UsageRow(relation_key=FCT_KEY, query_count=1)])
    card = _scan(_config(), client)
    assert card.project_name == "jaffle_shop"
    assert (card.active_models, card.unused_models) == (3, 0)


def test_scorecard_always_carries_coverage() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    card = build_scorecard(manifest, graph, [], {}, _config())
    assert card.coverage is not None
    assert card.coverage.total_models == 3
    # The fixture test guards fct_orders, so exactly one model is tested.
    assert card.coverage.tested_models == 1
    assert card.coverage.column_source == "manifest"


def test_partitioning_check_is_bigquery_only() -> None:
    manifest = load_manifest(FIXTURE)
    for model in manifest.models.values():
        model.materialized = "table"
    graph = Graph.from_manifest(manifest)
    storage = {FCT_KEY: 5 * 1024**3}

    card = build_scorecard(manifest, graph, [], storage, _config())
    assert [t.name for t in card.unpartitioned_tables] == ["fct_orders"]
    assert card.unpartitioned_tables[0].total_bytes == 5 * 1024**3

    snowflake = Config(
        project_dir=FIXTURE.parent.parent,
        target_path=FIXTURE.parent.name,
        warehouse="snowflake",
    )
    assert build_scorecard(manifest, graph, [], storage, snowflake).unpartitioned_tables == ()


def test_infer_database_from_model_database() -> None:
    manifest = load_manifest(FIXTURE)
    # Fixture models live in database "my-gcp-project"; that is the project to query.
    assert _infer_database(manifest) == "my-gcp-project"


def test_emit_writes_report_to_file_when_output_given(tmp_path: Path) -> None:
    out = tmp_path / "debt.json"
    _emit('{"unused_models": 2}', str(out))
    assert out.read_text() == '{"unused_models": 2}\n'
