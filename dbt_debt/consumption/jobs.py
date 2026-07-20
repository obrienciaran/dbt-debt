"""Pure BigQuery `INFORMATION_SCHEMA` SQL builders and warehouse-neutral row parsers.

Kept free of any BigQuery client so the query shape and parsing are unit-testable with plain
dicts. The real client (`bigquery`) builds the SQL here, runs it, and feeds the rows back to
these parsers. Rows are read by key, so both `google.cloud.bigquery.Row` and dicts work, which
also makes the parsers warehouse-neutral, so the Snowflake client can feed them rows from the
queries in `snowflake_queries`, whose result columns carry the same names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from dbt_debt.domain import TableHygiene, TableStorage, UsageRow, WarehouseRelation

_REGION_RE = re.compile(r"^[A-Za-z0-9-]+$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9-]+$")
_DATASET_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _region_dataset(region: str) -> str:
    """`region-<region>` qualifier for INFORMATION_SCHEMA, validated against injection."""

    if not _REGION_RE.match(region):
        raise ValueError(f"invalid BigQuery region: {region!r}")
    return f"`region-{region.lower()}`"


def _user_select_filter(lookback_days: int, exclusion_clause: str) -> str:
    """The shared `WHERE` predicate: completed, error-free user `SELECT`s within the window.

    Both JOBS queries count the same thing (non-dbt `SELECT`s a human or BI tool ran), so the
    window, statement-type, success, and dbt-exclusion filters live here in one place.
    """

    return (
        f"creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(lookback_days)} DAY)\n"
        "  AND statement_type = 'SELECT'\n"
        "  AND state = 'DONE'\n"
        "  AND error_result IS NULL\n"
        f"  AND {exclusion_clause}"
    )


def table_usage_query(region: str, lookback_days: int, exclusion_clause: str) -> str:
    """Count user queries per referenced relation over the lookback window.

    `JOBS_BY_PROJECT` requires `bigquery.jobs.listAll` to see all users' jobs (preflighted
    elsewhere). Each `referenced_tables` entry a job touched becomes one counted row; dbt's own
    queries are removed by `exclusion_clause`, and only completed, error-free `SELECT`s count.
    `total_bytes_processed` is the job's whole figure, so a job referencing several tables
    attributes it to each, good enough for ranking but not for exact billing.
    """

    return f"""
SELECT
  LOWER(CONCAT(ref.project_id, '.', ref.dataset_id, '.', ref.table_id)) AS relation_key,
  COUNT(*) AS query_count,
  MAX(creation_time) AS last_queried,
  COALESCE(SUM(total_bytes_processed), 0) AS bytes_scanned
FROM {_region_dataset(region)}.INFORMATION_SCHEMA.JOBS_BY_PROJECT,
  UNNEST(referenced_tables) AS ref
WHERE {_user_select_filter(lookback_days, exclusion_clause)}
GROUP BY relation_key
""".strip()


def query_text_query(region: str, lookback_days: int, exclusion_clause: str) -> str:
    """Distinct user-query SQL over the window, for column-level usage parsing.

    Grouping by `query` collapses identical statements so each distinct SQL is parsed once.
    Same window and dbt exclusion as the table-usage query.
    """

    return f"""
SELECT query
FROM {_region_dataset(region)}.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE {_user_select_filter(lookback_days, exclusion_clause)}
  AND query IS NOT NULL
