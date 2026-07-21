"""Tests for the text and JSON renderers."""

from __future__ import annotations

import json
from dataclasses import replace

from dbt_debt.report.render_json import render_json, render_orphans_json
from dbt_debt.report.render_text import humanize_bytes, render_orphans_text, render_text
from dbt_debt.report.scorecard import (
    AffectedConsumer,
    ColumnReport,
    DeadColumn,
    DeadModel,
    OrphanedRelation,
    OrphanReport,
    PhantomColumn,
    RarelyUsedModel,
    Scorecard,
    StaleSource,
    UnusedSource,
)

CARD = Scorecard(
    project_name="jaffle_shop",
    lookback_days=180,
    active_models=1,
    unused_models=1,
    removable_tests=("t1",),
    unaffected_exposures=(),
    affected_exposures=(AffectedConsumer("exposure", "orders_dashboard", "e1"),),
    dead_models=(DeadModel("model.x.fct", "fct", "p.d.fct", 2048),),
    reclaimable_bytes=2048,
)


def test_humanize_bytes() -> None:
    assert humanize_bytes(512) == "512 B"
    assert humanize_bytes(1536) == "1.5 KB"
    assert humanize_bytes(2 * 1024**3) == "2.0 GB"


def test_render_text_matches_mockup() -> None:
    expected = "\n".join(
        [
            "dbt-debt scorecard — jaffle_shop",
            "Lookback: 180 days",
            "",
            "Models:",
            "  ✓ 1 active",
            "  ✗ 1 unused",
            "",
            "Potential savings:",
            "  - 1 test removable",
            "  ! 1 exposure affected (review before removing)",
            "      - orders_dashboard",
            "  - 2.0 KB reclaimable storage",
            "",
            "Top 1 of 1 unused models (most reclaimable storage first):",
            "  1. fct (2.0 KB)",
        ]
    )
    assert render_text(CARD) == expected


def test_render_json_roundtrips() -> None:
    data = json.loads(render_json(CARD))
    assert data["project_name"] == "jaffle_shop"
    assert data["unused_models"] == 1
    assert data["dead_models"][0]["total_bytes"] == 2048
    assert data["dead_models"][0]["resource_type"] == "model"
    # Affected consumers are objects carrying kind, name, and the cause, not bare unique_ids.
    assert data["affected_exposures"] == [
        {
            "kind": "exposure",
            "name": "orders_dashboard",
            "unique_id": "e1",
            "via_name": None,
            "via_kind": None,
        }
    ]
    assert data["columns"] is None


def test_render_text_lists_affected_semantic_consumers() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=1,
        affected_semantic=(
            AffectedConsumer(
                "semantic_model",
                "orders",
                "semantic_model.p.orders",
                via_name="fct",
                via_kind="model",
            ),
            AffectedConsumer(
                "metric",
                "revenue",
                "metric.p.revenue",
                via_name="orders",
                via_kind="semantic_model",
            ),
            AffectedConsumer(
                "saved_query",
                "weekly",
                "saved_query.p.weekly",
                via_name="revenue",
                via_kind="metric",
            ),
        ),
        dead_models=(DeadModel("model.x.fct", "fct", "p.d.fct", 0),),
    )
    text = render_text(card)
    assert (
        "! 3 semantic-layer consumers read unused models "
        "(they would break if those models are removed):"
    ) in text
    # Each consumer is named with its kind and what makes it affected: the unused model for
    # direct hits, the consumer in between for transitive ones.
    assert "      - orders (semantic model) — built on fct (unused)" in text
    assert "      - revenue (metric) — via orders" in text
    assert "      - weekly (saved query) — via revenue" in text


