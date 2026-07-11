"""Tests for the pure INFORMATION_SCHEMA SQL builders, parsers, and dbt exclusion."""

from __future__ import annotations

from datetime import datetime

import pytest

from dbt_debt.config import DEFAULT_QUERY_COMMENT_PATTERN
from dbt_debt.consumption.exclusion import exclusion_clause
from dbt_debt.consumption.jobs import (
    existing_relations_query,
    first_seen_query,
    parse_first_seen_rows,
    parse_last_modified_rows,
    source_last_modified_query,
    parse_query_text_rows,
    parse_relation_rows,
    parse_table_hygiene_rows,
    parse_table_storage_rows,
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
    assert "COALESCE(SUM(total_bytes_processed), 0) AS bytes_scanned" in sql


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
    rows = [
        {"relation_key": "P.D.T", "query_count": 5, "last_queried": when, "bytes_scanned": 2048}
    ]
    parsed = parse_usage_rows(rows)
    assert parsed[0].relation_key == "p.d.t"
    assert parsed[0].query_count == 5
    assert parsed[0].last_queried == when
    assert parsed[0].bytes_scanned == 2048


def test_parse_usage_rows_tolerates_absent_or_null_bytes() -> None:
    # Rows cached before the bytes column existed have no key; a NULL sum can also slip
    # through. Either reads as 0 rather than failing the scan.
    when = datetime(2026, 6, 1)
    rows = [
        {"relation_key": "p.d.old", "query_count": 1, "last_queried": when},
        {"relation_key": "p.d.null", "query_count": 1, "last_queried": when, "bytes_scanned": None},
    ]
    assert [row.bytes_scanned for row in parse_usage_rows(rows)] == [0, 0]


def test_parse_table_storage_rows_lowercases_keys_and_reads_nulls_as_zero() -> None:
    from dbt_debt.domain import TableStorage

    rows = [
        {"relation_key": "DB.S.T", "active_bytes": 10, "time_travel_bytes": 5, "failsafe_bytes": 2},
        {"relation_key": "db.s.dropped", "active_bytes": None, "time_travel_bytes": None},
    ]
    assert parse_table_storage_rows(rows) == {
        "db.s.t": TableStorage(active_bytes=10, time_travel_bytes=5, failsafe_bytes=2),
        "db.s.dropped": TableStorage(active_bytes=0, time_travel_bytes=0, failsafe_bytes=0),
    }


def test_parse_table_hygiene_rows_lowercases_keys_and_reads_nulls_as_zero() -> None:
    from dbt_debt.domain import TableHygiene

    rows = [
        {
            "relation_key": "DB.S.T",
            "unsorted_percent": 42.5,
            "stats_off_percent": 10,
            "skew_rows": 4.2,
            "total_rows": 100,
            "active_bytes": 1024,
        },
        {"relation_key": "db.s.bare", "unsorted_percent": None},
    ]
    assert parse_table_hygiene_rows(rows) == {
        "db.s.t": TableHygiene(
            unsorted_percent=42.5,
            stats_off_percent=10,
            skew_rows=4.2,
            total_rows=100,
            active_bytes=1024,
        ),
        "db.s.bare": TableHygiene(
            unsorted_percent=0, stats_off_percent=0, skew_rows=0, total_rows=0, active_bytes=0
        ),
    }


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


def test_first_seen_query_shape() -> None:
    sql = first_seen_query("US", 90)
    # Every statement type and dbt's own builds count — the question is when the relation
    # first existed, not who used it — so no SELECT filter and no dbt exclusion.
    assert "statement_type" not in sql
    assert "REGEXP_CONTAINS" not in sql
    assert "MIN(creation_time) AS first_seen" in sql
    assert "UNNEST(referenced_tables)" in sql
    assert "destination_table.table_id IS NOT NULL" in sql
    assert "UNION ALL" in sql
    assert sql.count("INTERVAL 90 DAY") == 2


def test_first_seen_query_rejects_bad_region() -> None:
    with pytest.raises(ValueError):
        first_seen_query("US; DROP", 90)


def test_parse_first_seen_rows() -> None:
    when = datetime(2026, 6, 1)
    parsed = parse_first_seen_rows([{"relation_key": "P.D.T", "first_seen": when}])
    assert parsed == {"p.d.t": when}


def test_parse_relation_rows() -> None:
    parsed = parse_relation_rows([{"relation_key": "P.D.T", "table_type": "BASE TABLE"}])
    assert parsed[0].relation_key == "p.d.t"
    assert parsed[0].relation_type == "BASE TABLE"


def test_source_last_modified_query_unions_the_datasets() -> None:
    sql = source_last_modified_query({"proj-a.raw", "proj-b.landing_zone"})
    assert "FROM `proj-a`.`raw`.__TABLES__" in sql
    assert "FROM `proj-b`.`landing_zone`.__TABLES__" in sql
    assert "TIMESTAMP_MILLIS(last_modified_time) AS last_modified" in sql
    assert sql.count("UNION ALL") == 1


def test_source_last_modified_query_validates_identifiers() -> None:
    with pytest.raises(ValueError):
        source_last_modified_query({"bad;proj.raw"})
    with pytest.raises(ValueError):
        source_last_modified_query({"proj.bad-dataset"})


def test_parse_last_modified_rows_lowercases_keys() -> None:
    when = datetime(2026, 6, 1)
    rows = [{"relation_key": "P.RAW.Orders", "last_modified": when}]
    assert parse_last_modified_rows(rows) == {"p.raw.orders": when}
