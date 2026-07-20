"""Offline tests for conservative Databricks system-table SQL."""

from __future__ import annotations

import pytest
import sqlglot

from dbt_debt.config import DEFAULT_QUERY_COMMENT_PATTERN
from dbt_debt.consumption.databricks_queries import (
    exclusion_clause,
    existing_relations_query,
    first_seen_query,
    permission_probe_query,
    query_text_query,
    table_usage_query,
)


def test_preflight_touches_both_required_system_schemas() -> None:
    sql = permission_probe_query()
    assert "system.access.table_lineage" in sql
    assert "system.query.history" in sql
    assert "UNION ALL" in sql


def test_usage_query_implements_the_conservative_hybrid() -> None:
    sql = table_usage_query(90, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN, "h.statement_text"))
    assert "FROM system.access.table_lineage" in sql
    assert "FROM system.query.history" in sql
    assert "h.cache_origin_statement_id = l.statement_id" in sql
    assert "h.statement_type = 'SELECT'" in sql
    assert "h.execution_status = 'FINISHED'" in sql
    assert "REGEXP_LIKE(h.statement_text" in sql
    assert "COALESCE(NOT REGEXP_LIKE" in sql
    # Unjoinable source-only events count as use; source-to-target builds do not.
    assert "h.statement_id IS NULL" in sql
    assert "l.target_table_full_name IS NULL" in sql
    assert "l.target_path IS NULL" in sql


def test_usage_query_deduplicates_lineage_and_cache_repeats() -> None:
    sql = table_usage_query(30, "TRUE")
    assert "SELECT DISTINCT" in sql
    assert "COUNT(DISTINCT usage_id) AS query_count" in sql
    assert "cache_origin_statement_id" in sql
    assert "COALESCE(SUM(bytes_scanned), 0) AS bytes_scanned" in sql


def test_query_text_query_has_the_same_exact_filters() -> None:
    sql = query_text_query(14, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN))
    assert "statement_text AS query" in sql
    assert "statement_type = 'SELECT'" in sql
    assert "execution_status = 'FINISHED'" in sql
    assert "INTERVAL 14 DAYS" in sql
    assert "GROUP BY statement_text" in sql


def test_first_seen_uses_retained_lineage_not_unity_catalog_created() -> None:
    sql = first_seen_query()
    assert "system.access.table_lineage" in sql
    assert "source_table_full_name" in sql
    assert "target_table_full_name" in sql
    assert "MIN(event_time) AS first_seen" in sql
    assert "system.information_schema.tables" not in sql
    assert "created" not in sql.lower()


def test_relation_inventory_returns_shared_parser_aliases() -> None:
    sql = existing_relations_query({"Main.Marts", "main.staging", "MAIN.MARTS"})
    assert "system.information_schema.tables" in sql
    assert "AS relation_key" in sql
    assert "table_type" in sql
    assert "IN ('main.marts', 'main.staging')" in sql


@pytest.mark.parametrize("key", ["bad-catalog.marts", "main.bad schema", "main", "main.a.b"])
def test_relation_inventory_rejects_unsafe_identifiers(key: str) -> None:
    with pytest.raises(ValueError):
        existing_relations_query({key})


def test_exclusion_rejects_a_single_quote_that_would_end_the_raw_literal() -> None:
    with pytest.raises(ValueError, match="single quote"):
        exclusion_clause("dbt'comment")


def test_all_queries_parse_with_the_registered_databricks_dialect() -> None:
    exclusion = exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN, "h.statement_text")
    queries = (
        permission_probe_query(),
        table_usage_query(30, exclusion),
        query_text_query(30, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN)),
        first_seen_query(),
        existing_relations_query({"main.marts"}),
    )
    assert all(sqlglot.parse_one(query, read="databricks") for query in queries)