def test_render_text_details_affected_semantic_consumers() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=1,
        affected_semantic=(
            AffectedConsumer(
                "semantic_model",
                "orders",
                "semantic_model.p.orders",
                via_name="fct",
                via_kind="model",
            ),
            AffectedConsumer(
                "metric",
                "revenue",
                "metric.p.revenue",
                via_name="orders",
                via_kind="semantic_model",
            ),
        ),
        dead_models=(DeadModel("model.x.fct", "fct", "p.d.fct", 0),),
    )
    text = render_text(card, detail=True)
    assert "Semantic-layer consumers reading unused models (2):" in text
    assert "  - orders (semantic model)\n      depends on unused model: fct" in text
    assert "  - revenue (metric)\n      via orders (semantic model)" in text
    # The closing note says what "affected" means and what to do about it.
    assert "(declared use only; it does not make the model count as used" in text


def test_render_text_semantic_consumer_without_a_cause_stays_bare() -> None:
    # Handcrafted scorecards may not resolve via_name; the line degrades to name and kind.
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=1,
        affected_semantic=(AffectedConsumer("metric", "revenue", "metric.p.revenue"),),
        dead_models=(DeadModel("model.x.fct", "fct", "p.d.fct", 0),),
    )
    text = render_text(card)
    assert (
        "! 1 semantic-layer consumer reads unused models "
        "(it would break if those models are removed):"
    ) in text
    assert "      - revenue (metric)\n" in text


def test_render_text_lists_too_new_nodes_separately() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=2,
        unused_models=1,
        dead_models=(DeadModel("model.x.stg", "stg", "p.d.stg", 0),),
        too_new_models=(
            DeadModel("model.x.brand_new", "brand_new", "p.d.brand_new", 0, "models/new.sql"),
        ),
    )
    text = render_text(card, detail=True)
    assert "? 1 too new to judge (first seen recently; not counted in 'unused')" in text
    assert "Too new to judge (1):" in text
    assert "  - brand_new  models/new.sql" in text
    # The unused list stays clean of it.
    assert "Unused models (1):" in text


def test_render_text_lists_missing_first_seen_nodes_separately() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        warehouse="snowflake",
        active_models=2,
        unused_models=1,
        dead_models=(DeadModel("model.x.stg", "stg", "p.d.stg", 0),),
        missing_first_seen=(
            DeadModel("model.x.just_built", "just_built", "p.d.just_built", 0, "models/jb.sql"),
        ),
    )
    text = render_text(card, detail=True)
    assert "? 1 missing a first-seen date (age cannot be proven; not counted in 'unused')" in text
    assert "Missing a first-seen date, likely new tables (1):" in text
    assert "  - just_built  models/jb.sql" in text
    assert "ACCOUNT_USAGE.TABLES lags ~90 minutes" in text
    # The unused list stays clean of it.
    assert "Unused models (1):" in text


def test_render_text_lists_rarely_used_band_with_its_evidence() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=2,
        unused_models=0,
        rarely_used=(
            RarelyUsedModel(
                unique_id="model.x.dim",
                name="dim_old",
                relation_key="p.d.dim_old",
                query_count=2,
                last_queried="2026-06-14T12:00:00+00:00",
                total_bytes=2048,
                bytes_scanned=3 * 1024,
                file_path="models/dim_old.sql",
            ),
            RarelyUsedModel(
                unique_id="seed.x.codes",
                name="codes",
                relation_key="p.d.codes",
                query_count=1,
                last_queried=None,
                total_bytes=0,
                resource_type="seed",
            ),
        ),
        rare_threshold=5,
    )
    text = render_text(card, detail=True)
    assert "~ 2 rarely used (at most 5 queries; not counted in 'unused')" in text
    assert "Top 2 of 2 rarely used models" in text
    assert "most bytes scanned first" in text
    assert "dim_old (2 queries, last 2026-06-14, 2.0 KB, 3.0 KB scanned)" in text
    # No last-queried, size, or scanned bytes shown when unknown; non-model kinds are tagged.
    assert "codes (seed) (1 query)" in text
    assert "Rarely used models (2):" in text
    assert "models/dim_old.sql" in text
    # JSON carries the band verbatim (last_queried is already a string).
    data = json.loads(render_json(card))
    assert data["rarely_used"][0]["query_count"] == 2
    assert data["rare_threshold"] == 5


