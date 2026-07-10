"""Tests for recovering external column consumption from query text."""

from __future__ import annotations

from dbt_debt.consumption.columns import consumed_model_columns
from dbt_debt.sqlparse import build_schema

SCHEMA = build_schema({"my-gcp-project.jaffle_shop.fct_orders": ("order_id", "amount")})
RELATION_TO_ID = {"my-gcp-project.jaffle_shop.fct_orders": "model.shop.fct_orders"}

FCT = "model.shop.fct_orders"


def test_maps_read_columns_to_model_ids() -> None:
    queries = ["SELECT order_id FROM `my-gcp-project`.`jaffle_shop`.`fct_orders`"]
    result = consumed_model_columns(queries, SCHEMA, RELATION_TO_ID)
    assert result.consumed == {(FCT, "order_id")}
    assert (result.parsed, result.unparseable) == (1, 0)


def test_columns_of_unknown_relations_are_dropped() -> None:
    queries = ["SELECT whatever FROM `my-gcp-project`.`other`.`thing`"]
    result = consumed_model_columns(queries, SCHEMA, RELATION_TO_ID)
    assert result.consumed == set()
    # A query sqlglot cannot qualify against the catalog schema (here: an unknown relation)
    # contributed nothing to the column verdicts, so it counts on the unparseable side of the
    # confidence figure just like syntactically broken SQL.
    assert (result.parsed, result.unparseable) == (0, 1)


def test_unparseable_query_is_skipped_not_fatal_and_counted() -> None:
    queries = [
        "garbage ((",
        "SELECT amount FROM `my-gcp-project`.`jaffle_shop`.`fct_orders`",
    ]
    result = consumed_model_columns(queries, SCHEMA, RELATION_TO_ID)
    assert result.consumed == {(FCT, "amount")}
    assert (result.parsed, result.unparseable) == (1, 1)


def test_no_queries_yields_zero_counts() -> None:
    result = consumed_model_columns([], SCHEMA, RELATION_TO_ID)
    assert result.consumed == set()
    assert (result.parsed, result.unparseable) == (0, 0)
