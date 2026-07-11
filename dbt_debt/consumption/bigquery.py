"""The BigQuery `WarehouseClient`, the only module that imports `google-cloud-bigquery`.

It composes the pure SQL builders/parsers in `jobs` with a live client. Imports are lazy so the
rest of the package (and the test suite, via the fake) loads without the BigQuery dependency or
any credentials.
"""

from __future__ import annotations

from collections.abc import Iterator, Set
from datetime import datetime
from typing import Any

from dbt_debt.config import Config
from dbt_debt.consumption import jobs
from dbt_debt.consumption.client import (
    MissingCredentialsError,
    MissingPermissionError,
    WarehouseError,
)
from dbt_debt.consumption.exclusion import exclusion_clause
from dbt_debt.domain import TableHygiene, TableStorage, UsageRow, WarehouseRelation


class RealBigQueryClient:
    """Live BigQuery client implementing the `WarehouseClient` Protocol."""

    def __init__(self, config: Config, project: str | None = None) -> None:
        from google.auth.exceptions import DefaultCredentialsError
        from google.cloud import bigquery

        self._config = config
        try:
            self._bq = bigquery.Client(project=project)
        except DefaultCredentialsError as exc:
            raise MissingCredentialsError(
                "No Google credentials found. Sign in with "
                "`gcloud auth application-default login` (dbt-debt uses the same credentials "
                "as dbt) and run the scan again."
            ) from exc

    def assert_usage_permission(self) -> None:
        """Confirm the caller can read all users' jobs by actually listing them.

        Listing jobs with `all_users=True` exercises `bigquery.jobs.listAll` directly: it raises
        `Forbidden` when the permission is missing. This is preferred over a `testIamPermissions`
        probe because it verifies the exact capability the usage query relies on without pulling
        in the Resource Manager dependency.
        """

        from google.api_core.exceptions import Forbidden, GoogleAPIError

        try:
            next(iter(self._bq.list_jobs(all_users=True, max_results=1)), None)
        except Forbidden as exc:
            raise MissingPermissionError(
                "Cannot read all users' BigQuery jobs (need bigquery.jobs.listAll, e.g. "
                "roles/bigquery.resourceViewer). Without it 'unused' counts only your own "
                "queries and would be false-confident."
            ) from exc
        except GoogleAPIError as exc:
            raise WarehouseError(f"BigQuery job listing failed: {exc}") from exc

    def table_usage(self) -> list[UsageRow]:
        clause = exclusion_clause(self._config.query_comment_pattern)
        sql = jobs.table_usage_query(self._config.region, self._config.lookback_days, clause)
        return jobs.parse_usage_rows(self._run(sql, "job history"))

    def query_texts(self) -> list[str]:
        clause = exclusion_clause(self._config.query_comment_pattern)
        sql = jobs.query_text_query(self._config.region, self._config.lookback_days, clause)
        return jobs.parse_query_text_rows(self._run(sql, "query text"))

    def relation_first_seen(self) -> dict[str, datetime]:
        sql = jobs.first_seen_query(self._config.region, self._config.lookback_days)
        return jobs.parse_first_seen_rows(self._run(sql, "relation ages"))

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        if not datasets:
            return []
        from google.api_core.exceptions import Forbidden

        sql = jobs.existing_relations_query(self._bq.project, datasets)
        try:
            rows = self._run(sql, "warehouse table listing")
        except Forbidden as exc:
            raise MissingPermissionError(
                "Cannot read table metadata for the managed datasets (need read access, e.g. "
                "roles/bigquery.metadataViewer or dataViewer on them). Orphaned-relation discovery "
                "is skipped; undeclared sources are still reported from the manifest."
            ) from exc
        return jobs.parse_relation_rows(rows)

    def table_storage(self) -> dict[str, TableStorage]:
        """Always empty: BigQuery has no billing-grade storage view readable with our grants
        (`TABLE_STORAGE` needs `bigquery.tables.list`), so sizes come from catalog.json."""

        return {}

    def table_hygiene(self) -> dict[str, TableHygiene]:
        """Always empty: BigQuery manages storage layout itself and exposes no maintenance
        columns; the CLI only calls this on Redshift."""

        return {}

    def source_last_modified(self, datasets: Set[str]) -> dict[str, datetime]:
        if not datasets:
            return {}
        from google.api_core.exceptions import Forbidden

        sql = jobs.source_last_modified_query(datasets)
        try:
            rows = self._run(sql, "source freshness")
        except Forbidden as exc:
            raise MissingPermissionError(
                "Cannot read table metadata for the source datasets (need read access, e.g. "
                "roles/bigquery.metadataViewer or dataViewer on them). The stale-source check "
                "is skipped; the rest of the scan is unaffected."
            ) from exc
        return jobs.parse_last_modified_rows(rows)

    def _run(self, sql: str, stage: str) -> Iterator[Any]:
        """Run one query, translating any API failure into a readable `WarehouseError`.

        `Forbidden` passes through untouched so the callers that give it a sharper meaning
        (the managed-dataset listing) can still catch it.
        """

        from google.api_core.exceptions import Forbidden, GoogleAPIError

        try:
            return iter(self._bq.query(sql).result())
        except Forbidden:
            raise
        except GoogleAPIError as exc:
            raise WarehouseError(
                f"BigQuery query for {stage} failed: {exc} — check --region and --project."
            ) from exc
