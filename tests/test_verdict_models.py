"""Tests for the unused-model verdict and its DAG propagation."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.graph import Graph
from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.domain import Manifest, Model
from dbt_debt.verdict.models import dead_models

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

STG = "model.jaffle_shop.stg_orders"
FCT = "model.jaffle_shop.fct_orders"
SEED = "seed.jaffle_shop.country_codes"


def _scan(manifest: Manifest, queried: set[str]) -> set[str]:
    return dead_models(manifest, Graph.from_manifest(manifest), queried)


def test_nothing_queried_means_everything_dead() -> None:
    manifest = load_manifest(FIXTURE)
    assert _scan(manifest, set()) == {STG, FCT, SEED}


def test_querying_a_descendant_keeps_its_ancestors_alive() -> None:
    manifest = load_manifest(FIXTURE)
    # fct is queried; stg feeds fct and the seed feeds stg, so all three are alive. The seed
    # staying alive pins the graph fix — model→seed edges used to be dropped.
    assert _scan(manifest, {FCT}) == set()


def test_querying_an_ancestor_does_not_save_its_descendant() -> None:
    manifest = load_manifest(FIXTURE)
    # stg is queried but its descendant fct is not, so fct is dead; the seed feeding stg lives.
    assert _scan(manifest, {STG}) == {FCT}


def test_unqueried_seed_with_no_queried_descendants_is_dead() -> None:
    manifest = load_manifest(FIXTURE)
    # Only the seed itself is queried: it is alive, both models above it are dead.
    assert _scan(manifest, {SEED}) == {STG, FCT}


def test_multi_hop_propagation() -> None:
    manifest = Manifest(
        project_name="t",
        dbt_schema_version="",
        dbt_version=None,
        models={
            "a": Model(unique_id="a", name="a"),
            "b": Model(unique_id="b", name="b", depends_on=("a",)),
            "c": Model(unique_id="c", name="c", depends_on=("b",)),
        },
    )
    # Querying the leaf keeps the whole chain alive.
    assert _scan(manifest, {"c"}) == set()
    # Querying the middle keeps a and b alive, leaves c dead.
    assert _scan(manifest, {"b"}) == {"c"}


def test_unknown_queried_id_is_ignored() -> None:
    manifest = load_manifest(FIXTURE)
    assert _scan(manifest, {"model.other.thing"}) == {STG, FCT, SEED}
