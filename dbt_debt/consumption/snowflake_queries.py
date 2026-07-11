"""Pure Snowflake `ACCOUNT_USAGE` / `INFORMATION_SCHEMA` SQL builders.

The Snowflake analogue of `jobs`: kept free of any Snowflake client so the query shape is
unit-testable with plain strings, and the row parsers in `jobs` (warehouse-neutral: they read
rows by key) are reused by the real client. Written from Snowflake's published ACCOUNT_USAGE
schemas, pinned by tests, and validated against a live Enterprise account. See DESIGN.md.

Usage comes from `ACCESS_HISTORY.direct_objects_accessed` (the metadata analogue of BigQuery's
`referenced_tables`), never from parsing `query_text`, since a silently unparseable query would
erase evidence of use and produce false "unused" verdicts. ACCESS_HISTORY requires Enterprise
edition
(and IMPORTED PRIVILEGES on the SNOWFLAKE database); the preflight fails loudly on Standard.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from dbt_debt.consumption.exclusion import validate_query_comment_pattern

_ACCESS_HISTORY = "SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY"
_QUERY_HISTORY = "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY"
_TABLES = "SNOWFLAKE.ACCOUNT_USAGE.TABLES"
_TABLE_STORAGE_METRICS = "SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS"

# Snowflake unquoted identifiers: letters, digits, underscore, dollar.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$]+$")


def exclusion_clause(query_comment_pattern: str, column: str = "query_text") -> str:
    """A SQL boolean that is true for rows whose query text is *not* a dbt query.

    Snowflake's `REGEXP_LIKE` implicitly anchors to the whole subject, so the unanchored
    `REGEXP_COUNT(...) = 0` expresses "does not contain" directly. The pattern is wrapped in a
    dollar-quoted string, Snowflake's no-escape literal, so its backslashes and quotes survive
    verbatim, the analogue of BigQuery's raw triple-quoted string.
    """

    validate_query_comment_pattern(query_comment_pattern)
    return f"REGEXP_COUNT({column}, $${query_comment_pattern}$$) = 0"


def permission_probe_query() -> str:
    """The preflight: touch ACCESS_HISTORY to prove account-wide history is readable at all."""

    return f"SELECT 1 FROM {_ACCESS_HISTORY} LIMIT 1"


def _user_select_filter(lookback_days: int, exclusion: str, prefix: str = "") -> str:
    """The shared `WHERE` predicate: successful user `SELECT`s within the window.

    Mirrors the BigQuery filter: window, statement type, success, and the dbt exclusion in one
    place. `prefix` qualifies the QUERY_HISTORY columns when the query joins other views.
    """

    return (
        f"{prefix}start_time >= DATEADD(day, -{int(lookback_days)}, CURRENT_TIMESTAMP())\n"
        f"  AND {prefix}query_type = 'SELECT'\n"
        f"  AND {prefix}execution_status = 'SUCCESS'\n"
        f"  AND {exclusion}"
    )


def table_usage_query(lookback_days: int, exclusion: str) -> str:
    """Count user queries per accessed relation over the lookback window.

    Each entry of `ACCESS_HISTORY.direct_objects_accessed` (one flattened row per relation a
    query touched) becomes one counted row, joined to QUERY_HISTORY for the SELECT-only /
    success / window / dbt-exclusion filters. `objectName` is already the fully qualified
    `DATABASE.SCHEMA.TABLE`, lowercased into the canonical relation_key. The caller builds
    `exclusion` against `qh.query_text`. `bytes_scanned` is the query's whole figure, so a
    query touching several tables attributes it to each, a ranking signal and not billing.
    """

    return f"""
SELECT
  LOWER(obj.value:"objectName"::STRING) AS relation_key,
  COUNT(*) AS query_count,
  MAX(qh.start_time) AS last_queried,
  COALESCE(SUM(qh.bytes_scanned), 0) AS bytes_scanned
FROM {_ACCESS_HISTORY} AS ah,
  LATERAL FLATTEN(input => ah.direct_objects_accessed) AS obj,
  {_QUERY_HISTORY} AS qh
WHERE qh.query_id = ah.query_id
  AND {_user_select_filter(lookback_days, exclusion, prefix="qh.")}
  AND obj.value:"objectDomain"::STRING IN ('Table', 'View', 'Materialized view')
GROUP BY relation_key
""".strip()


def query_text_query(lookback_days: int, exclusion: str) -> str:
    """Distinct user-query SQL over the window, for column-level usage parsing.

    Aliased to `query` so the warehouse-neutral row parsers in `jobs` read both warehouses'
    rows the same way. Grouping collapses identical statements so each distinct SQL is parsed
    once.
    """

    return f"""
