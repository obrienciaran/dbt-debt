"""Tests for DAG descendant/ancestor traversal used by unused-model propagation."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.graph import Graph
from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.domain import Manifest, Model

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

STG = "model.jaffle_shop.stg_orders"
FCT = "model.jaffle_shop.fct_orders"
SEED = "seed.jaffle_shop.country_codes"


def test_descendants_and_ancestors_from_fixture() -> None:
    graph = Graph.from_manifest(load_manifest(FIXTURE))
    # The seed is a graph node like any model, so the model→seed edge is kept and a queried
    # mart reaches its seed through ancestors().
    assert graph.descendants(SEED) == {STG, FCT}
    assert graph.descendants(STG) == {FCT}
    assert graph.ancestors(FCT) == {STG, SEED}
    assert graph.descendants(FCT) == set()
    assert graph.ancestors(STG) == {SEED}


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
    graph = Graph.from_manifest(manifest)
    assert graph.descendants("a") == {"b", "c"}
    assert graph.ancestors("c") == {"a", "b"}


def test_external_dependencies_are_ignored() -> None:
    manifest = Manifest(
        project_name="t",
        dbt_schema_version="",
        dbt_version=None,
        models={
            "m": Model(
                unique_id="m",
                name="m",
                depends_on=("source.t.raw.orders", "seed.t.lookup"),
            ),
        },
    )
    graph = Graph.from_manifest(manifest)
    assert graph.descendants("m") == set()
    assert graph.ancestors("m") == set()
