"""Tests for the pure INFORMATION_SCHEMA SQL builders, parsers, and dbt exclusion."""

from __future__ import annotations

from datetime import datetime

import pytest

from dbt_debt.config import DEFAULT_QUERY_COMMENT_PATTERN
from dbt_debt.consumption.exclusion import exclusion_clause
from dbt_debt.consumption.jobs import (
    existing_relations_query,
    parse_query_text_rows,
    parse_relation_rows,
    parse_usage_rows,
    query_text_query,
    table_usage_query,
)


def test_usage_query_shape() -> None:
    sql = table_usage_query("US", 90, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN))
    assert "`region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sql
    assert "UNNEST(referenced_tables)" in sql
    assert "INTERVAL 90 DAY" in sql
    assert "statement_type = 'SELECT'" in sql
    assert "NOT REGEXP_CONTAINS(query" in sql


def test_query_text_query_shape() -> None:
    sql = query_text_query("US", 30, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN))
    assert "SELECT query" in sql
    assert "INTERVAL 30 DAY" in sql
    assert "GROUP BY query" in sql
    assert "NOT REGEXP_CONTAINS(query" in sql


def test_parse_query_text_rows() -> None:
    assert parse_query_text_rows([{"query": "SELECT 1"}, {"query": "SELECT 2"}]) == [
        "SELECT 1",
        "SELECT 2",
    ]


def test_invalid_region_is_rejected() -> None:
    with pytest.raises(ValueError):
        table_usage_query("US; DROP", 30, "TRUE")


def test_exclusion_clause_wraps_pattern_as_raw_string() -> None:
    clause = exclusion_clause(r'"app":\s*"dbt"')
    assert clause == "NOT REGEXP_CONTAINS(query, r'''\"app\":\\s*\"dbt\"''')"


def test_exclusion_clause_rejects_patterns_that_break_the_sql_string() -> None:
    # The pattern sits inside a raw triple-quoted BigQuery string, so these would end it early.
    with pytest.raises(ValueError):
        exclusion_clause("bad'''pattern")
    with pytest.raises(ValueError):
        exclusion_clause("ends'")


def test_parse_usage_rows() -> None:
    when = datetime(2026, 6, 1)
    rows = [{"relation_key": "P.D.T", "query_count": 5, "last_queried": when}]
    parsed = parse_usage_rows(rows)
    assert parsed[0].relation_key == "p.d.t"
    assert parsed[0].query_count == 5
    assert parsed[0].last_queried == when


def test_existing_relations_query_shape() -> None:
    sql = existing_relations_query("my-proj", ["jaffle_shop", "marts"])
    # Each managed dataset's own INFORMATION_SCHEMA.TABLES, unioned (not the region-wide view).
    assert "`my-proj`.`jaffle_shop`.INFORMATION_SCHEMA.TABLES" in sql
    assert "`my-proj`.`marts`.INFORMATION_SCHEMA.TABLES" in sql
    assert "UNION ALL" in sql
    assert "table_type" in sql


def test_existing_relations_query_rejects_bad_dataset() -> None:
    with pytest.raises(ValueError):
        existing_relations_query("my-proj", ["good", "bad; DROP"])


def test_existing_relations_query_rejects_bad_project() -> None:
    with pytest.raises(ValueError):
        existing_relations_query("bad project!", ["good"])


def test_parse_relation_rows() -> None:
    parsed = parse_relation_rows([{"relation_key": "P.D.T", "table_type": "BASE TABLE"}])
    assert parsed[0].relation_key == "p.d.t"
    assert parsed[0].relation_type == "BASE TABLE"
