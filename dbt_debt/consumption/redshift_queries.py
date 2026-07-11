"""Pure Redshift `SYS` / `SVV` SQL builders.

The Redshift analogue of `jobs` and `snowflake_queries`: kept free of any Redshift client so
the query shape is unit-testable with plain strings, and the row parsers in `jobs`
(warehouse-neutral: they read rows by key) are reused by the real client. Written from AWS's
published system-view schemas and validated live against a Serverless workgroup — see
DESIGN.md's Redshift section for what is confirmed and what remains open.

Usage comes from `SYS_QUERY_DETAIL` scan steps, whose `table_name` records each relation a
query physically read — the engine-metadata analogue of BigQuery's `referenced_tables` and
Snowflake's ACCESS_HISTORY — never from parsing `query_text`: a silently unparseable query
would erase evidence of use and produce false "unused" verdicts. A result-cache hit runs no
scan steps, so the usage join follows `result_cache_query_id` back to the originating query;
cached repeats still count as use. Both SYS views work on serverless workgroups and
provisioned clusters, but they only keep a bounded history (AWS documents seven days for the
older STL views and leaves SYS retention unstated), so the effective lookback window is capped
by whatever the account actually retains.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from dbt_debt.consumption.exclusion import validate_query_comment_pattern

_QUERY_HISTORY = "sys_query_history"
_QUERY_DETAIL = "sys_query_detail"
_REDSHIFT_TABLES = "svv_redshift_tables"
_TABLE_INFO = "svv_table_info"
_USER_INFO = "svv_user_info"

# Redshift unquoted identifiers: letters, digits, underscore, dollar.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$]+$")

# Steps on the optimizer's own temp tables and on system schemas are engine bookkeeping, not
# user consumption of a dbt relation; `table_name` is `database.schema.table`, so the schema
# is its second dot-part.
_USER_TABLE_FILTER = (
    "qd.table_name LIKE '%.%.%'\n"
    "  AND qd.table_name NOT LIKE '%volt_tt_%'\n"
    "  AND SPLIT_PART(qd.table_name, '.', 2) NOT IN "
    "('pg_catalog', 'pg_internal', 'information_schema')"
)


def exclusion_clause(query_comment_pattern: str, column: str = "query_text") -> str:
    """A SQL boolean that is true for rows whose query text is *not* a dbt query.

    Redshift supports both `REGEXP_COUNT` and dollar-quoted string literals, so this is the
    same "does not contain" form as Snowflake's: the pattern's backslashes and quotes survive
    verbatim inside `$$...$$`. `SYS_QUERY_HISTORY.query_text` is truncated at 4000 characters,
    which is harmless here — dbt's query-comment leads the statement.
    """

    validate_query_comment_pattern(query_comment_pattern)
    return f"REGEXP_COUNT({column}, $${query_comment_pattern}$$) = 0"


def permission_probe_query() -> str:
    """The preflight: prove the caller sees *all* users' rows in the SYS views.

    Redshift lets every user select from SYS_QUERY_HISTORY but silently filters it to their
    own queries unless they are a superuser or hold SYSLOG ACCESS UNRESTRICTED — the failure
    mode where "unused" would mean "unused by me". The probe returns a row only when the
    current user has one of those, so the client treats an empty result as a missing
    permission, not a passed check.
    """

    return f"""
SELECT 1
FROM {_USER_INFO}
WHERE user_name = current_user
  AND (superuser OR syslog_access = 'UNRESTRICTED')
