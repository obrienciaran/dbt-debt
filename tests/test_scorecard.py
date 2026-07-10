"""Tests for scorecard assembly and the orchestration seam, driven by the fake client."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dbt_debt.artifacts.graph import Graph
from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.cli import _emit, _infer_database, _scan
from dbt_debt.config import Config
from dbt_debt.domain import TableStorage, UsageRow
from dbt_debt.report.scorecard import ColumnReport, DeadColumn, build_scorecard
from tests.fakes import FakeWarehouseClient

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

STG_KEY = "my-gcp-project.jaffle_shop.stg_orders"
FCT_KEY = "my-gcp-project.jaffle_shop.fct_orders"
SEED_KEY = "my-gcp-project.jaffle_shop.country_codes"
SOURCE_ID = "source.jaffle_shop.raw.orders"
SOURCE_KEY = "my-gcp-project.raw.orders"
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
    # Every model the dashboard reads is dead, so it lands in the likely-dead bucket (with
    # its name, so the report can say which dashboard) rather than the affected list.
    assert card.affected_exposures == ()
    assert [(e.kind, e.name, e.unique_id) for e in card.dead_exposures] == [
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


def test_snowflake_dead_node_without_first_seen_is_set_aside() -> None:
    # ACCOUNT_USAGE.TABLES lags (~90 minutes), so on Snowflake a dead node with no first-seen
    # row cannot prove its age — it is likely a new table, not an unused one.
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    first_seen = {STG_KEY: now - timedelta(days=170), SEED_KEY: now - timedelta(days=170)}
    storage = {STG_KEY: 1024, FCT_KEY: 2048, SEED_KEY: 512}
    config = Config(
        project_dir=FIXTURE.parent.parent, target_path=FIXTURE.parent.name, warehouse="snowflake"
    )
    card = build_scorecard(manifest, graph, [], storage, config, first_seen=first_seen, now=now)

    assert [m.name for m in card.missing_first_seen] == ["fct_orders"]
    assert (card.active_models, card.unused_models) == (0, 2)
    # Everything derived from "unused" excludes the set-aside node, exactly like too-new.
    assert card.removable_tests == ()
    assert card.affected_exposures == ()
    assert card.reclaimable_bytes == 1536
    assert [m.name for m in card.dead_models] == ["stg_orders", "country_codes"]


def test_bigquery_dead_node_without_first_seen_is_judged_unused() -> None:
    # On BigQuery a missing first-seen means zero jobs in the whole lookback window — the
    # strongest unused signal there is — so nothing is set aside.
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    first_seen = {STG_KEY: now - timedelta(days=170), SEED_KEY: now - timedelta(days=170)}
    card = build_scorecard(manifest, graph, [], {}, _config(), first_seen=first_seen, now=now)

    assert card.missing_first_seen == ()
    assert card.unused_models == 3


def test_redshift_dead_node_without_first_seen_is_judged_unused() -> None:
    # On Redshift a missing first-seen means no jobs within the SYS views' retention — judged
    # normally like BigQuery; the set-aside exists only for Snowflake's lagging metadata.
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    first_seen = {STG_KEY: now - timedelta(days=170), SEED_KEY: now - timedelta(days=170)}
    config = Config(
        project_dir=FIXTURE.parent.parent, target_path=FIXTURE.parent.name, warehouse="redshift"
    )
    card = build_scorecard(manifest, graph, [], {}, config, first_seen=first_seen, now=now)

    assert card.missing_first_seen == ()
    assert card.unused_models == 3


def test_min_age_zero_disables_the_missing_first_seen_guard_too() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    config = Config(
        project_dir=FIXTURE.parent.parent,
        target_path=FIXTURE.parent.name,
        warehouse="snowflake",
        min_age_days=0,
    )
    card = build_scorecard(manifest, graph, [], {}, config, first_seen={}, now=now)
    assert card.missing_first_seen == ()
    assert card.unused_models == 3


def test_snowflake_rare_node_without_first_seen_leaves_the_band() -> None:
    # The rare band gets the same protection: a queried node whose TABLES row has not landed
    # yet cannot prove it had a full window to accumulate queries.
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    usage = [UsageRow(relation_key=FCT_KEY, query_count=2)]
    config = Config(
        project_dir=FIXTURE.parent.parent, target_path=FIXTURE.parent.name, warehouse="snowflake"
    )
    card = build_scorecard(manifest, graph, usage, {}, config, first_seen={}, now=now)
    assert card.rarely_used == ()
    # A queried node is alive, so it never lands in the missing-first-seen review list either.
    assert card.missing_first_seen == ()


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
    assert [(c.kind, c.name, c.via_name, c.via_kind) for c in card.affected_semantic] == [
        ("semantic_model", "orders", "fct_orders", "model")
    ]

    # With the mart queried nothing is dead, so nothing is flagged.
    usage = [UsageRow(relation_key=FCT_KEY, query_count=1)]
    alive = build_scorecard(manifest, graph, usage, {}, _config())
    assert alive.affected_semantic == ()


def test_rarely_used_band_reports_usage_and_bytes_without_touching_unused_figures() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    from datetime import datetime, timezone

    when = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    usage = [UsageRow(relation_key=FCT_KEY, query_count=2, last_queried=when, bytes_scanned=4096)]
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
    assert card.rarely_used[0].bytes_scanned == 4096
    assert card.removable_tests == ()
    assert card.reclaimable_bytes == 0


def test_rare_band_ranks_by_scanned_bytes_before_stored_size() -> None:
    # stg is smaller on disk but its few queries scanned more, so it outranks fct — the
    # "expensive but rarely used" model belongs at the top of the review band.
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    usage = [
        UsageRow(relation_key=FCT_KEY, query_count=2, bytes_scanned=1024),
        UsageRow(relation_key=STG_KEY, query_count=3, bytes_scanned=8192),
    ]
    storage = {FCT_KEY: 4096, STG_KEY: 512}
    card = build_scorecard(manifest, graph, usage, storage, _config())
    assert [r.name for r in card.rarely_used] == ["stg_orders", "fct_orders"]


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
    assert card.unpartitioned_tables[0].bytes_scanned == 0

    snowflake = Config(
        project_dir=FIXTURE.parent.parent,
        target_path=FIXTURE.parent.name,
        warehouse="snowflake",
    )
    assert build_scorecard(manifest, graph, [], storage, snowflake).unpartitioned_tables == ()


def test_partitioning_check_ranks_by_scanned_bytes_from_the_usage_rows() -> None:
    # stg stores less than fct but user queries scanned it far more, so it tops the list —
    # partitioning it saves the most. The bytes carried on each entry come from the same rows.
    manifest = load_manifest(FIXTURE)
    for model in manifest.models.values():
        model.materialized = "table"
    graph = Graph.from_manifest(manifest)
    storage = {FCT_KEY: 5 * 1024**3, STG_KEY: 2 * 1024**3}
    usage = [UsageRow(relation_key=STG_KEY, query_count=40, bytes_scanned=80 * 1024**3)]

    card = build_scorecard(manifest, graph, usage, storage, _config())
    assert [t.name for t in card.unpartitioned_tables] == ["stg_orders", "fct_orders"]
    assert card.unpartitioned_tables[0].bytes_scanned == 80 * 1024**3


def test_infer_database_from_model_database() -> None:
    manifest = load_manifest(FIXTURE)
    # Fixture models live in database "my-gcp-project"; that is the project to query.
    assert _infer_database(manifest) == "my-gcp-project"


def test_emit_writes_report_to_file_when_output_given(tmp_path: Path) -> None:
    out = tmp_path / "debt.json"
    _emit('{"unused_models": 2}', str(out))
    assert out.read_text() == '{"unused_models": 2}\n'


def test_unused_declared_source_carries_direct_query_evidence() -> None:
    # The fixture source is read by nothing, so it is always reported; a usage row on its
    # relation key shows up as direct-query evidence, never as a revival.
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    last = datetime(2026, 6, 1, tzinfo=timezone.utc)
    usage = [
        UsageRow(relation_key=FCT_KEY, query_count=4),
        UsageRow(relation_key=SOURCE_KEY, query_count=2, last_queried=last, bytes_scanned=1024),
    ]
    card = build_scorecard(manifest, graph, usage, {}, _config())

    [source] = card.unused_sources
    assert source.unique_id == SOURCE_ID
    assert source.name == "raw.orders"
    assert source.relation_key == SOURCE_KEY
    assert (source.query_count, source.last_queried) == (2, last.isoformat())
    assert source.bytes_scanned == 1024
    assert source.file_path == "models/staging/sources.yml"


def test_unused_declared_source_without_queries_reports_zero() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    card = build_scorecard(manifest, graph, [], {}, _config())

    [source] = card.unused_sources
    assert (source.query_count, source.last_queried) == (0, None)


def test_stale_source_is_reported_with_its_last_data_date() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    now = datetime(2026, 7, 9, tzinfo=timezone.utc)
    modified = now - timedelta(days=40)
    card = build_scorecard(
        manifest, graph, [], {}, _config(), last_modified={SOURCE_KEY: modified}, now=now
    )

    assert card.stale_checked is True
    assert card.stale_days == 30
    [stale] = card.stale_sources
    assert (stale.unique_id, stale.name, stale.relation_key) == (
        SOURCE_ID,
        "raw.orders",
        SOURCE_KEY,
    )
    assert stale.last_modified == modified.isoformat()
    assert stale.file_path == "models/staging/sources.yml"


def test_fresh_source_is_not_stale_and_disabled_check_is_unchecked() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    now = datetime(2026, 7, 9, tzinfo=timezone.utc)
    fresh = {SOURCE_KEY: now - timedelta(days=1)}

    card = build_scorecard(manifest, graph, [], {}, _config(), last_modified=fresh, now=now)
    assert card.stale_checked is True
    assert card.stale_sources == ()

    disabled = Config(
        project_dir=FIXTURE.parent.parent,
        target_path=FIXTURE.parent.name,
        stale_source_days=0,
    )
    card = build_scorecard(manifest, graph, [], {}, disabled, last_modified=fresh, now=now)
    assert card.stale_checked is False
    assert card.stale_sources == ()


def test_missing_freshness_metadata_reads_as_unchecked() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    card = build_scorecard(manifest, graph, [], {}, _config())
    assert card.stale_checked is False
    assert card.stale_sources == ()


def test_phantom_columns_come_from_the_catalog_comparison() -> None:
    # fct_orders declares order_id and amount; the catalog only has order_id, so amount is
    # stale documentation. stg_orders is absent from the catalog and therefore skipped.
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    catalog_columns = {"model.jaffle_shop.fct_orders": ("order_id",)}
    card = build_scorecard(manifest, graph, [], {}, _config(), catalog_columns=catalog_columns)

    [phantom] = card.phantom_columns
    assert (phantom.model_name, phantom.column) == ("fct_orders", "amount")

    clean = {"model.jaffle_shop.fct_orders": ("order_id", "amount")}
    card = build_scorecard(manifest, graph, [], {}, _config(), catalog_columns=clean)
    assert card.phantom_columns == ()


def test_scan_passes_source_freshness_through_and_degrades_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(timezone.utc)
    usage = [UsageRow(relation_key=FCT_KEY, query_count=1)]
    client = FakeWarehouseClient(usage=usage, last_modified={SOURCE_KEY: now - timedelta(days=90)})
    card = _scan(_config(), client)
    assert client.calls["source_last_modified"] == 1
    assert card.stale_checked is True
    assert [s.name for s in card.stale_sources] == ["raw.orders"]

    denied = FakeWarehouseClient(usage=usage, freshness_permitted=False)
    card = _scan(_config(), denied)
    assert card.stale_checked is False
    assert card.stale_sources == ()
    assert "source" in capsys.readouterr().err


def test_snowflake_storage_breakdown_lands_on_dead_models_only() -> None:
    manifest = load_manifest(FIXTURE)
    graph = Graph.from_manifest(manifest)
    storage = {FCT_KEY: 2048}
    table_storage = {
        FCT_KEY: TableStorage(active_bytes=2048, time_travel_bytes=512, failsafe_bytes=256)
    }
    card = build_scorecard(manifest, graph, [], storage, _config(), table_storage=table_storage)

    dead = {m.relation_key: m for m in card.dead_models}
    assert (dead[FCT_KEY].time_travel_bytes, dead[FCT_KEY].failsafe_bytes) == (512, 256)
    assert (dead[STG_KEY].time_travel_bytes, dead[STG_KEY].failsafe_bytes) == (0, 0)
    # The retained copies never feed the reclaimable figure, which stays the live bytes.
    assert card.reclaimable_bytes == 2048


def test_scan_on_snowflake_prefers_live_storage_metrics_over_catalog_sizes() -> None:
    config = Config(
        project_dir=FIXTURE.parent.parent,
        target_path=Path(FIXTURE.parent.name),
        warehouse="snowflake",
        min_age_days=0,
    )
    client = FakeWarehouseClient(
        table_storage={
            FCT_KEY: TableStorage(active_bytes=4096, time_travel_bytes=100, failsafe_bytes=50)
        }
    )
    card = _scan(config, client)
    assert client.calls["table_storage"] == 1
    dead = {m.relation_key: m for m in card.dead_models}
    assert dead[FCT_KEY].total_bytes == 4096
    assert (dead[FCT_KEY].time_travel_bytes, dead[FCT_KEY].failsafe_bytes) == (100, 50)


def test_scan_on_bigquery_never_asks_for_storage_metrics() -> None:
    client = FakeWarehouseClient()
    _scan(_config(), client)
    assert client.calls["table_storage"] == 0


def test_scan_on_redshift_prefers_live_storage_metrics_over_catalog_sizes() -> None:
    # SVV_TABLE_INFO active bytes replace the catalog sizes, like Snowflake's storage
    # metrics; Redshift has no time-travel or fail-safe retention, so those stay zero.
    config = Config(
        project_dir=FIXTURE.parent.parent,
        target_path=Path(FIXTURE.parent.name),
        warehouse="redshift",
        min_age_days=0,
    )
    client = FakeWarehouseClient(
        table_storage={
            FCT_KEY: TableStorage(active_bytes=4096, time_travel_bytes=0, failsafe_bytes=0)
        }
    )
    card = _scan(config, client)
    assert client.calls["table_storage"] == 1
    dead = {m.relation_key: m for m in card.dead_models}
    assert dead[FCT_KEY].total_bytes == 4096
    assert (dead[FCT_KEY].time_travel_bytes, dead[FCT_KEY].failsafe_bytes) == (0, 0)


def test_scan_on_redshift_skips_the_stale_source_check_with_a_note(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Redshift exposes no last-modified metadata, so staleness is never guessed: the client
    # is not even asked, and the skip is announced.
    now = datetime.now(timezone.utc)
    config = Config(
        project_dir=FIXTURE.parent.parent,
        target_path=Path(FIXTURE.parent.name),
        warehouse="redshift",
        min_age_days=0,
    )
    client = FakeWarehouseClient(last_modified={SOURCE_KEY: now - timedelta(days=90)})
    card = _scan(config, client)
    assert client.calls["source_last_modified"] == 0
    assert card.stale_checked is False
    assert card.stale_sources == ()
    assert "stale-source check" in capsys.readouterr().err
