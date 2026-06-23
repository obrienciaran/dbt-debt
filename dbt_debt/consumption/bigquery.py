"""The real `BigQueryClient`, the only module that imports `google-cloud-bigquery`.

It composes the pure SQL builders/parsers in `jobs` with a live client. Imports are lazy so the
rest of the package (and the test suite, via the fake) loads without the BigQuery dependency or
any credentials.
"""

from __future__ import annotations

from collections.abc import Set

from dbt_debt.config import Config
from dbt_debt.consumption import jobs
from dbt_debt.consumption.client import MissingPermissionError
from dbt_debt.consumption.exclusion import exclusion_clause
from dbt_debt.domain import UsageRow, WarehouseRelation


class RealBigQueryClient:
    """Live BigQuery client implementing the `BigQueryClient` Protocol."""

    def __init__(self, config: Config, project: str | None = None) -> None:
        from google.cloud import bigquery

        self._config = config
        self._bq = bigquery.Client(project=project)

    def assert_usage_permission(self) -> None:
        """Confirm the caller can read all users' jobs by actually listing them.

        Listing jobs with `all_users=True` exercises `bigquery.jobs.listAll` directly: it raises
        `Forbidden` when the permission is missing. This is preferred over a `testIamPermissions`
        probe because it verifies the exact capability the usage query relies on without pulling
        in the Resource Manager dependency.
        """

        from google.api_core.exceptions import Forbidden

        try:
            next(iter(self._bq.list_jobs(all_users=True, max_results=1)), None)
        except Forbidden as exc:
            raise MissingPermissionError(
                "Cannot read all users' BigQuery jobs (need bigquery.jobs.listAll, e.g. "
                "roles/bigquery.resourceViewer). Without it 'unused' counts only your own "
                "queries and would be false-confident."
            ) from exc

    def table_usage(self) -> list[UsageRow]:
        clause = exclusion_clause(self._config.query_comment_pattern)
        sql = jobs.table_usage_query(self._config.region, self._config.lookback_days, clause)
        return jobs.parse_usage_rows(self._bq.query(sql).result())

    def query_texts(self) -> list[str]:
        clause = exclusion_clause(self._config.query_comment_pattern)
        sql = jobs.query_text_query(self._config.region, self._config.lookback_days, clause)
        return jobs.parse_query_text_rows(self._bq.query(sql).result())

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        if not datasets:
            return []
        from google.api_core.exceptions import Forbidden

        sql = jobs.existing_relations_query(self._bq.project, datasets)
        try:
            rows = self._bq.query(sql).result()
        except Forbidden as exc:
            raise MissingPermissionError(
                "Cannot read table metadata for the managed datasets (need read access, e.g. "
                "roles/bigquery.metadataViewer or dataViewer on them). Orphaned-relation discovery "
                "is skipped; undeclared sources are still reported from the manifest."
            ) from exc
        return jobs.parse_relation_rows(rows)
