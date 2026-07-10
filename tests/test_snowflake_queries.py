"""Tests pinning the pure Snowflake ACCOUNT_USAGE SQL builders and the dbt exclusion.

These pin the account-blind query shapes (built from Snowflake's published schemas, not yet
validated live) so any drift is a conscious decision, the same way the BigQuery builders are
pinned in test_consumption_jobs.
"""

from __future__ import annotations

import pytest

from dbt_debt.config import DEFAULT_QUERY_COMMENT_PATTERN
from dbt_debt.consumption.snowflake_queries import (
    exclusion_clause,
    existing_relations_query,
    first_seen_query,
    permission_probe_query,
    query_text_query,
    source_last_modified_query,
    table_usage_query,
)


def test_usage_query_reads_access_history_not_query_text() -> None:
    sql = table_usage_query(90, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN, "qh.query_text"))
    # Usage must come from ACCESS_HISTORY metadata (the referenced_tables analogue) so an
    # unparseable query can never erase evidence of use.
    assert "SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY" in sql
    assert "LATERAL FLATTEN(input => ah.direct_objects_accessed)" in sql
    assert 'LOWER(obj.value:"objectName"::STRING) AS relation_key' in sql
    assert "qh.query_id = ah.query_id" in sql
    assert "DATEADD(day, -90, CURRENT_TIMESTAMP())" in sql
    assert "qh.query_type = 'SELECT'" in sql
    assert "qh.execution_status = 'SUCCESS'" in sql
    assert "REGEXP_COUNT(qh.query_text" in sql
    assert "'Table', 'View', 'Materialized view'" in sql
    assert "COALESCE(SUM(qh.bytes_scanned), 0) AS bytes_scanned" in sql


def test_query_text_query_aliases_to_the_shared_parser_name() -> None:
    sql = query_text_query(30, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN))
    # Aliased to `query` so jobs.parse_query_text_rows reads Snowflake rows unchanged.
    assert "SELECT query_text AS query" in sql
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY" in sql
    assert "DATEADD(day, -30, CURRENT_TIMESTAMP())" in sql
    assert "GROUP BY query_text" in sql


def test_first_seen_query_spans_dropped_incarnations() -> None:
    sql = first_seen_query()
    # MIN(created) over ACCOUNT_USAGE.TABLES *including* deleted rows, so CREATE OR REPLACE
    # rebuilds do not reset first-seen; filtering `deleted` out would break the too-new guard.
    assert "SNOWFLAKE.ACCOUNT_USAGE.TABLES" in sql
    assert "MIN(created) AS first_seen" in sql
    assert "deleted" not in sql.lower().replace("min(created)", "")
    assert "WHERE" not in sql


def test_existing_relations_query_shape() -> None:
    sql = existing_relations_query("ANALYTICS", ["Marts", "staging"])
    # One INFORMATION_SCHEMA per database covers every schema — a single filtered query,
    # compared on lowercased names.
    assert "FROM ANALYTICS.INFORMATION_SCHEMA.TABLES" in sql
    assert "LOWER(table_schema) IN ('marts', 'staging')" in sql
    assert "table_type" in sql


def test_existing_relations_query_rejects_bad_identifiers() -> None:
    with pytest.raises(ValueError):
        existing_relations_query("bad-db;", ["good"])
    with pytest.raises(ValueError):
        existing_relations_query("gooddb", ["bad schema!"])


def test_permission_probe_touches_access_history() -> None:
    assert "SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY" in permission_probe_query()


def test_exclusion_clause_uses_dollar_quoting_and_regexp_count() -> None:
    clause = exclusion_clause(r'"app":\s*"dbt"')
    # REGEXP_COUNT = 0 expresses "does not contain" (REGEXP_LIKE is whole-string anchored);
    # dollar quoting keeps backslashes and quotes verbatim, like BigQuery's raw string.
    assert clause == 'REGEXP_COUNT(query_text, $$"app":\\s*"dbt"$$) = 0'


def test_exclusion_clause_rejects_patterns_that_break_dollar_quoting() -> None:
    with pytest.raises(ValueError):
        exclusion_clause("bad$$pattern")


def test_source_last_modified_query_reads_account_usage_tables() -> None:
    sql = source_last_modified_query({"DB.RAW", "db.landing"})
    assert "FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES" in sql
    assert "MAX(last_altered) AS last_modified" in sql
    assert "deleted IS NULL" in sql
    # Keys are lowercased and deduplicated for the IN filter.
    assert "IN ('db.landing', 'db.raw')" in sql


def test_source_last_modified_query_validates_identifiers() -> None:
    with pytest.raises(ValueError):
        source_last_modified_query({"db.bad-schema"})
    with pytest.raises(ValueError):
        source_last_modified_query({"bad db.raw"})
