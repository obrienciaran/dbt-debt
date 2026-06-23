"""Tests for resolving warehouse usage rows to manifest model ids."""

from __future__ import annotations

from pathlib import Path

from dbt_debt.artifacts.manifest import load_manifest
from dbt_debt.consumption.jobs import parse_usage_rows
from dbt_debt.consumption.usage import queried_model_ids
from dbt_debt.domain import UsageRow

FIXTURE = Path(__file__).parent / "fixtures" / "manifest.json"

STG = "model.jaffle_shop.stg_orders"
FCT = "model.jaffle_shop.fct_orders"
FCT_KEY = "my-gcp-project.jaffle_shop.fct_orders"


def test_resolves_relation_key_to_model_id() -> None:
    manifest = load_manifest(FIXTURE)
    rows = [UsageRow(relation_key=FCT_KEY, query_count=3)]
    assert queried_model_ids(manifest, rows) == {FCT}


def test_relation_key_match_is_case_insensitive() -> None:
    # Both sides are canonicalised to lowercase at construction: the model via relation_key,
    # the usage row via parse_usage_rows. A warehouse row that arrives upper-cased still matches.
    manifest = load_manifest(FIXTURE)
    rows = parse_usage_rows(
        [{"relation_key": FCT_KEY.upper(), "query_count": 1, "last_queried": None}]
    )
    assert queried_model_ids(manifest, rows) == {FCT}


def test_zero_count_rows_are_ignored() -> None:
    manifest = load_manifest(FIXTURE)
    rows = [UsageRow(relation_key=FCT_KEY, query_count=0)]
    assert queried_model_ids(manifest, rows) == set()


def test_unknown_relation_is_dropped() -> None:
    manifest = load_manifest(FIXTURE)
    rows = [UsageRow(relation_key="other.dataset.table", query_count=9)]
    assert queried_model_ids(manifest, rows) == set()
    assert STG not in queried_model_ids(manifest, rows)
