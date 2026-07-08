"""Tests for the removable-tests verdict."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.domain import Manifest, Model, Test
from dbt_debt.verdict.tests import removable_tests

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

FCT = "model.jaffle_shop.fct_orders"
STG = "model.jaffle_shop.stg_orders"
TEST_ID = "test.jaffle_shop.not_null_fct_orders_order_id.a1b2c3"


def test_dead_model_makes_its_tests_removable() -> None:
    manifest = load_manifest(FIXTURE)
    result = removable_tests(manifest, dead_models={FCT})
    assert [t.unique_id for t in result] == [TEST_ID]


def test_dead_column_makes_its_test_removable() -> None:
    manifest = load_manifest(FIXTURE)
    result = removable_tests(manifest, dead_models=set(), dead_columns={(FCT, "order_id")})
    assert [t.unique_id for t in result] == [TEST_ID]


def test_nothing_dead_means_no_removable_tests() -> None:
    manifest = load_manifest(FIXTURE)
    assert removable_tests(manifest, dead_models=set()) == []


def test_unrelated_dead_model_does_not_remove_test() -> None:
    manifest = load_manifest(FIXTURE)
    assert removable_tests(manifest, dead_models={STG}) == []


def test_dead_seed_makes_its_test_removable() -> None:
    # A seed is a buildable node like any model, so a test attached to a dead seed goes with it.
    manifest = Manifest(
        project_name="t",
        dbt_schema_version="",
        dbt_version=None,
        models={
            "seed.t.codes": Model(unique_id="seed.t.codes", name="codes", resource_type="seed")
        },
        tests={
            "test.t.codes": Test(
                unique_id="test.t.codes",
                name="not_null_codes_code",
                depends_on=("seed.t.codes",),
                attached_node="seed.t.codes",
                column_name="code",
            ),
        },
    )
    result = removable_tests(manifest, dead_models={"seed.t.codes"})
    assert [t.unique_id for t in result] == ["test.t.codes"]


def test_unattached_test_falls_back_to_dependencies() -> None:
    manifest = Manifest(
        project_name="t",
        dbt_schema_version="",
        dbt_version=None,
        models={
            "model.t.a": Model(unique_id="model.t.a", name="a"),
            "model.t.b": Model(unique_id="model.t.b", name="b"),
        },
        tests={
            "rel": Test(
                unique_id="rel",
                name="relationships_a_b",
                depends_on=("model.t.a", "model.t.b"),
            ),
        },
    )
    # No attached_node; removable only when every model dependency is dead.
    assert removable_tests(manifest, dead_models={"model.t.a"}) == []
    result = removable_tests(manifest, dead_models={"model.t.a", "model.t.b"})
    assert [t.unique_id for t in result] == ["rel"]