""".strip()


def _user_select_filter(lookback_days: int, exclusion: str, prefix: str = "") -> str:
    """The shared `WHERE` predicate: successful user `SELECT`s within the window.

    Mirrors the BigQuery and Snowflake filters: window, statement type, success, and the dbt
    exclusion in one place. `prefix` qualifies the SYS_QUERY_HISTORY columns when the query
    joins other views.
    """

    return (
        f"{prefix}start_time >= DATEADD(day, -{int(lookback_days)}, GETDATE())\n"
        f"  AND {prefix}query_type = 'SELECT'\n"
        f"  AND {prefix}status = 'success'\n"
        f"  AND {exclusion}"
    )


def table_usage_query(lookback_days: int, exclusion: str) -> str:
    """Count user queries per scanned relation over the lookback window.

    Each scan step in `SYS_QUERY_DETAIL` names the relation it read as a fully qualified
    `database.schema.table`, lowercased into the canonical relation_key; the join to
    SYS_QUERY_HISTORY applies the SELECT-only / success / window / dbt-exclusion filters. The
    join key follows `result_cache_query_id` back to the originating query so result-cache
    hits (which run no scan steps of their own) still count as use of the tables the original
    query read. The caller builds `exclusion` against `qh.query_text`. Unlike BigQuery and
    Snowflake, `bytes_scanned` here is per table (the scan steps' output bytes), and a cached
    repeat re-attributes the originating scan's bytes — a ranking signal, not billing.
    """

    return f"""
SELECT
  LOWER(qd.table_name) AS relation_key,
  COUNT(DISTINCT qh.query_id) AS query_count,
  MAX(qh.start_time) AS last_queried,
  COALESCE(SUM(qd.output_bytes), 0) AS bytes_scanned
FROM {_QUERY_HISTORY} AS qh
JOIN {_QUERY_DETAIL} AS qd
  ON qd.query_id = COALESCE(NULLIF(qh.result_cache_query_id, 0), qh.query_id)
WHERE qd.step_name = 'scan'
  AND {_USER_TABLE_FILTER}
  AND {_user_select_filter(lookback_days, exclusion, prefix="qh.")}
GROUP BY relation_key
""".strip()


def query_text_query(lookback_days: int, exclusion: str) -> str:
    """Distinct user-query SQL over the window, for column-level usage parsing.

    Aliased to `query` so the warehouse-neutral row parsers in `jobs` read all warehouses'
    rows the same way. Grouping collapses identical statements so each distinct SQL is parsed
    once. The 4000-character truncation of `query_text` can only add parse failures, which
    feed the column report's confidence statement and never touch usage verdicts.
    """

    return f"""
SELECT query_text AS query
FROM {_QUERY_HISTORY}
WHERE {_user_select_filter(lookback_days, exclusion)}
  AND query_text IS NOT NULL
GROUP BY query_text
""".strip()


def first_seen_query() -> str:
    """The earliest query that touched each relation on record, for the too-new guard.

    Deliberately unfiltered — every statement type and step, dbt's own builds included —
    because the question is "when did this relation first exist", not "who used it". Reads
    the job history rather than table metadata (`SVV_TABLE_INFO.create_time` resets on every
    dbt rebuild, like BigQuery's `TABLES.creation_time`). Deliberately unwindowed: the SYS
    views bound their own retention, and that retention caps how far back first-seen can
    reach — a documented Redshift caveat.

    dbt-redshift builds each table as `<name>__dbt_tmp` and renames it into place, and the
    rename is DDL that SYS_QUERY_DETAIL records under no name (confirmed live) — so a
    dbt-built relation's only history rows carry the tmp name, and without folding that
    suffix back the final table would have no first-seen row and the too-new guard would
    never protect it.
    """

    return f"""
SELECT
  REGEXP_REPLACE(LOWER(qd.table_name), '__dbt_tmp$', '') AS relation_key,
  MIN(qh.start_time) AS first_seen
FROM {_QUERY_HISTORY} AS qh
JOIN {_QUERY_DETAIL} AS qd ON qd.query_id = qh.query_id
WHERE {_USER_TABLE_FILTER}
GROUP BY relation_key
""".strip()


def table_storage_query() -> str:
    """Per-relation active bytes, for the storage-debt figures.

    `SVV_TABLE_INFO.size` counts 1 MB blocks, converted to bytes here. Redshift has no
    time-travel or fail-safe retention, so those fields are simply absent and parse as zero.
    Known limit: SVV_TABLE_INFO omits empty tables, whose catalog size (or blank) then stands.
    """

    return f"""
SELECT
  LOWER("database" || '.' || "schema" || '.' || "table") AS relation_key,
  COALESCE(size, 0) * 1024 * 1024 AS active_bytes
FROM {_TABLE_INFO}
""".strip()


def existing_relations_query(database: str, schemas: Iterable[str]) -> str:
    """All tables and views in `database` limited to `schemas`, for orphan discovery.

    `SVV_REDSHIFT_TABLES` covers every schema in one query (SVV_TABLE_INFO would be wrong
    here: it omits empty tables, and an empty leftover is still an orphan). Comparison is on
    lowercased names, matching the relation_key normalization everywhere else. The database
    and each schema name are validated against injection.
    """

    if not _IDENTIFIER_RE.match(database):
        raise ValueError(f"invalid Redshift database name: {database!r}")
    names = sorted({schema.lower() for schema in schemas})
    for name in names:
        if not _IDENTIFIER_RE.match(name):
            raise ValueError(f"invalid Redshift schema name: {name!r}")
    schema_list = ", ".join(f"'{name}'" for name in names)
    return f"""
SELECT
  LOWER(database_name || '.' || schema_name || '.' || table_name) AS relation_key,
  table_type
FROM {_REDSHIFT_TABLES}
WHERE LOWER(database_name) = '{database.lower()}'
  AND LOWER(schema_name) IN ({schema_list})
""".strip()
