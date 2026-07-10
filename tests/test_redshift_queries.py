"""Tests pinning the pure Redshift SYS/SVV SQL builders and the dbt exclusion.

These pin the query shapes (built from AWS's published system-view schemas, not yet validated
live) so any drift is a conscious decision, the same way the BigQuery and Snowflake builders
are pinned in test_consumption_jobs and test_snowflake_queries.
"""

from __future__ import annotations

import pytest

from dbt_debt.config import DEFAULT_QUERY_COMMENT_PATTERN
from dbt_debt.consumption.redshift_queries import (
    exclusion_clause,
    existing_relations_query,
    first_seen_query,
    permission_probe_query,
    query_text_query,
    table_storage_query,
    table_usage_query,
)


def test_usage_query_reads_scan_steps_not_query_text() -> None:
    sql = table_usage_query(90, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN, "qh.query_text"))
    # Usage must come from SYS_QUERY_DETAIL scan-step metadata (the referenced_tables
    # analogue) so an unparseable query can never erase evidence of use.
    assert "FROM sys_query_history AS qh" in sql
    assert "JOIN sys_query_detail AS qd" in sql
    assert "qd.step_name = 'scan'" in sql
    assert "LOWER(qd.table_name) AS relation_key" in sql
    assert "DATEADD(day, -90, GETDATE())" in sql
    assert "qh.query_type = 'SELECT'" in sql
    assert "qh.status = 'success'" in sql
    assert "REGEXP_COUNT(qh.query_text" in sql
    assert "COALESCE(SUM(qd.output_bytes), 0) AS bytes_scanned" in sql


def test_usage_query_follows_result_cache_hits_to_the_originating_scan() -> None:
    sql = table_usage_query(30, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN, "qh.query_text"))
    # A result-cache hit runs no scan steps of its own, so the join follows
    # result_cache_query_id back to the originating query; cached repeats still count as use.
    assert "COALESCE(NULLIF(qh.result_cache_query_id, 0), qh.query_id)" in sql
    # Repeats and multiple scan steps per query must not inflate the count.
    assert "COUNT(DISTINCT qh.query_id) AS query_count" in sql


def test_usage_query_skips_optimizer_temp_tables_and_system_schemas() -> None:
    sql = table_usage_query(30, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN, "qh.query_text"))
    assert "qd.table_name LIKE '%.%.%'" in sql
    assert "NOT LIKE '%volt_tt_%'" in sql
    assert "SPLIT_PART(qd.table_name, '.', 2) NOT IN" in sql
    assert "'pg_catalog', 'pg_internal', 'information_schema'" in sql


def test_query_text_query_aliases_to_the_shared_parser_name() -> None:
    sql = query_text_query(30, exclusion_clause(DEFAULT_QUERY_COMMENT_PATTERN))
    # Aliased to `query` so jobs.parse_query_text_rows reads Redshift rows unchanged.
    assert "SELECT query_text AS query" in sql
    assert "FROM sys_query_history" in sql
    assert "DATEADD(day, -30, GETDATE())" in sql
    assert "GROUP BY query_text" in sql


def test_first_seen_query_reads_job_history_over_every_statement_type() -> None:
    sql = first_seen_query()
    # First-seen comes from the query history (dbt builds included), never from
    # SVV_TABLE_INFO.create_time, which resets on every rebuild; unwindowed because the SYS
    # views bound their own retention.
    assert "MIN(qh.start_time) AS first_seen" in sql
    assert "sys_query_detail" in sql
    assert "query_type" not in sql
    assert "step_name" not in sql
    assert "DATEADD" not in sql
    assert "svv_table_info" not in sql.lower()


def test_first_seen_query_folds_the_dbt_tmp_suffix_onto_the_final_name() -> None:
    sql = first_seen_query()
    # dbt-redshift builds `<name>__dbt_tmp` and renames it into place; the rename is DDL the
    # history never records, so the tmp incarnation must date the final relation (confirmed
    # live: without this, dbt-built tables have no first-seen row at all).
    assert "REGEXP_REPLACE(LOWER(qd.table_name), '__dbt_tmp$', '')" in sql


def test_existing_relations_query_shape() -> None:
    sql = existing_relations_query("dev", ["Marts", "staging"])
    # SVV_REDSHIFT_TABLES rather than SVV_TABLE_INFO: the latter omits empty tables, and an
    # empty leftover is still an orphan. Compared on lowercased names.
    assert "FROM svv_redshift_tables" in sql
    assert "LOWER(database_name) = 'dev'" in sql
    assert "LOWER(schema_name) IN ('marts', 'staging')" in sql
    assert "table_type" in sql


def test_existing_relations_query_rejects_bad_identifiers() -> None:
    with pytest.raises(ValueError):
        existing_relations_query("bad-db;", ["good"])
    with pytest.raises(ValueError):
        existing_relations_query("gooddb", ["bad schema!"])


def test_permission_probe_checks_all_rows_visibility() -> None:
    sql = permission_probe_query()
    # The SYS views are readable by everyone but row-filtered for regular users, so the probe
    # must test the visibility condition itself: superuser or SYSLOG ACCESS UNRESTRICTED.
    assert "FROM svv_user_info" in sql
    assert "user_name = current_user" in sql
    assert "superuser OR syslog_access = 'UNRESTRICTED'" in sql


def test_exclusion_clause_uses_dollar_quoting_and_regexp_count() -> None:
    clause = exclusion_clause(r'"app":\s*"dbt"')
    # Same "does not contain" form as Snowflake: Redshift supports REGEXP_COUNT and
    # dollar-quoted literals, keeping backslashes and quotes verbatim.
    assert clause == 'REGEXP_COUNT(query_text, $$"app":\\s*"dbt"$$) = 0'


def test_exclusion_clause_rejects_patterns_that_break_dollar_quoting() -> None:
    with pytest.raises(ValueError):
        exclusion_clause("bad$$pattern")


def test_table_storage_query_converts_blocks_to_bytes() -> None:
    sql = table_storage_query()
    # SVV_TABLE_INFO.size counts 1 MB blocks; Redshift has no time-travel or fail-safe
    # retention, so only active bytes are selected (the parser reads absent columns as 0).
    assert "FROM svv_table_info" in sql
    assert "COALESCE(size, 0) * 1024 * 1024 AS active_bytes" in sql
    assert 'LOWER("database" || \'.\' || "schema" || \'.\' || "table")' in sql
    assert "time_travel_bytes" not in sql
    assert "failsafe_bytes" not in sql