GROUP BY query
""".strip()


def first_seen_query(region: str, lookback_days: int) -> str:
    """The earliest job that touched each relation in the window, for the too-new guard.

    Deliberately unfiltered (every statement type, dbt's own builds included) because the
    question is "when did this relation first exist", not "who used it". An old model rebuilt
    nightly then has builds throughout the window, while a model created mid-window first
    appears mid-window. Both `referenced_tables` and `destination_table` are unioned in, since
    a CTAS/dbt build reliably records the created table in `destination_table` (its presence
    in `referenced_tables` is not guaranteed).
    """

    window = (
        f"creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(lookback_days)} DAY)"
    )
    return f"""
SELECT relation_key, MIN(creation_time) AS first_seen
FROM (
  SELECT
    LOWER(CONCAT(ref.project_id, '.', ref.dataset_id, '.', ref.table_id)) AS relation_key,
    creation_time
  FROM {_region_dataset(region)}.INFORMATION_SCHEMA.JOBS_BY_PROJECT,
    UNNEST(referenced_tables) AS ref
  WHERE {window}
  UNION ALL
  SELECT
    LOWER(CONCAT(destination_table.project_id, '.', destination_table.dataset_id, '.',
      destination_table.table_id)) AS relation_key,
    creation_time
  FROM {_region_dataset(region)}.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE {window}
    AND destination_table.table_id IS NOT NULL
)
GROUP BY relation_key
""".strip()


def existing_relations_query(project: str, datasets: Iterable[str]) -> str:
    """All base tables and views in `datasets`, for orphan discovery.

    Reads each managed dataset's own `INFORMATION_SCHEMA.TABLES` (dataset-qualified) and unions
    them, rather than the region-wide view. The dataset-scoped view needs only read access to that
    dataset, which a dbt user already has, whereas the region-wide view needs a project-level
    grant that even an Owner can be refused. The project and each dataset name are validated
    against injection; dataset case is preserved because BigQuery dataset ids are case-sensitive.
    """

    if not _PROJECT_RE.match(project):
        raise ValueError(f"invalid GCP project id: {project!r}")
    names = sorted(set(datasets))
    for name in names:
        if not _DATASET_RE.match(name):
            raise ValueError(f"invalid BigQuery dataset name: {name!r}")
    selects = [
        "SELECT\n"
        "  LOWER(CONCAT(table_catalog, '.', table_schema, '.', table_name)) AS relation_key,\n"
        "  table_type\n"
        f"FROM `{project}`.`{name}`.INFORMATION_SCHEMA.TABLES"
        for name in names
    ]
    return "\nUNION ALL\n".join(selects)


def source_last_modified_query(datasets: Iterable[str]) -> str:
    """When each table in `datasets` (each a `project.dataset`) last received data.

    Reads each dataset's legacy `__TABLES__` metadata table, whose `last_modified_time`
    (epoch milliseconds) is updated by loads and streaming writes alike, and which needs only
    read access to that dataset, the same optional grant as orphan discovery. Source datasets
    can live in other GCP projects, hence the project-qualified keys. (Inference to confirm
    live: `__TABLES__` is a legacy surface, readable from standard SQL.)
    """

    pairs = sorted({_split_dataset_key(key) for key in datasets})
    selects = [
        "SELECT\n"
        "  LOWER(CONCAT(project_id, '.', dataset_id, '.', table_id)) AS relation_key,\n"
        "  TIMESTAMP_MILLIS(last_modified_time) AS last_modified\n"
        f"FROM `{project}`.`{dataset}`.__TABLES__"
        for project, dataset in pairs
    ]
    return "\nUNION ALL\n".join(selects)


def _split_dataset_key(key: str) -> tuple[str, str]:
    """Split a `project.dataset` key, validating both parts against injection."""

    project, _, dataset = key.partition(".")
    if not _PROJECT_RE.match(project):
        raise ValueError(f"invalid GCP project id: {project!r}")
    if not _DATASET_RE.match(dataset):
        raise ValueError(f"invalid BigQuery dataset name: {dataset!r}")
    return project, dataset


def as_utc(value: Any) -> Any:
    """Stamp UTC on naive datetimes; everything else passes through.

    Redshift SYS views report timestamps in UTC but as ``timestamp without time zone``, so the
    driver hands back naive datetimes (confirmed live). Databricks validation returned aware
    values, but the connector makes no guarantee, so the same normalization is applied
    defensively. Verdicts compare against aware ``now`` values, so the zone is restored at the
    client boundary that knows it may have been dropped.
    """

    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def parse_query_text_rows(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Parse query-text rows into a list of SQL strings."""

    return [str(row["query"]) for row in rows]


def parse_relation_rows(rows: Iterable[Mapping[str, Any]]) -> list[WarehouseRelation]:
    """Parse INFORMATION_SCHEMA.TABLES rows into `WarehouseRelation` value objects."""

    return [
        WarehouseRelation(
            relation_key=str(row["relation_key"]).lower(),
            relation_type=str(row["table_type"]),
        )
        for row in rows
    ]


def parse_first_seen_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, datetime]:
    """Parse first-seen rows into a relation_key -> earliest job timestamp map."""

    return {str(row["relation_key"]).lower(): row["first_seen"] for row in rows}


def parse_last_modified_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, datetime]:
    """Parse last-modified rows into a relation_key -> last data change map."""

    return {str(row["relation_key"]).lower(): row["last_modified"] for row in rows}


def parse_table_storage_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, TableStorage]:
    """Parse storage-metrics rows into a relation_key -> `TableStorage` map (NULL bytes read as 0)."""

    return {
        str(row["relation_key"]).lower(): TableStorage(
            active_bytes=int(row.get("active_bytes") or 0),
            time_travel_bytes=int(row.get("time_travel_bytes") or 0),
            failsafe_bytes=int(row.get("failsafe_bytes") or 0),
        )
        for row in rows
    }


def parse_table_hygiene_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, TableHygiene]:
    """Parse hygiene rows into a relation_key -> `TableHygiene` map (NULL or absent values read as 0)."""

    return {
        str(row["relation_key"]).lower(): TableHygiene(
            unsorted_percent=float(row.get("unsorted_percent") or 0),
            stats_off_percent=float(row.get("stats_off_percent") or 0),
            skew_rows=float(row.get("skew_rows") or 0),
            total_rows=int(row.get("total_rows") or 0),
            active_bytes=int(row.get("active_bytes") or 0),
        )
        for row in rows
    }


def parse_usage_rows(rows: Iterable[Mapping[str, Any]]) -> list[UsageRow]:
    """Parse usage query rows into `UsageRow` value objects.

    `bytes_scanned` tolerates an absent or NULL column so cached rows written before it
    existed still parse (as 0) rather than failing the scan.
    """

    return [
        UsageRow(
            relation_key=str(row["relation_key"]).lower(),
            query_count=int(row["query_count"]),
            last_queried=row["last_queried"],
            bytes_scanned=int(row.get("bytes_scanned") or 0),
        )
        for row in rows
    ]
