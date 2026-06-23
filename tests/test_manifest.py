"""Tests for the manifest loader against a trimmed real-shaped fixture."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.manifest import load_manifest

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"


def test_loads_models_tests_exposures() -> None:
    manifest = load_manifest(FIXTURE)
    assert manifest.project_name == "jaffle_shop"
    assert manifest.dbt_version == "1.8.0"
    assert set(manifest.models) == {
        "model.jaffle_shop.stg_orders",
        "model.jaffle_shop.fct_orders",
    }
    assert len(manifest.tests) == 1
    assert len(manifest.exposures) == 1


def test_model_depends_on_columns_and_contract() -> None:
    manifest = load_manifest(FIXTURE)
    fct = manifest.models["model.jaffle_shop.fct_orders"]
    assert fct.depends_on == ("model.jaffle_shop.stg_orders",)
    assert set(fct.columns) == {"order_id", "amount"}
    assert fct.contract_enforced is True

    stg = manifest.models["model.jaffle_shop.stg_orders"]
    assert stg.depends_on == ()
    assert stg.contract_enforced is False


def test_test_attachment() -> None:
    manifest = load_manifest(FIXTURE)
    test = manifest.tests["test.jaffle_shop.not_null_fct_orders_order_id.a1b2c3"]
    assert test.attached_node == "model.jaffle_shop.fct_orders"
    assert test.column_name == "order_id"


def test_exposure_depends_on() -> None:
    manifest = load_manifest(FIXTURE)
    exposure = manifest.exposures["exposure.jaffle_shop.orders_dashboard"]
    assert exposure.depends_on == ("model.jaffle_shop.fct_orders",)


def test_relation_to_id_reverses_each_model_relation_key() -> None:
    manifest = load_manifest(FIXTURE)
    assert manifest.relation_to_id() == {
        "my-gcp-project.jaffle_shop.stg_orders": "model.jaffle_shop.stg_orders",
        "my-gcp-project.jaffle_shop.fct_orders": "model.jaffle_shop.fct_orders",
    }


def test_parses_seeds_and_sources_as_relations() -> None:
    manifest = load_manifest(FIXTURE)
    assert set(manifest.relations) == {
        "seed.jaffle_shop.country_codes",
        "source.jaffle_shop.raw.orders",
    }
    seed = manifest.relations["seed.jaffle_shop.country_codes"]
    assert seed.relation_key == "my-gcp-project.jaffle_shop.country_codes"
    assert seed.materialized is True
    source = manifest.relations["source.jaffle_shop.raw.orders"]
    assert source.relation_key == "my-gcp-project.raw.orders"
    assert source.materialized is False


def test_dbt_relation_keys_covers_models_seeds_and_sources() -> None:
    manifest = load_manifest(FIXTURE)
    assert manifest.dbt_relation_keys() == {
        "my-gcp-project.jaffle_shop.stg_orders",
        "my-gcp-project.jaffle_shop.fct_orders",
        "my-gcp-project.jaffle_shop.country_codes",
        "my-gcp-project.raw.orders",
    }


def test_managed_datasets_excludes_source_only_schemas() -> None:
    manifest = load_manifest(FIXTURE)
    # Models and the seed live in jaffle_shop; the source's raw dataset is only read, not managed.
    assert manifest.managed_datasets() == {"jaffle_shop"}
