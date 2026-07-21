"""Tests for the manifest loader against a trimmed real-shaped fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbt_debt.artifacts.errors import ArtifactError
from dbt_debt.artifacts.manifest import load_manifest, parse_manifest

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"


def test_loads_models_tests_exposures() -> None:
    manifest = load_manifest(FIXTURE)
    assert manifest.project_name == "jaffle_shop"
    assert manifest.dbt_version == "1.8.0"
    assert set(manifest.models) == {
        "model.jaffle_shop.stg_orders",
        "model.jaffle_shop.fct_orders",
        "seed.jaffle_shop.country_codes",
    }
    assert len(manifest.tests) == 1
    assert len(manifest.exposures) == 1


def test_descriptions_and_partitioning_config_are_parsed() -> None:
    data = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": "p",
        },
        "nodes": {
            "model.p.m": {
                "resource_type": "model",
                "name": "m",
                "description": "A documented model.",
                "columns": {
                    "ID": {"name": "ID", "description": "The key."},
                    "amount": {"name": "amount", "description": "   "},
                    "note": {"name": "note"},
                },
                "config": {
                    "materialized": "incremental",
                    "partition_by": {"field": "day", "data_type": "date"},
                    "cluster_by": None,
                },
            },
            "model.p.bare": {"resource_type": "model", "name": "bare", "description": ""},
        },
    }
    manifest = parse_manifest(data)
    m = manifest.models["model.p.m"]
    # Whitespace-only descriptions do not count, and documented names are lowercased like
    # the column list itself.
    assert m.has_description is True
    assert m.documented_columns == ("id",)
    assert (m.materialized, m.partitioned, m.clustered) == ("incremental", True, False)
    bare = manifest.models["model.p.bare"]
    assert bare.has_description is False
    assert (bare.materialized, bare.partitioned, bare.clustered) == (None, False, False)


def test_adapter_type_is_parsed_and_lowercased() -> None:
    data = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": "p",
            "adapter_type": "Snowflake",
        },
        "nodes": {},
    }
    assert parse_manifest(data).adapter_type == "snowflake"
    del data["metadata"]["adapter_type"]  # type: ignore[attr-defined]
    assert parse_manifest(data).adapter_type is None


def test_model_depends_on_columns_and_contract() -> None:
    manifest = load_manifest(FIXTURE)
    fct = manifest.models["model.jaffle_shop.fct_orders"]
    assert fct.depends_on == ("model.jaffle_shop.stg_orders",)
    assert set(fct.columns) == {"order_id", "amount"}
    assert fct.contract_enforced is True

    stg = manifest.models["model.jaffle_shop.stg_orders"]
    assert stg.depends_on == ("seed.jaffle_shop.country_codes",)
    assert stg.contract_enforced is False


def test_test_attachment() -> None:
    manifest = load_manifest(FIXTURE)
    test = manifest.tests["test.jaffle_shop.not_null_fct_orders_order_id.a1b2c3"]
    assert test.attached_node == "model.jaffle_shop.fct_orders"
    assert test.column_name == "order_id"


def test_column_and_test_names_are_lowercased() -> None:
    # Mixed-case YAML column names are normalized at parse time, the same policy as
    # relation_key, so they compare equal to catalog columns and parsed query reads.
    data = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": "p",
        },
        "nodes": {
            "model.p.m": {
                "resource_type": "model",
                "name": "m",
                "columns": {"UserID": {"name": "UserID"}},
            },
            "test.p.t": {
                "resource_type": "test",
                "name": "t",
                "attached_node": "model.p.m",
                "column_name": "UserID",
            },
        },
    }
    manifest = parse_manifest(data)
    assert manifest.models["model.p.m"].columns == ("userid",)
    assert manifest.tests["test.p.t"].column_name == "userid"


def test_exposure_depends_on() -> None:
    manifest = load_manifest(FIXTURE)
    exposure = manifest.exposures["exposure.jaffle_shop.orders_dashboard"]
    assert exposure.depends_on == ("model.jaffle_shop.fct_orders",)


def test_relation_to_id_reverses_each_model_relation_key() -> None:
    manifest = load_manifest(FIXTURE)
    # Seeds and snapshots are buildable, so usage rows join back to them like any model.
    assert manifest.relation_to_id() == {
        "my-gcp-project.jaffle_shop.stg_orders": "model.jaffle_shop.stg_orders",
        "my-gcp-project.jaffle_shop.fct_orders": "model.jaffle_shop.fct_orders",
        "my-gcp-project.jaffle_shop.country_codes": "seed.jaffle_shop.country_codes",
    }


def test_seeds_are_models_tagged_by_resource_type() -> None:
    manifest = load_manifest(FIXTURE)
    seed = manifest.models["seed.jaffle_shop.country_codes"]
    assert seed.resource_type == "seed"
    assert seed.relation_key == "my-gcp-project.jaffle_shop.country_codes"
    assert seed.compiled_code is None
    assert manifest.models["model.jaffle_shop.stg_orders"].resource_type == "model"


def test_only_sources_are_relations() -> None:
    manifest = load_manifest(FIXTURE)
    assert set(manifest.relations) == {"source.jaffle_shop.raw.orders"}
    source = manifest.relations["source.jaffle_shop.raw.orders"]
    assert source.relation_key == "my-gcp-project.raw.orders"


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


def test_snapshots_parse_like_models_with_their_sql() -> None:
    data = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": "p",
        },
        "nodes": {
            "snapshot.p.orders_snapshot": {
                "resource_type": "snapshot",
                "name": "orders_snapshot",
                "database": "proj",
                "schema": "snapshots",
                "alias": "orders_snapshot",
                "depends_on": {"nodes": ["model.p.stg_orders"]},
                "compiled_code": "select * from proj.mart.stg_orders",
            }
        },
    }
    manifest = parse_manifest(data)
    snap = manifest.models["snapshot.p.orders_snapshot"]
    assert snap.resource_type == "snapshot"
    assert snap.depends_on == ("model.p.stg_orders",)
    assert snap.compiled_code is not None
    # Its dataset is dbt-managed: snapshots are built, not read.
    assert manifest.managed_datasets() == {"snapshots"}


def test_parses_semantic_models_metrics_and_saved_queries() -> None:
    data = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": "p",
        },
        "semantic_models": {
            "semantic_model.p.orders": {
                "name": "orders",
                "depends_on": {"nodes": ["model.p.fct_orders"]},
                "entities": [{"name": "order_id", "expr": None}],
                "dimensions": [{"name": "is_large", "expr": "case when amount > 100 then 1 end"}],
                "measures": [{"name": "revenue", "expr": "Amount"}],
            }
        },
        "metrics": {
            "metric.p.total_revenue": {
                "name": "total_revenue",
                "depends_on": {"nodes": ["semantic_model.p.orders"]},
            }
        },
        "saved_queries": {
            "saved_query.p.weekly": {
                "name": "weekly",
                "depends_on": {"nodes": ["metric.p.total_revenue"]},
            }
        },
    }
    manifest = parse_manifest(data)
    sem = manifest.semantic_consumers["semantic_model.p.orders"]
    assert sem.kind == "semantic_model"
    # A null expr falls back to the element name; expressions resolve to the columns they
    # read, lowercased — so order_id (name), amount (from the CASE and the measure).
    assert sem.column_refs == (
        ("model.p.fct_orders", "amount"),
        ("model.p.fct_orders", "order_id"),
    )
    metric = manifest.semantic_consumers["metric.p.total_revenue"]
    assert (metric.kind, metric.depends_on) == ("metric", ("semantic_model.p.orders",))
    saved = manifest.semantic_consumers["saved_query.p.weekly"]
    assert (saved.kind, saved.column_refs) == ("saved_query", ())


def test_malformed_manifest_raises_artifact_error_with_the_path(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{ truncated")
    with pytest.raises(ArtifactError, match=str(path)):
        load_manifest(path)


def test_non_object_manifest_raises_artifact_error(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(ArtifactError, match="not a dbt artifact"):
        load_manifest(path)


def test_invalid_utf8_manifest_raises_artifact_error(tmp_path: Path) -> None:
    # A file truncated mid-multibyte-character fails to decode before JSON parsing starts,
    # and must fail with the path rather than a UnicodeDecodeError traceback.
    path = tmp_path / "manifest.json"
    path.write_bytes(b'{"metadata": \xff}')
    with pytest.raises(ArtifactError, match="not valid UTF-8"):
        load_manifest(path)


def test_missing_manifest_raises_artifact_error(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError, match="cannot read"):
        load_manifest(tmp_path / "absent.json")
