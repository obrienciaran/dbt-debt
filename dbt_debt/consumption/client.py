"""The warehouse seam: a Protocol so the engine never imports a warehouse SDK directly.

Methods return already-parsed domain values (not raw rows) so a `FakeWarehouseClient` can supply
canned data in tests with no network and no credentials. Each real implementation lives in its
own module (`bigquery`, `snowflake`); nothing else in the package imports a warehouse SDK.
"""

from __future__ import annotations

from collections.abc import Set
from datetime import datetime
from typing import Protocol, runtime_checkable

from dbt_debt.domain import TableHygiene, TableStorage, UsageRow, WarehouseRelation


class WarehouseError(RuntimeError):
    """Raised when a warehouse call fails for any reason the scan cannot recover from.

    The base of the warehouse error family: the CLI catches this one type and exits with the
    warehouse status code, so a mid-scan API failure (bad region, transient outage, quota) ends
    with a readable message instead of a traceback.
    """


class MissingCredentialsError(WarehouseError):
    """Raised when no warehouse credentials can be found at all.

    Distinct from `MissingPermissionError`: here the caller is not signed in, rather than signed
    in without the required grant. The CLI turns both into a friendly message and a clean exit.
    """


class MissingPermissionError(WarehouseError):
    """Raised when the caller cannot see every user's query history.

    Without account-wide history (`bigquery.jobs.listAll`; Snowflake's ACCOUNT_USAGE share) a
    caller silently sees only their own queries, so every "unused" verdict would be
    false-confident. Each client preflights this and fails loudly rather than reporting a
    valid-looking but partial result.
    """


class InvalidIdentifierError(WarehouseError):
    """Raised when a manifest identifier cannot be safely placed in a metadata query.

    The orphan and stale-source builders reject a dataset, schema, or database name that is
    not a valid warehouse identifier (the injection guard). A client translates that rejection
    into this error so the CLI degrades the optional check with a message, the same as a
    missing grant, rather than letting the builder's `ValueError` escape as a traceback. It is
    a `WarehouseError`, so the top-level handler is a safe net if a caller ever forgets to
    degrade.
    """


@runtime_checkable
class WarehouseClient(Protocol):
    """What the engine needs from the warehouse, narrowed to parsed results."""

    def assert_usage_permission(self) -> None:
        """Verify the caller can read all users' queries; raise `MissingPermissionError` if not."""
        ...

    def table_usage(self) -> list[UsageRow]:
        """Relations referenced by user queries within the lookback window (dbt excluded)."""
        ...

    def query_texts(self) -> list[str]:
        """Distinct user-query SQL within the window, for column-level usage parsing."""
        ...

    def relation_first_seen(self) -> dict[str, datetime]:
        """Earliest job per relation in the window (all statement types), for the too-new guard."""
        ...

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        """Tables and views physically present in `datasets`, for orphan discovery.

        Raises `MissingPermissionError` when the warehouse metadata cannot be listed (on
        BigQuery the caller needs `bigquery.tables.list`, e.g. `roles/bigquery.metadataViewer`);
        returns an empty list when `datasets` is empty.
        """
        ...

    def table_storage(self) -> dict[str, TableStorage]:
        """relation_key -> live active/time-travel/fail-safe bytes, for storage-debt figures.

        Snowflake reads `ACCOUNT_USAGE.TABLE_STORAGE_METRICS` (covered by the same grant as
        the usage preflight); Redshift reads `SVV_TABLE_INFO` (active bytes only); BigQuery
        has no equivalent surface and returns an empty dict, so its sizes come from
        catalog.json alone.
        """
        ...

    def table_hygiene(self) -> dict[str, TableHygiene]:
        """relation_key -> maintenance state, for the Redshift-only table-hygiene check.

        Redshift reads the `SVV_TABLE_INFO` maintenance columns (unsorted region, statistics
        staleness, slice skew); BigQuery and Snowflake manage these automatically, expose no
        equivalent, and return an empty dict, since the CLI only calls this on Redshift.
        """
        ...

    def source_last_modified(self, datasets: Set[str]) -> dict[str, datetime]:
        """relation_key -> when the table last received data, for the stale-source check.

        `datasets` are `database.schema` keys of the declared sources. Raises
        `MissingPermissionError` when the metadata cannot be read (on BigQuery this needs
        read access to the source datasets); returns an empty dict when `datasets` is empty.
        Redshift has no such metadata and always returns an empty dict; the CLI skips the
        check there without calling this.
        """
        ...
