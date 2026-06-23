"""Tests for the exposure-impact verdict."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.verdict.exposures import affected_exposures, unaffected_exposures

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

FCT = "model.jaffle_shop.fct_orders"
STG = "model.jaffle_shop.stg_orders"
DASHBOARD = "exposure.jaffle_shop.orders_dashboard"


def test_unaffected_when_no_dead_upstreams() -> None:
    manifest = load_manifest(FIXTURE)
    assert [e.unique_id for e in unaffected_exposures(manifest, set())] == [DASHBOARD]
    assert affected_exposures(manifest, set()) == []


def test_affected_when_upstream_model_dead() -> None:
    manifest = load_manifest(FIXTURE)
    assert [e.unique_id for e in affected_exposures(manifest, {FCT})] == [DASHBOARD]
    assert unaffected_exposures(manifest, {FCT}) == []


def test_unrelated_dead_model_leaves_exposure_unaffected() -> None:
    manifest = load_manifest(FIXTURE)
    # The dashboard depends on fct_orders, not stg_orders, so a dead stg leaves it unaffected.
    assert [e.unique_id for e in unaffected_exposures(manifest, {STG})] == [DASHBOARD]