SELECT query_text AS query
FROM {_QUERY_HISTORY}
WHERE {_user_select_filter(lookback_days, exclusion)}
  AND query_text IS NOT NULL
GROUP BY query_text
""".strip()


def first_seen_query() -> str:
    """The earliest creation of each relation ever recorded, for the too-new guard.

    `ACCOUNT_USAGE.TABLES` retains dropped incarnations (rows with `deleted` set), so
    `MIN(created)` over *all* rows survives dbt's `CREATE OR REPLACE` rebuilds, the same reason
    the BigQuery side reads JOBS rather than the live TABLES view, whose creation time resets on
    every rebuild. Deliberately unwindowed: the question is "when did this relation first
    exist", and ACCOUNT_USAGE already bounds its own retention.
    """

    return f"""
SELECT
  LOWER(table_catalog || '.' || table_schema || '.' || table_name) AS relation_key,
  MIN(created) AS first_seen
FROM {_TABLES}
GROUP BY relation_key
""".strip()


def table_storage_query() -> str:
    """Per-relation active, time-travel, and fail-safe bytes, for the storage-debt figures.

    `TABLE_STORAGE_METRICS` keeps one row per table incarnation, dropped ones included while
    their retained copies still bill, so the sums per relation_key cover everything the account
    pays for under that name: a dropped incarnation carries zero active bytes but real
    time-travel/fail-safe bytes until they expire. Covered by the same IMPORTED PRIVILEGES
    grant as the rest of ACCOUNT_USAGE, so no new permission.
    """

    return f"""
SELECT
  LOWER(table_catalog || '.' || table_schema || '.' || table_name) AS relation_key,
  COALESCE(SUM(active_bytes), 0) AS active_bytes,
  COALESCE(SUM(time_travel_bytes), 0) AS time_travel_bytes,
  COALESCE(SUM(failsafe_bytes), 0) AS failsafe_bytes
FROM {_TABLE_STORAGE_METRICS}
GROUP BY relation_key
""".strip()


def source_last_modified_query(datasets: Iterable[str]) -> str:
    """When each table in `datasets` (each a `database.schema`) last changed.

    Reads `ACCOUNT_USAGE.TABLES` (already required for first-seen, so no new grant), taking
    `MAX(last_altered)` over the live rows per relation. Documented caveat: `last_altered`
    also moves on DDL, so a table can look fresher than its data; the check under-reports
    staleness, never over-reports use. Comparison is on lowercased `database.schema` keys,
    matching the relation_key normalization; each identifier is validated against injection.
    """

    keys = sorted({key.lower() for key in datasets})
    for key in keys:
        database, _, schema = key.partition(".")
        for part in (database, schema):
            if not _IDENTIFIER_RE.match(part):
                raise ValueError(f"invalid Snowflake identifier: {part!r}")
    dataset_list = ", ".join(f"'{key}'" for key in keys)
    return f"""
SELECT
  LOWER(table_catalog || '.' || table_schema || '.' || table_name) AS relation_key,
  MAX(last_altered) AS last_modified
FROM {_TABLES}
WHERE deleted IS NULL
  AND LOWER(table_catalog || '.' || table_schema) IN ({dataset_list})
GROUP BY relation_key
""".strip()


def existing_relations_query(database: str, schemas: Iterable[str]) -> str:
    """All tables and views in `database` limited to `schemas`, for orphan discovery.

    Unlike BigQuery, one `INFORMATION_SCHEMA.TABLES` per database covers every schema, so a
    single filtered query replaces the per-dataset union. Comparison is on lowercased schema
    names, matching the relation_key normalization everywhere else. The database and each
    schema name are validated against injection.
    """

    if not _IDENTIFIER_RE.match(database):
        raise ValueError(f"invalid Snowflake database name: {database!r}")
    names = sorted({schema.lower() for schema in schemas})
    for name in names:
        if not _IDENTIFIER_RE.match(name):
            raise ValueError(f"invalid Snowflake schema name: {name!r}")
    schema_list = ", ".join(f"'{name}'" for name in names)
    return f"""
SELECT
  LOWER(table_catalog || '.' || table_schema || '.' || table_name) AS relation_key,
  table_type
FROM {database}.INFORMATION_SCHEMA.TABLES
WHERE LOWER(table_schema) IN ({schema_list})
""".strip()
