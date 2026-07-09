"""Tests for the documentation-drift verdict."""

from __future__ import annotations

from dbt_debt.domain import Model
from dbt_debt.verdict.drift import phantom_columns


def _model(columns: tuple[str, ...]) -> Model:
    return Model(unique_id="model.p.m", name="m", columns=columns)


def test_declared_column_missing_from_the_table_is_phantom() -> None:
    models = {"model.p.m": _model(("id", "legacy_score"))}
    catalog = {"model.p.m": ("id", "amount")}
    assert phantom_columns(models, catalog) == [("model.p.m", "legacy_score")]


def test_matching_declarations_are_clean() -> None:
    models = {"model.p.m": _model(("id",))}
    catalog = {"model.p.m": ("id", "amount")}
    assert phantom_columns(models, catalog) == []


def test_node_absent_from_the_catalog_is_skipped() -> None:
    # An unknown physical schema is not drift, so nothing is flagged.
    models = {"model.p.m": _model(("id", "legacy_score"))}
    assert phantom_columns(models, {}) == []
    assert phantom_columns(models, {"model.p.m": ()}) == []


def test_results_are_sorted() -> None:
    models = {
        "model.p.b": Model(unique_id="model.p.b", name="b", columns=("z", "a")),
        "model.p.a": Model(unique_id="model.p.a", name="a", columns=("x",)),
    }
    catalog = {"model.p.a": ("id",), "model.p.b": ("id",)}
    assert phantom_columns(models, catalog) == [
        ("model.p.a", "x"),
        ("model.p.b", "a"),
        ("model.p.b", "z"),
    ]
