"""Tests for the "unused != removable" blocker analysis."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.verdict.blockers import analyze_columns, column_blockers

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

FCT = "model.jaffle_shop.fct_orders"
STG = "model.jaffle_shop.stg_orders"
TEST_ID = "test.jaffle_shop.not_null_fct_orders_order_id.a1b2c3"


def test_column_backed_by_test_is_blocked() -> None:
    manifest = load_manifest(FIXTURE)
    blockers = column_blockers(manifest, FCT, "order_id")
    assert blockers.backed_by_tests == (TEST_ID,)
    assert blockers.contract_enforced is True
    assert blockers.is_blocked is True


def test_contract_blocks_even_without_a_test() -> None:
    manifest = load_manifest(FIXTURE)
    blockers = column_blockers(manifest, FCT, "amount")
    assert blockers.backed_by_tests == ()
    assert blockers.contract_enforced is True
    assert blockers.is_blocked is True


def test_uncontracted_untested_column_is_clean() -> None:
    manifest = load_manifest(FIXTURE)
    # stg_orders has no enforced contract and no column tests.
    blockers = column_blockers(manifest, STG, "order_id")
    assert blockers.backed_by_tests == ()
    assert blockers.contract_enforced is False
    assert blockers.is_blocked is False


def test_analyze_columns_is_ordered_and_complete() -> None:
    manifest = load_manifest(FIXTURE)
    dead = {(FCT, "amount"), (STG, "order_id"), (FCT, "order_id")}
    result = analyze_columns(manifest, dead)
    assert [(b.model_unique_id, b.column_name) for b in result] == [
        (FCT, "amount"),
        (FCT, "order_id"),
        (STG, "order_id"),
    ]
    clean = [b for b in result if not b.is_blocked]
    assert [(b.model_unique_id, b.column_name) for b in clean] == [(STG, "order_id")]
