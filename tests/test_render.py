"""Tests for the text and JSON renderers."""

from __future__ import annotations

import json

from dbt_debt.domain import WarehouseRelation
from dbt_debt.report.render_json import render_json, render_orphans_json
from dbt_debt.report.render_text import humanize_bytes, render_orphans_text, render_text
from dbt_debt.report.scorecard import (
    AffectedConsumer,
    ColumnReport,
    DeadColumn,
    DeadModel,
    OrphanReport,
    Scorecard,
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
    # Affected consumers are objects carrying kind and name, not bare unique_ids.
    assert data["affected_exposures"] == [
        {"kind": "exposure", "name": "orders_dashboard", "unique_id": "e1"}
    ]
    assert data["columns"] is None


def test_render_text_lists_affected_semantic_consumers() -> None:
    card = Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=1,
        affected_semantic=(
            AffectedConsumer("semantic_model", "orders", "semantic_model.p.orders"),
            AffectedConsumer("metric", "revenue", "metric.p.revenue"),
            AffectedConsumer("saved_query", "weekly", "saved_query.p.weekly"),
        ),
        dead_models=(DeadModel("model.x.fct", "fct", "p.d.fct", 0),),
    )
    text = render_text(card)
    assert "! 3 semantic-layer consumers affected (review before removing)" in text
    # Each consumer is named with its kind, so the reader knows what to go and check.
    assert "      - orders (semantic model)" in text
    assert "      - revenue (metric)" in text
    assert "      - weekly (saved query)" in text


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
    assert "? 1 too new to judge (first seen recently; not counted as unused)" in text
    assert "Too new to judge (1):" in text
    assert "  - brand_new  models/new.sql" in text
    # The unused list stays clean of it.
    assert "Unused models (1):" in text


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
        ),
    )


def test_render_text_with_column_section() -> None:
    out = render_text(_column_card())
    assert "Columns:" in out
    assert "  ✗ 623 unused" in out
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


def _orphan_card(checked: bool = True) -> Scorecard:
    return Scorecard(
        project_name="jaffle_shop",
        lookback_days=180,
        active_models=1,
        unused_models=0,
        orphans=OrphanReport(
            orphaned_relations=(WarehouseRelation("p.d.tmp_old", "BASE TABLE"),),
            undeclared_sources=("p.raw.events",),
            orphans_checked=checked,
        ),
    )


def test_render_text_includes_orphan_summary() -> None:
    out = render_text(_orphan_card())
    assert "Orphans:" in out
    assert "  ✗ 1 table in managed datasets with no dbt model" in out
    assert "  ! 1 source found but not declared in the manifest" in out


def test_render_text_orphan_skipped_when_metadata_unreadable() -> None:
    out = render_text(_orphan_card(checked=False))
    assert "orphan check skipped — needs bigquery.tables.list" in out
    # Undeclared sources are still reported even when the orphan listing was skipped.
    assert "1 source found but not declared" in out


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
