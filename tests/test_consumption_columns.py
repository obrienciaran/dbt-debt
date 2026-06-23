"""Tests for recovering external column consumption from query text."""

from __future__ import annotations

from dbt_debt.consumption.columns import consumed_model_columns
from dbt_debt.sqlparse import build_schema

SCHEMA = build_schema({"my-gcp-project.jaffle_shop.fct_orders": ("order_id", "amount")})
RELATION_TO_ID = {"my-gcp-project.jaffle_shop.fct_orders": "model.shop.fct_orders"}

FCT = "model.shop.fct_orders"


def test_maps_read_columns_to_model_ids() -> None:
    queries = ["SELECT order_id FROM `my-gcp-project`.`jaffle_shop`.`fct_orders`"]
    assert consumed_model_columns(queries, SCHEMA, RELATION_TO_ID) == {(FCT, "order_id")}


def test_columns_of_unknown_relations_are_dropped() -> None:
    queries = ["SELECT whatever FROM `my-gcp-project`.`other`.`thing`"]
    assert consumed_model_columns(queries, SCHEMA, RELATION_TO_ID) == set()


def test_unparseable_query_is_skipped_not_fatal() -> None:
    queries = [
        "garbage ((",
        "SELECT amount FROM `my-gcp-project`.`jaffle_shop`.`fct_orders`",
    ]
    assert consumed_model_columns(queries, SCHEMA, RELATION_TO_ID) == {(FCT, "amount")}