def test_render_text_says_when_retention_capped_the_window() -> None:
    card = replace(CARD, lookback_days=7, requested_lookback_days=180, warehouse="redshift")
    text = render_text(card)
    assert (
        "Only 7 days lookback displayed (180 requested but Redshift SYS views retain only 7)"
        in text
    )
    assert "Lookback: 180 days" not in text


def test_every_warehouse_phrase_agrees_in_number() -> None:
    # The subjects differ in number ("SYS views retain" but "JOBS retains"), so each phrase
    # carries its own verb; a missing one reads as broken English in the header.
    from dbt_debt.report.render_text import lookback_line

    assert lookback_line(180, "bigquery", 400) == (
        "Only 180 days lookback displayed "
        "(400 requested but BigQuery INFORMATION_SCHEMA.JOBS retains only 180)"
    )
    assert lookback_line(365, "snowflake", 400) == (
        "Only 365 days lookback displayed "
        "(400 requested but Snowflake ACCOUNT_USAGE retains only 365)"
    )
    assert lookback_line(365, "databricks", 400) == (
        "Only 365 days lookback displayed "
        "(400 requested but Databricks system tables retain only 365)"
    )
    # An unlisted warehouse still reads as a sentence.
    assert lookback_line(90, "duckdb", 400) == (
        "Only 90 days lookback displayed (400 requested but duckdb query history retains only 90)"
    )


def test_render_text_uncapped_window_reads_plainly() -> None:
    assert "Lookback: 180 days" in render_text(CARD)
    assert "requested but" not in render_text(CARD)


def test_the_rarely_used_band_counts_days_in_the_retained_window() -> None:
    # The band's sentence must quote the window the evidence actually covers, not the request.
    card = replace(
        CARD,
        lookback_days=7,
        requested_lookback_days=180,
        warehouse="redshift",
        rarely_used=(
            RarelyUsedModel(
                unique_id="model.x.dim_old",
                name="dim_old",
                relation_key="p.d.dim_old",
                query_count=2,
                last_queried=None,
                total_bytes=0,
            ),
        ),
        rare_threshold=5,
    )
    assert "at most 5 queries in 7 days" in render_text(card)


def test_json_carries_both_windows() -> None:
    capped = json.loads(render_json(replace(CARD, lookback_days=7, requested_lookback_days=180)))
    assert capped["lookback_days"] == 7
    assert capped["requested_lookback_days"] == 180
    assert json.loads(render_json(CARD))["requested_lookback_days"] is None


def test_render_text_coverage_sentences_and_unpartitioned_tables() -> None:
    from dbt_debt.report.scorecard import UnpartitionedTable
    from dbt_debt.verdict.coverage import Coverage

    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=3,
        unused_models=0,
        coverage=Coverage(
            tested_models=2,
            documented_models=1,
            total_models=3,
            documented_columns=4,
            total_columns=10,
            column_source="catalog",
        ),
        unpartitioned_tables=(
            UnpartitionedTable(
                unique_id="model.x.events",
                name="events",
                relation_key="p.d.events",
                total_bytes=12 * 1024**3,
                materialized="table",
                bytes_scanned=30 * 1024**3,
                file_path="models/events.sql",
            ),
            UnpartitionedTable(
                unique_id="model.x.archive",
                name="archive",
                relation_key="p.d.archive",
                total_bytes=8 * 1024**3,
                materialized="table",
            ),
        ),
    )
    text = render_text(card, detail=True)
    assert "Coverage:" in text
    assert "- tests: 2 of 3 models have at least one test (67%)" in text
    assert "- model docs: 1 of 3 models have a description (33%)" in text
    assert "- column docs: 4 of 10 columns have a description (40%, catalog columns)" in text
    assert "Large tables with neither partition_by nor cluster_by (2" in text
    assert "- events (12.0 GB, table, 30.0 GB scanned)" in text
    # The scanned part is dropped when the window saw no reads of the table.
    assert "- archive (8.0 GB, table)" in text
    assert "models/events.sql" in text
    data = json.loads(render_json(card))
    assert data["coverage"]["tested_models"] == 2
    assert data["unpartitioned_tables"][0]["name"] == "events"


