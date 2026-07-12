"""The Snowflake `WarehouseClient`, the only module that imports `snowflake-connector-python`.

It composes the pure SQL builders in `snowflake_queries` with a live connection and reuses the
warehouse-neutral row parsers in `jobs`. Imports are lazy so the rest of the package (and the
test suite, via the fake) loads without the Snowflake dependency or any credentials. The
connector is an optional extra (`pip install 'dbt-debt[snowflake]'`).

Validated against a live Enterprise account (see DESIGN.md). Known documented caveats:
ACCOUNT_USAGE views lag reality by up to ~45 minutes (harmless for a debt scan), and
ACCESS_HISTORY needs Enterprise edition plus IMPORTED PRIVILEGES on the SNOWFLAKE database.
"""

from __future__ import annotations

from collections.abc import Set
from datetime import datetime
from typing import Any, cast

from dbt_debt.config import Config
from dbt_debt.consumption import jobs, snowflake_queries
from dbt_debt.consumption.client import (
    InvalidIdentifierError,
    MissingCredentialsError,
    MissingPermissionError,
    WarehouseError,
)
from dbt_debt.domain import TableHygiene, TableStorage, UsageRow, WarehouseRelation

_PERMISSION_HINT = (
    "reading SNOWFLAKE.ACCOUNT_USAGE needs IMPORTED PRIVILEGES on the SNOWFLAKE database, and "
    "ACCESS_HISTORY additionally needs Enterprise edition. Without account-wide history "
    "'unused' would count only your own queries and be false-confident."
)


class RealSnowflakeClient:
    """Live Snowflake client implementing the `WarehouseClient` Protocol."""

    def __init__(self, config: Config, database: str | None = None) -> None:
        try:
            import snowflake.connector
        except ModuleNotFoundError as exc:
            raise WarehouseError(
                "Snowflake support needs the optional connector dependency; install it with "
                "`pip install 'dbt-debt[snowflake]'`."
            ) from exc

        self._config = config
        self._database = database
        try:
            if config.connection:
                self._conn = snowflake.connector.connect(connection_name=config.connection)
            else:
                self._conn = snowflake.connector.connect()
        except snowflake.connector.errors.Error as exc:
            raise MissingCredentialsError(
                "Could not connect to Snowflake. Define a connection in "
                "~/.snowflake/connections.toml (or SNOWFLAKE_* environment variables) and pass "
                f"--connection NAME if it is not the default. ({exc})"
            ) from exc

    def assert_usage_permission(self) -> None:
        """Confirm the caller can read account-wide history by actually touching ACCESS_HISTORY.

        Selecting from the view exercises the exact capability the usage query relies on;
        Snowflake reports both a missing share grant and a missing edition as the same
        not-authorized `ProgrammingError`, so one probe covers both.
        """

        try:
            self._run(snowflake_queries.permission_probe_query(), "preflight")
        except _programming_error() as exc:
            raise MissingPermissionError(
                f"Cannot read SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY: {_PERMISSION_HINT}"
            ) from exc

    def table_usage(self) -> list[UsageRow]:
        exclusion = snowflake_queries.exclusion_clause(
            self._config.query_comment_pattern, column="qh.query_text"
        )
        sql = snowflake_queries.table_usage_query(self._config.lookback_days, exclusion)
        return jobs.parse_usage_rows(self._run_wrapped(sql, "query history"))

    def query_texts(self) -> list[str]:
        exclusion = snowflake_queries.exclusion_clause(self._config.query_comment_pattern)
        sql = snowflake_queries.query_text_query(self._config.lookback_days, exclusion)
        return jobs.parse_query_text_rows(self._run_wrapped(sql, "query text"))

    def relation_first_seen(self) -> dict[str, datetime]:
        sql = snowflake_queries.first_seen_query()
        return jobs.parse_first_seen_rows(self._run_wrapped(sql, "relation ages"))

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        if not datasets:
            return []
        if not self._database:
            raise MissingPermissionError(
                "Cannot list Snowflake table metadata: no database could be inferred from the "
                "models. Pass --project with the database name; orphaned-relation discovery is "
                "skipped, undeclared sources are still reported from the manifest."
            )
        try:
            sql = snowflake_queries.existing_relations_query(self._database, datasets)
        except ValueError as exc:
            raise InvalidIdentifierError(
                "A managed schema name in the manifest is not a valid Snowflake identifier, so "
                f"orphaned-relation discovery is skipped; undeclared sources are still "
                f"reported. ({exc})"
            ) from exc
        try:
            rows = self._run(sql, "warehouse table listing")
        except _programming_error() as exc:
            raise MissingPermissionError(
                "Cannot read table metadata for the managed schemas (need USAGE on the database "
                "and schemas). Orphaned-relation discovery is skipped; undeclared sources are "
                "still reported from the manifest."
            ) from exc
        return jobs.parse_relation_rows(rows)

    def table_storage(self) -> dict[str, TableStorage]:
        sql = snowflake_queries.table_storage_query()
        return jobs.parse_table_storage_rows(self._run_wrapped(sql, "storage metrics"))

    def table_hygiene(self) -> dict[str, TableHygiene]:
        """Always empty: Snowflake micro-partitions and maintains tables automatically and
        exposes no maintenance columns; the CLI only calls this on Redshift."""

        return {}

    def source_last_modified(self, datasets: Set[str]) -> dict[str, datetime]:
        if not datasets:
            return {}
        try:
            sql = snowflake_queries.source_last_modified_query(datasets)
        except ValueError as exc:
            raise InvalidIdentifierError(
                "A declared source's schema name in the manifest is not a valid Snowflake "
                f"identifier, so the stale-source check is skipped; the rest of the scan is "
                f"unaffected. ({exc})"
            ) from exc
        return jobs.parse_last_modified_rows(self._run_wrapped(sql, "source freshness"))

    def _run_wrapped(self, sql: str, stage: str) -> list[dict[str, Any]]:
        """Run one query with *every* failure translated, for stages past the preflight.

        A not-authorized error after a successful preflight is unexpected, so it reads as a
        plain warehouse failure rather than a permission verdict.
        """

        try:
            return self._run(sql, stage)
        except _programming_error() as exc:
            raise WarehouseError(f"Snowflake query for {stage} failed: {exc}") from exc

    def _run(self, sql: str, stage: str) -> list[dict[str, Any]]:
        """Run one query, translating API failures into readable `WarehouseError`s.

        `ProgrammingError` passes through untouched so the callers that give it a sharper
        meaning (the preflight, the managed-schema listing) can still catch it. Row keys are
        lowercased because Snowflake uppercases unquoted result aliases, while the shared
        parsers read the lowercase names the SQL spells.
        """

        from snowflake.connector import DictCursor
        from snowflake.connector.errors import Error, ProgrammingError

        try:
            cursor = self._conn.cursor(DictCursor)
            try:
                cursor.execute(sql)
                rows = cursor.fetchall()
            finally:
                cursor.close()
        except ProgrammingError:
            raise
        except Error as exc:
            raise WarehouseError(f"Snowflake query for {stage} failed: {exc}") from exc
        return [{str(key).lower(): value for key, value in row.items()} for row in rows or []]


def _programming_error() -> type[Exception]:
    """The connector's `ProgrammingError`, imported lazily to keep module import SDK-free."""

    from snowflake.connector.errors import ProgrammingError

    return cast("type[Exception]", ProgrammingError)
