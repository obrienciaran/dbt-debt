"""Tests for the exposure-impact verdict."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.domain import Exposure, Manifest, Model
from dbt_debt.verdict.exposures import affected_exposures, dead_exposures, unaffected_exposures

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

FCT = "model.jaffle_shop.fct_orders"
STG = "model.jaffle_shop.stg_orders"
DASHBOARD = "exposure.jaffle_shop.orders_dashboard"


def _two_dep_manifest() -> Manifest:
    """A manifest with one exposure over two models, to exercise the partial/dead split."""

    return Manifest(
        project_name="p",
        dbt_schema_version="",
        dbt_version=None,
        models={
            "model.p.a": Model(unique_id="model.p.a", name="a"),
            "model.p.b": Model(unique_id="model.p.b", name="b"),
        },
        exposures={
            "exposure.p.dash": Exposure(
                unique_id="exposure.p.dash",
                name="dash",
                depends_on=("model.p.a", "model.p.b", "source.p.raw.events"),
            )
        },
    )


def test_unaffected_when_no_dead_upstreams() -> None:
    manifest = load_manifest(FIXTURE)
    assert [e.unique_id for e in unaffected_exposures(manifest, set())] == [DASHBOARD]
    assert affected_exposures(manifest, set()) == []
    assert dead_exposures(manifest, set()) == []


def test_dead_when_every_model_dep_is_dead() -> None:
    # The dashboard's only model dependency is fct_orders, so with it dead the exposure is
    # likely dead itself — and no longer listed as merely affected.
    manifest = load_manifest(FIXTURE)
    assert [e.unique_id for e in dead_exposures(manifest, {FCT})] == [DASHBOARD]
    assert affected_exposures(manifest, {FCT}) == []
    assert unaffected_exposures(manifest, {FCT}) == []


def test_partially_dead_upstreams_read_as_affected_not_dead() -> None:
    manifest = _two_dep_manifest()
    assert [e.unique_id for e in affected_exposures(manifest, {"model.p.a"})] == ["exposure.p.dash"]
    assert dead_exposures(manifest, {"model.p.a"}) == []


def test_non_model_deps_are_ignored_for_the_all_dead_rule() -> None:
    # Both models are dead; the source dependency does not keep the exposure out of the
    # dead bucket.
    manifest = _two_dep_manifest()
    dead = {"model.p.a", "model.p.b"}
    assert [e.unique_id for e in dead_exposures(manifest, dead)] == ["exposure.p.dash"]
    assert affected_exposures(manifest, dead) == []


def test_exposure_with_no_model_deps_is_never_dead() -> None:
    manifest = _two_dep_manifest()
    manifest.exposures["exposure.p.raw_only"] = Exposure(
        unique_id="exposure.p.raw_only",
        name="raw_only",
        depends_on=("source.p.raw.events",),
    )
    assert "exposure.p.raw_only" not in {
        e.unique_id for e in dead_exposures(manifest, {"model.p.a", "model.p.b"})
    }


def test_unrelated_dead_model_leaves_exposure_unaffected() -> None:
    manifest = load_manifest(FIXTURE)
    # The dashboard depends on fct_orders, not stg_orders, so a dead stg leaves it unaffected.
    assert [e.unique_id for e in unaffected_exposures(manifest, {STG})] == [DASHBOARD]