def test_render_text_unhealthy_tables_show_only_tripped_figures() -> None:
    from dbt_debt.report.scorecard import UnhealthyTable

    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=3,
        unused_models=0,
        unhealthy_tables=(
            UnhealthyTable(
                unique_id="model.x.events",
                name="events",
                relation_key="db.s.events",
                total_bytes=12 * 1024**3,
                unsorted_percent=42.0,
                stats_off_percent=0.0,
                skew_rows=5.2,
                bytes_scanned=30 * 1024**3,
                file_path="models/events.sql",
            ),
            UnhealthyTable(
                unique_id="model.x.archive",
                name="archive",
                relation_key="db.s.archive",
                total_bytes=8 * 1024**3,
                unsorted_percent=0.0,
                stats_off_percent=100.0,
                skew_rows=0.0,
            ),
        ),
    )
    text = render_text(card, detail=True)
    assert "Large tables whose maintenance has fallen behind (2" in text
    # Only the figures at or above their threshold appear, and the scanned part is dropped
    # when the window saw no reads of the table.
    assert "- events (12.0 GB, 42% unsorted, 5.2x skew, 30.0 GB scanned)" in text
    assert "- archive (8.0 GB, stats 100% stale)" in text
    assert "models/events.sql" in text
    assert "VACUUM fixes the unsorted region" in text
    data = json.loads(render_json(card))
    assert data["unhealthy_tables"][0]["name"] == "events"
    assert data["unhealthy_tables"][0]["unsorted_percent"] == 42.0


def test_render_text_without_unhealthy_tables_says_nothing_about_maintenance() -> None:
    card = Scorecard(
        project_name="jaffle_shop", lookback_days=180, active_models=3, unused_models=0
    )
    # The empty check is the healthy state on Redshift and the only state elsewhere; it must
    # render as nothing, not noise.
    assert "maintenance" not in render_text(card, detail=True)


def test_render_text_tags_dead_seeds_and_snapshots() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=3,
        dead_models=(
            DeadModel("model.x.fct", "fct", "p.d.fct", 2048),
            DeadModel("seed.x.codes", "codes", "p.d.codes", 512, resource_type="seed"),
            DeadModel("snapshot.x.orders", "orders", "p.d.orders", 0, resource_type="snapshot"),
        ),
        reclaimable_bytes=2560,
    )
    text = render_text(card, detail=True)
    assert "✗ 3 unused (incl. 1 seed, 1 snapshot)" in text
    assert "2. codes (seed) (512 B)" in text
    assert "3. orders (snapshot)" in text
    assert "- codes (seed)  512 B" in text


def _column_card() -> Scorecard:
    return Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=1,
        removable_tests=("t1",),
        unaffected_exposures=("e1",),
        columns=ColumnReport(
            active=4382,
            unused=623,
            removable=600,
            dead_columns=(
                DeadColumn(
                    "model.p.dim_customer",
                    "dim_customer",
                    "old_marketing_score",
                    False,
                    "models/dim_customer.sql",
                ),
                DeadColumn(
                    "model.p.fct_orders", "fct_orders", "amount", True, "models/fct_orders.sql"
                ),
            ),
            parsed_queries=183,
            unparseable_queries=7,
        ),
    )


def test_render_text_with_column_section() -> None:
    out = render_text(_column_card())
    assert "Columns:" in out
    assert "  ✗ 623 unused" in out
    # The confidence sentence states how much query text the column verdicts saw.
    assert (
        "  (column verdicts based on 96% of query text, 183 of 190 queries parsed; "
        "usage verdicts are unaffected)" in out
    )
    assert "  - 600 columns removable" in out
    # Column-grain top dead assets replace the model-grain list when columns are present.
    assert (
        "Top 2 of 2 unused columns "
        "(ranked by table bytes; BigQuery has no per-column sizes):" in out
    )
    assert "  1. dim_customer.old_marketing_score" in out
    assert "  2. fct_orders.amount (blocked)" in out
    # A blocked column in the list triggers the explanatory legend.
    assert (
        "(blocked = unused but still backed by a test, enforced contract, or semantic model" in out
    )
    # No detail section unless asked.
    assert "Unused columns (" not in out


