"""Tests for the pure test/docs coverage counts."""

from __future__ import annotations

from dbt_debt.domain import Manifest, Model, Test
from dbt_debt.verdict.coverage import coverage


def _manifest() -> Manifest:
    models = {
        "model.p.a": Model(
            unique_id="model.p.a",
            name="a",
            columns=("id", "amount"),
            documented_columns=("id",),
            has_description=True,
        ),
        "model.p.b": Model(unique_id="model.p.b", name="b", columns=("id",)),
    }
    tests = {
        "test.p.t1": Test(unique_id="test.p.t1", name="t1", attached_node="model.p.a"),
        # A singular test carries no attached_node, only depends_on.
        "test.p.t2": Test(unique_id="test.p.t2", name="t2", depends_on=("model.p.a",)),
    }
    return Manifest(
        project_name="p", dbt_schema_version="", dbt_version=None, models=models, tests=tests
    )


def test_counts_from_the_manifest_alone() -> None:
    cov = coverage(_manifest(), None)
    assert (cov.tested_models, cov.total_models) == (1, 2)
    assert cov.documented_models == 1
    # Without a catalog the declared columns are the universe.
    assert (cov.documented_columns, cov.total_columns) == (1, 3)
    assert cov.column_source == "manifest"


def test_catalog_columns_become_the_denominator() -> None:
    # The catalog knows the physical universe: model a really has three columns (one
    # undeclared), and a documented-but-dropped column must not count.
    catalog_columns = {"model.p.a": ("id", "amount", "extra"), "model.p.b": ("id",)}
    cov = coverage(_manifest(), catalog_columns)
    assert (cov.documented_columns, cov.total_columns) == (1, 4)
    assert cov.column_source == "catalog"


def test_singular_test_still_marks_its_model_tested() -> None:
    manifest = _manifest()
    manifest.tests = {
        "test.p.t2": Test(unique_id="test.p.t2", name="t2", depends_on=("model.p.b",))
    }
    assert coverage(manifest, None).tested_models == 1
