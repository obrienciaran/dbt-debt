"""Pure `INFORMATION_SCHEMA` SQL builders and row parsers.

Kept free of any BigQuery client so the query shape and parsing are unit-testable with plain
dicts. The real client (`bigquery`) builds the SQL here, runs it, and feeds the rows back to
these parsers. Rows are read by key, so both `google.cloud.bigquery.Row` and dicts work.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from dbt_debt.domain import UsageRow, WarehouseRelation

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

    Both JOBS queries count the same thing — non-dbt `SELECT`s a human or BI tool ran — so the
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
    """

    return f"""
SELECT
  LOWER(CONCAT(ref.project_id, '.', ref.dataset_id, '.', ref.table_id)) AS relation_key,
  COUNT(*) AS query_count,
  MAX(creation_time) AS last_queried
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


def existing_relations_query(project: str, datasets: Iterable[str]) -> str:
    """All base tables and views in `datasets`, for orphan discovery.

    Reads each managed dataset's own `INFORMATION_SCHEMA.TABLES` (dataset-qualified) and unions
    them, rather than the region-wide view. The dataset-scoped view needs only read access to that
    dataset — which a dbt user already has — whereas the region-wide view needs a project-level
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


def parse_usage_rows(rows: Iterable[Mapping[str, Any]]) -> list[UsageRow]:
    """Parse usage query rows into `UsageRow` value objects."""

    return [
        UsageRow(
            relation_key=str(row["relation_key"]).lower(),
            query_count=int(row["query_count"]),
            last_queried=row["last_queried"],
        )
        for row in rows
    ]