def test_render_text_detail_lists_all_grouped_by_model() -> None:
    out = render_text(_column_card(), detail=True)
    assert "Unused columns (2):" in out
    # Grouped under the owning model, with its file path, then the columns beneath.
    assert "  dim_customer  models/dim_customer.sql" in out
    assert "    - old_marketing_score" in out
    assert "  fct_orders  models/fct_orders.sql" in out
    assert "    - amount  (blocked)" in out


def test_render_text_detail_lists_dead_models() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=0,
        unused_models=2,
        dead_models=(
            DeadModel("model.x.fct", "fct", "p.d.fct", 2048, "models/fct.sql"),
            DeadModel("model.x.stg", "stg", "p.d.stg", 0, "models/stg.sql"),
        ),
        reclaimable_bytes=2048,
    )
    out = render_text(card, detail=True)
    assert "Unused models (2):" in out
    assert "  - fct  2.0 KB  models/fct.sql" in out
    # A model with no size still lists, just without a byte figure.
    assert "  - stg  models/stg.sql" in out


def _orphan_card(checked: bool = True, warehouse: str = "bigquery") -> Scorecard:
    return Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=0,
        warehouse=warehouse,
        orphans=OrphanReport(
            orphaned_relations=(OrphanedRelation("p.d.tmp_old", "BASE TABLE"),),
            undeclared_sources=("p.raw.events",),
            orphans_checked=checked,
        ),
    )


def test_render_text_includes_orphan_summary() -> None:
    out = render_text(_orphan_card())
    assert "Orphans:" in out
    assert "  ✗ 1 table in managed datasets with no dbt model" in out
    assert "  ! 1 source found but not declared in the manifest" in out


def test_render_text_orphan_query_evidence_and_ranking_note() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=0,
        orphans=OrphanReport(
            orphaned_relations=(
                OrphanedRelation("p.d.tmp_hot", "BASE TABLE", 3, "2026-07-01T00:00:00+00:00", 2048),
                OrphanedRelation("p.d.tmp_old", "BASE TABLE"),
            ),
            orphans_checked=True,
        ),
    )
    out = render_text(card, detail=True)
    assert "✗ 2 tables in managed datasets with no dbt model (1 still queried directly)" in out
    assert "Orphaned tables (2; still-queried first):" in out
    assert (
        "  - p.d.tmp_hot  (BASE TABLE)  "
        "(queried directly: 3 queries, last 2026-07-01, 2.0 KB scanned)" in out
    )
    assert "  - p.d.tmp_old  (BASE TABLE)  (no queries seen)" in out
    assert "review before dropping" in out


def test_render_text_shows_snowflake_retained_storage() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=0,
        unused_models=1,
        dead_models=(
            DeadModel(
                "model.x.fct",
                "fct",
                "p.d.fct",
                2048,
                "models/fct.sql",
                time_travel_bytes=1024,
                failsafe_bytes=1024,
            ),
        ),
        reclaimable_bytes=2048,
    )
    out = render_text(card, detail=True)
    assert "1. fct (2.0 KB) (+ 2.0 KB time-travel/fail-safe)" in out
    assert (
        "- 2.0 KB more in time-travel and fail-safe copies of the unused tables "
        "(billed until they expire)" in out
    )
    # The detail list carries the same tag next to the size and path.
    assert "  - fct  2.0 KB (+ 2.0 KB time-travel/fail-safe)  models/fct.sql" in out


def test_render_text_orphan_skipped_when_metadata_unreadable() -> None:
    out = render_text(_orphan_card(checked=False))
    assert "orphan check skipped — needs bigquery.tables.list" in out
    # Undeclared sources are still reported even when the orphan listing was skipped.
    assert "1 source found but not declared" in out


