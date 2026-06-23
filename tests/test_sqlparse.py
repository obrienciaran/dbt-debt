"""Tests for the sqlglot column resolver — the load-bearing, fragile primitive.

Pins behaviour on the known hazards: SELECT * expansion, alias/collision resolution, and
multi-hop lineage through a CTE.
"""

from __future__ import annotations

import pytest

from dbt_debt.sqlparse import (
    SqlParseError,
    build_schema,
    column_lineage_edges,
    columns_read,
    referenced_relations,
)

SCHEMA = build_schema(
    {
        "p.d.stg_orders": ("order_id", "amount", "status"),
        "p.d.customers": ("id", "name"),
    }
)


def test_columns_read_resolves_aliases_across_a_join() -> None:
    sql = """
    SELECT o.order_id, c.name
    FROM p.d.stg_orders o
    JOIN p.d.customers c ON c.id = o.order_id
    WHERE o.status = 'shipped'
    """
    assert columns_read(sql, SCHEMA) == {
        ("p.d.stg_orders", "order_id"),
        ("p.d.stg_orders", "status"),
        ("p.d.customers", "id"),
        ("p.d.customers", "name"),
    }


def test_select_star_expands_conservatively() -> None:
    # The conservative policy: * counts every column of the table as used.
    assert columns_read("SELECT * FROM p.d.stg_orders", SCHEMA) == {
        ("p.d.stg_orders", "order_id"),
        ("p.d.stg_orders", "amount"),
        ("p.d.stg_orders", "status"),
    }


def test_unparseable_sql_raises() -> None:
    with pytest.raises(SqlParseError):
        columns_read("this is not sql ((", SCHEMA)


def test_referenced_relations_keeps_only_three_part_keys() -> None:
    sql = """
    WITH base AS (SELECT id FROM p.d.customers)
    SELECT b.id FROM base b JOIN p.d.stg_orders o ON o.order_id = b.id
    """
    # `base` is a CTE (a bare name), so only the two real three-part relations survive.
    assert referenced_relations(sql) == {"p.d.customers", "p.d.stg_orders"}


def test_referenced_relations_skips_information_schema() -> None:
    sql = "SELECT table_name FROM `region-us`.INFORMATION_SCHEMA.TABLES"
    assert referenced_relations(sql) == set()


def test_referenced_relations_raises_on_unparseable_sql() -> None:
    with pytest.raises(SqlParseError):
        referenced_relations("this is not sql ((")


def test_lineage_traces_output_columns_through_a_cte() -> None:
    sql = """
    WITH base AS (
      SELECT order_id, amount, status FROM p.d.stg_orders WHERE status = 'shipped'
    )
    SELECT order_id, amount * 0.9 AS net_amount FROM base
    """
    edges = column_lineage_edges(sql, ["order_id", "net_amount"], SCHEMA)
    assert set(edges) == {
        ("p.d.stg_orders", "order_id", "order_id"),
        ("p.d.stg_orders", "amount", "net_amount"),
    }
