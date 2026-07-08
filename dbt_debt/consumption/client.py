"""The warehouse seam: a Protocol so the engine never imports a BigQuery client directly.

Methods return already-parsed domain values (not raw rows) so a `FakeBigQueryClient` can supply
canned data in tests with no network and no credentials. The real implementation lives in
`bigquery`; nothing else in the package imports `google-cloud-bigquery`.
"""

from __future__ import annotations

from collections.abc import Set
from typing import Protocol, runtime_checkable

from dbt_debt.domain import UsageRow, WarehouseRelation


class MissingCredentialsError(RuntimeError):
    """Raised when no Google credentials can be found at all.

    Distinct from `MissingPermissionError`: here the caller is not signed in, rather than signed
    in without the required grant. The CLI turns both into a friendly message and a clean exit.
    """


class MissingPermissionError(RuntimeError):
    """Raised when the caller cannot see every user's jobs.

    Without `bigquery.jobs.listAll` a caller silently sees only their own jobs, so every
    "unused" verdict would be false-confident. The client preflights this and fails loudly
    rather than reporting a valid-looking but partial result.
    """


@runtime_checkable
class BigQueryClient(Protocol):
    """What the engine needs from the warehouse, narrowed to parsed results."""

    def assert_usage_permission(self) -> None:
        """Verify the caller can read all users' jobs; raise `MissingPermissionError` if not."""
        ...

    def table_usage(self) -> list[UsageRow]:
        """Relations referenced by user queries within the lookback window (dbt excluded)."""
        ...

    def query_texts(self) -> list[str]:
        """Distinct user-query SQL within the window, for column-level usage parsing."""
        ...

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        """Tables and views physically present in `datasets`, for orphan discovery.

        Raises `MissingPermissionError` when the warehouse metadata cannot be listed (the caller
        needs `bigquery.tables.list`, e.g. `roles/bigquery.metadataViewer`); returns an empty list
        when `datasets` is empty.
        """
        ...