def test_orphan_skip_wording_names_each_warehouse_grant() -> None:
    # A skipped orphan check names the scanned warehouse's own grant, in the summary line and
    # the detail line both; only a BigQuery reader is pointed at bigquery.tables.list.
    grants = {
        "bigquery": "bigquery.tables.list (roles/bigquery.metadataViewer)",
        "snowflake": "USAGE on the database and its managed schemas",
        "redshift": "USAGE on the managed schemas",
        "databricks": "SELECT on system.information_schema.tables",
    }
    for warehouse, grant in grants.items():
        out = render_text(_orphan_card(checked=False, warehouse=warehouse), detail=True)
        assert f"  ⚠ orphan check skipped — needs {grant}" in out
        assert f"Orphaned tables: skipped — needs {grant}" in out
        if warehouse != "bigquery":
            assert "bigquery.tables.list" not in out
    # The focused --orphans report goes through the same helpers and follows the warehouse too.
    focused = render_orphans_text(_orphan_card(checked=False, warehouse="redshift"))
    assert "needs USAGE on the managed schemas" in focused


def test_render_text_detail_lists_orphans_and_sources() -> None:
    out = render_text(_orphan_card(), detail=True)
    assert "Orphaned tables (1):" in out
    assert "  - p.d.tmp_old  (BASE TABLE)" in out
    assert "Sources found but not declared in the manifest (1):" in out
    assert "  - p.raw.events" in out


def test_render_orphans_focused_text_and_json() -> None:
    card = _orphan_card()
    text = render_orphans_text(card)
    assert text.startswith("dbt-debt orphans — jaffle_shop")
    assert "p.d.tmp_old" in text and "p.raw.events" in text
    data = json.loads(render_orphans_json(card))
    assert data["project_name"] == "jaffle_shop"
    assert data["orphans"]["orphaned_relations"][0]["relation_key"] == "p.d.tmp_old"
    assert data["orphans"]["undeclared_sources"] == ["p.raw.events"]


def test_render_text_strips_terminal_control_sequences() -> None:
    # Names and file paths come verbatim from the manifest; embedded escape sequences must not
    # reach the terminal, where they could recolour or overwrite report lines.
    card = Scorecard(
        project_name="jaffle\x9bshop",
        lookback_days=180,
        active_models=0,
        unused_models=1,
        dead_models=(DeadModel("model.x.fct", "fct\x1b[31m", "p.d.fct", 2048, "models/fct\r.sql"),),
        reclaimable_bytes=2048,
        orphans=OrphanReport(
            orphaned_relations=(OrphanedRelation("p.d.tmp\x1b[2Kold", "BASE TABLE"),),
            orphans_checked=True,
        ),
    )
    for out in (render_text(card), render_text(card, detail=True), render_orphans_text(card)):
        assert "\x1b" not in out and "\r" not in out and "\x9b" not in out
    assert "fct[31m" in render_text(card)


def test_render_json_includes_orphans_section() -> None:
    data = json.loads(render_json(_orphan_card()))
    assert data["orphans"]["orphans_checked"] is True
    assert data["orphans"]["orphaned_relations"][0]["relation_type"] == "BASE TABLE"


def test_detail_lists_both_dead_models_and_columns_in_column_mode() -> None:
    # A column scan should show whole unused tables AND the per-column breakdown, not columns alone.
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=1,
        dead_models=(
            DeadModel("model.x.legacy", "legacy_users", "p.d.legacy", 67, "models/legacy.sql"),
        ),
        reclaimable_bytes=67,
        columns=ColumnReport(
            active=2,
            unused=1,
            removable=1,
            dead_columns=(
                DeadColumn("model.p.fct_orders", "fct_orders", "amount", False, "models/fct.sql"),
            ),
        ),
    )
    out = render_text(card, detail=True)
    assert "Unused models (1):" in out
    assert "  - legacy_users  67 B  models/legacy.sql" in out
    assert "Unused columns (1):" in out
    assert "    - amount" in out
    # Models come before columns in the breakdown.
    assert out.index("Unused models (1):") < out.index("Unused columns (1):")


