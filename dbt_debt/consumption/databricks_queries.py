"""Pure Databricks system-table SQL builders.

Usage is deliberately conservative. Lineage with a ``statement_id`` is joined to successful
query history so dbt statements can be excluded exactly. Although Databricks documents that ID
for SQL warehouses, some serverless events expose a joinable ID too. An unjoinable source-only
event therefore counts as usage, while an unjoinable event with a target is omitted as probable
build lineage. False activity is safer than a false "unused" verdict.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from dbt_debt.consumption.exclusion import validate_query_comment_pattern

_TABLE_LINEAGE = "system.access.table_lineage"
_QUERY_HISTORY = "system.query.history"
_TABLES = "system.information_schema.tables"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


def exclusion_clause(query_comment_pattern: str, column: str = "statement_text") -> str:
    """A predicate excluding dbt query comments from joined query-history rows."""

    validate_query_comment_pattern(query_comment_pattern)
    if "'" in query_comment_pattern:
        raise ValueError(
            "--query-comment-pattern must not contain a single quote for Databricks SQL."
        )
    # Customer-managed keys can make statement_text unavailable. Unknown text must count as
    # usage (possibly preserving a dbt read) rather than erase a legitimate user read.
    return f"COALESCE(NOT REGEXP_LIKE({column}, r'{query_comment_pattern}'), TRUE)"


def permission_probe_query() -> str:
    """Touch both required system schemas; either missing grant must fail the preflight."""

    return f"""
WITH access_probe AS (
  SELECT statement_id FROM {_TABLE_LINEAGE} LIMIT 1
),
query_probe AS (
  SELECT statement_id FROM {_QUERY_HISTORY} LIMIT 1
)
SELECT statement_id FROM access_probe
UNION ALL
SELECT statement_id FROM query_probe
""".strip()


def table_usage_query(lookback_days: int, exclusion: str) -> str:
    """Count joined reads plus safely identified unjoinable source-only lineage.

    ``cache_origin_statement_id`` maps result-cache repeats to the originating statement's
    lineage. The history-presence join is intentionally broader than the eligible-use filter:
    a joined failed, non-SELECT, or dbt statement must not fall through into the conservative
    unjoinable branch.
    """

    return f"""
WITH lineage AS (
  SELECT
    LOWER(source_table_full_name) AS relation_key,
    target_table_full_name,
    target_path,
    event_id,
    statement_id,
    event_time
  FROM {_TABLE_LINEAGE}
  WHERE event_date >= CURRENT_DATE() - INTERVAL {int(lookback_days)} DAYS
    AND source_table_full_name IS NOT NULL
),
history AS (
  SELECT
    statement_id,
    cache_origin_statement_id,
    statement_type,
    execution_status,
    statement_text,
    start_time,
    COALESCE(read_bytes, 0) AS read_bytes
  FROM {_QUERY_HISTORY}
  WHERE start_time >= CURRENT_TIMESTAMP() - INTERVAL {int(lookback_days)} DAYS
),
joined_usage AS (
  SELECT DISTINCT
    l.relation_key,
    h.statement_id AS usage_id,
    h.start_time AS last_queried,
    h.read_bytes AS bytes_scanned
  FROM lineage AS l
  JOIN history AS h
    ON h.cache_origin_statement_id = l.statement_id
  WHERE h.statement_type = 'SELECT'
    AND h.execution_status = 'FINISHED'
    AND {exclusion}
),
unjoinable_read_usage AS (
  SELECT DISTINCT
    l.relation_key,
    CONCAT('lineage:', l.event_id) AS usage_id,
    l.event_time AS last_queried,
    0 AS bytes_scanned
  FROM lineage AS l
  LEFT JOIN history AS h
    ON h.statement_id = l.statement_id
  WHERE h.statement_id IS NULL
    AND l.target_table_full_name IS NULL
    AND l.target_path IS NULL
),
usage AS (
  SELECT * FROM joined_usage
  UNION ALL
  SELECT * FROM unjoinable_read_usage
)
SELECT
  relation_key,
  COUNT(DISTINCT usage_id) AS query_count,
  MAX(last_queried) AS last_queried,
  COALESCE(SUM(bytes_scanned), 0) AS bytes_scanned
FROM usage
GROUP BY relation_key
""".strip()


def query_text_query(lookback_days: int, exclusion: str) -> str:
    """Successful non-dbt SELECT text for a future Databricks ``--columns`` mode.

    Column analysis is disabled until complete query-text or column-lineage coverage is proven
    across supported compute paths (tracked as a GitHub issue).
    """

    return f"""
SELECT statement_text AS query
FROM {_QUERY_HISTORY}
WHERE start_time >= CURRENT_TIMESTAMP() - INTERVAL {int(lookback_days)} DAYS
  AND statement_type = 'SELECT'
  AND execution_status = 'FINISHED'
  AND statement_text IS NOT NULL
  AND {exclusion}
GROUP BY statement_text
""".strip()


def first_seen_query() -> str:
    """Earliest retained lineage event, never Unity Catalog's resettable ``created`` value."""

    return f"""
SELECT relation_key, MIN(event_time) AS first_seen
FROM (
  SELECT LOWER(source_table_full_name) AS relation_key, event_time
  FROM {_TABLE_LINEAGE}
  WHERE source_table_full_name IS NOT NULL
  UNION ALL
  SELECT LOWER(target_table_full_name) AS relation_key, event_time
  FROM {_TABLE_LINEAGE}
  WHERE target_table_full_name IS NOT NULL
)
GROUP BY relation_key
""".strip()


def existing_relations_query(datasets: Iterable[str]) -> str:
    """Inventory tables and views in managed ``catalog.schema`` pairs."""

    keys = sorted({_validate_dataset_key(key) for key in datasets})
    dataset_list = ", ".join(f"'{key}'" for key in keys)
    return f"""
SELECT
  LOWER(table_catalog || '.' || table_schema || '.' || table_name) AS relation_key,
  table_type
FROM {_TABLES}
WHERE LOWER(table_catalog || '.' || table_schema) IN ({dataset_list})
""".strip()


def _validate_dataset_key(key: str) -> str:
    catalog, separator, schema = key.partition(".")
    if not separator or not _IDENTIFIER_RE.fullmatch(catalog):
        raise ValueError(f"invalid Databricks catalog name: {catalog!r}")
    if not _IDENTIFIER_RE.fullmatch(schema):
        raise ValueError(f"invalid Databricks schema name: {schema!r}")
    return f"{catalog.lower()}.{schema.lower()}"
