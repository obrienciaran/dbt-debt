"""Tests for table-grain relation references recovered from compiled model SQL."""

from __future__ import annotations

from dbt_debt.domain import Manifest, Model
from dbt_debt.references import model_relation_references


def _model(uid: str, sql: str | None) -> Model:
    return Model(unique_id=uid, name=uid, compiled_code=sql)


def _manifest(models: dict[str, Model]) -> Manifest:
    return Manifest(project_name="t", dbt_schema_version="", dbt_version=None, models=models)


def test_collects_three_part_references_across_models() -> None:
    manifest = _manifest(
        {
            "a": _model("a", "select x from `proj`.`ds`.`raw_events`"),
            "b": _model("b", "select y from proj.ds.a"),
        }
    )
    assert model_relation_references(manifest) == {"proj.ds.raw_events", "proj.ds.a"}


def test_skips_models_without_or_with_unparseable_sql() -> None:
    manifest = _manifest(
        {
            "a": _model("a", None),
            "b": _model("b", "garbage (("),
            "c": _model("c", "select 1 from proj.ds.real"),
        }
    )
    assert model_relation_references(manifest) == {"proj.ds.real"}