def test_render_text_detail_lists_removable_tests() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=0,
        unused_models=1,
        removable_tests=("test.p.not_null_fct_order_id.abc123",),
        dead_models=(DeadModel("model.x.fct", "fct", "p.d.fct", 0),),
    )
    summary = render_text(card)
    assert "- 1 test removable" in summary
    assert "Removable tests (" not in summary  # itemized only in the detail view

    detail = render_text(card, detail=True)
    assert "Removable tests (1):" in detail
    assert "  - test.p.not_null_fct_order_id.abc123" in detail
    assert "removable once the unused model or column each one guards is removed" in detail


def test_detail_orders_orphans_before_removable_tests() -> None:
    # The breakdown mirrors the summary's order: models, orphans, then the savings.
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=0,
        unused_models=1,
        removable_tests=("t1",),
        dead_models=(DeadModel("model.x.fct", "fct", "p.d.fct", 0),),
        orphans=OrphanReport(
            orphaned_relations=(OrphanedRelation("p.d.tmp_old", "BASE TABLE"),),
            orphans_checked=True,
        ),
    )
    detail = render_text(card, detail=True)
    assert detail.index("Orphaned tables (1):") < detail.index("Removable tests (1):")


def test_render_text_lists_unused_sources_with_query_evidence() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=0,
        unused_sources=(
            UnusedSource(
                unique_id="source.p.raw.events",
                name="raw.events",
                relation_key="db.raw.events",
                query_count=3,
                last_queried="2026-06-01T00:00:00+00:00",
                bytes_scanned=5 * 1024**2,
                file_path="models/staging/sources.yml",
            ),
            UnusedSource(
                unique_id="source.p.raw.legacy",
                name="raw.legacy",
                relation_key="db.raw.legacy",
                query_count=0,
            ),
        ),
    )
    text = render_text(card)
    assert "Sources:" in text
    assert "✗ 2 declared sources nothing in the project reads" in text

    detail = render_text(card, detail=True)
    assert "Declared sources nothing in the project reads (2):" in detail
    assert (
        "  - raw.events  (queried directly: 3 queries, last 2026-06-01, 5.0 MB scanned)"
        "  models/staging/sources.yml" in detail
    )
    assert "  - raw.legacy  (no queries seen)" in detail


def test_render_text_lists_stale_sources_and_docs_drift() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=0,
        stale_sources=(
            StaleSource(
                unique_id="source.p.raw.events",
                name="raw.events",
                relation_key="db.raw.events",
                last_modified="2026-05-01T00:00:00+00:00",
                file_path="models/staging/sources.yml",
            ),
        ),
        stale_days=30,
        stale_checked=True,
        phantom_columns=(
            PhantomColumn("model.p.m", "m", "legacy_score", "models/m.sql"),
            PhantomColumn("model.p.m", "m", "old_flag", "models/m.sql"),
        ),
    )
    text = render_text(card)
    assert "! 1 source stale (no new data in 30+ days)" in text
    assert "Docs drift:" in text
    assert "! 2 documented columns no longer exist in the table" in text

    detail = render_text(card, detail=True)
    assert "Stale sources (no new data in 30+ days; 1):" in detail
    assert "  - raw.events  (last data 2026-05-01)  models/staging/sources.yml" in detail
    assert "Documented columns missing from the table (2):" in detail
    assert "  m  models/m.sql" in detail
    assert "    - legacy_score" in detail
    assert "dbt docs generate" in detail


def test_render_text_lists_dead_exposures_separately_from_affected() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=0,
        unused_models=2,
        dead_exposures=(AffectedConsumer("exposure", "orders_dashboard", "e1"),),
    )
    text = render_text(card)
    assert "! 1 exposure fed only by unused models (likely dead)" in text
    assert "      - orders_dashboard" in text
    assert "affected (review before removing)" not in text
