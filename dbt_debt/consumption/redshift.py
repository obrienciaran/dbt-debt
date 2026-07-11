"""The Redshift `WarehouseClient`, the only module that imports `redshift-connector`.

It composes the pure SQL builders in `redshift_queries` with a live connection and reuses the
warehouse-neutral row parsers in `jobs`. Imports are lazy so the rest of the package (and the
test suite, via the fake) loads without the Redshift dependency or any credentials. The
connector is an optional extra (`pip install 'dbt-debt[redshift]'`).

Connection details come from environment variables (`REDSHIFT_HOST`, `REDSHIFT_USER`,
`REDSHIFT_PASSWORD`, optional `REDSHIFT_DATABASE` and `REDSHIFT_PORT`): redshift-connector has
no named-connection file like Snowflake's connections.toml, and env vars keep credentials out
of both the repository and the shell history. Core loop validated against a live Serverless
workgroup. See DESIGN.md's Redshift section for what is confirmed and what remains open.
"""

from __future__ import annotations

import os
from collections.abc import Set
from datetime import datetime, timezone
from typing import Any, cast

from dbt_debt.config import Config
from dbt_debt.consumption import jobs, redshift_queries
from dbt_debt.consumption.client import (
    MissingCredentialsError,
    MissingPermissionError,
    WarehouseError,
)
from dbt_debt.domain import TableHygiene, TableStorage, UsageRow, WarehouseRelation

_PERMISSION_HINT = (
    "the SYS query-history views show a regular user only their own queries, so 'unused' would "
    "count only your own use and be false-confident. Connect as a superuser (the namespace "
    "admin) or a user granted SYSLOG ACCESS UNRESTRICTED."
)

_CREDENTIALS_HINT = (
    "Set REDSHIFT_HOST (the workgroup or cluster endpoint), REDSHIFT_USER, and "
    "REDSHIFT_PASSWORD; REDSHIFT_DATABASE and REDSHIFT_PORT are optional (the database "
    "defaults to the models' database, the port to 5439)."
)


class RealRedshiftClient:
    """Live Redshift client implementing the `WarehouseClient` Protocol."""

    def __init__(self, config: Config, database: str | None = None) -> None:
        try:
            import redshift_connector
        except ModuleNotFoundError as exc:
            raise WarehouseError(
                "Redshift support needs the optional connector dependency; install it with "
                "`pip install 'dbt-debt[redshift]'`."
            ) from exc

        self._config = config
        self._database = database
        host = os.environ.get("REDSHIFT_HOST")
        user = os.environ.get("REDSHIFT_USER")
        password = os.environ.get("REDSHIFT_PASSWORD")
        connect_database = os.environ.get("REDSHIFT_DATABASE") or database
        if not (host and user and password and connect_database):
            raise MissingCredentialsError(
                f"Could not connect to Redshift: missing connection settings. {_CREDENTIALS_HINT}"
            )
        try:
            self._conn = redshift_connector.connect(
                host=host,
                database=connect_database,
                user=user,
                password=password,
                port=int(os.environ.get("REDSHIFT_PORT") or 5439),
            )
        except redshift_connector.Error as exc:
            raise MissingCredentialsError(
                f"Could not connect to Redshift at {host}. {_CREDENTIALS_HINT} ({exc})"
            ) from exc
        # Plain reads need no transaction, and autocommit keeps the session from holding one
        # open across the scan's queries.
        self._conn.autocommit = True

    def assert_usage_permission(self) -> None:
        """Confirm the caller sees account-wide query history, not just their own rows.

        The SYS views are readable by everyone but silently row-filtered for regular users, so
        unlike the other warehouses the probe cannot rely on an access error: it returns a row
        only for a superuser or SYSLOG ACCESS UNRESTRICTED, and an empty result is the failure.
        """

        try:
            rows = self._run(redshift_queries.permission_probe_query(), "preflight")
        except _programming_error() as exc:
            raise MissingPermissionError(
                f"Cannot verify query-history visibility on Redshift: {_PERMISSION_HINT}"
            ) from exc
        if not rows:
            raise MissingPermissionError(
                f"This Redshift user cannot see other users' query history: {_PERMISSION_HINT}"
            )

    def table_usage(self) -> list[UsageRow]:
        exclusion = redshift_queries.exclusion_clause(
            self._config.query_comment_pattern, column="qh.query_text"
        )
        sql = redshift_queries.table_usage_query(self._config.lookback_days, exclusion)
        return jobs.parse_usage_rows(self._run_wrapped(sql, "query history"))

    def query_texts(self) -> list[str]:
        exclusion = redshift_queries.exclusion_clause(self._config.query_comment_pattern)
        sql = redshift_queries.query_text_query(self._config.lookback_days, exclusion)
        return jobs.parse_query_text_rows(self._run_wrapped(sql, "query text"))

    def relation_first_seen(self) -> dict[str, datetime]:
        sql = redshift_queries.first_seen_query()
        return jobs.parse_first_seen_rows(self._run_wrapped(sql, "relation ages"))

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        if not datasets:
            return []
        if not self._database:
            raise MissingPermissionError(
                "Cannot list Redshift table metadata: no database could be inferred from the "
                "models. Pass --project with the database name; orphaned-relation discovery is "
                "skipped, undeclared sources are still reported."
            )
        sql = redshift_queries.existing_relations_query(self._database, datasets)
        try:
            rows = self._run(sql, "warehouse table listing")
        except _programming_error() as exc:
            raise MissingPermissionError(
                "Cannot read table metadata for the managed schemas (need USAGE on the "
                "schemas). Orphaned-relation discovery is skipped; undeclared sources are "
                "still reported from the manifest."
            ) from exc
        return jobs.parse_relation_rows(rows)

    def table_storage(self) -> dict[str, TableStorage]:
        sql = redshift_queries.table_storage_query()
        return jobs.parse_table_storage_rows(self._run_wrapped(sql, "storage metrics"))

    def table_hygiene(self) -> dict[str, TableHygiene]:
        sql = redshift_queries.table_hygiene_query()
        return jobs.parse_table_hygiene_rows(self._run_wrapped(sql, "table hygiene"))

    def source_last_modified(self, datasets: Set[str]) -> dict[str, datetime]:
        """Always empty: Redshift exposes no last-data-received metadata to read.

        There is no `last_altered` or `__TABLES__` analogue, and inferring freshness from the
        query history would breach the retention-bounded window. The CLI skips the
        stale-source check on Redshift with a note rather than calling this.
        """

        return {}

    def _run_wrapped(self, sql: str, stage: str) -> list[dict[str, Any]]:
        """Run one query with *every* failure translated, for stages past the preflight.

        A permission error after a successful preflight is unexpected, so it reads as a plain
        warehouse failure rather than a permission verdict.
        """

        try:
            return self._run(sql, stage)
        except _programming_error() as exc:
            raise WarehouseError(f"Redshift query for {stage} failed: {exc}") from exc

    def _run(self, sql: str, stage: str) -> list[dict[str, Any]]:
        """Run one query, translating API failures into readable `WarehouseError`s.

        `ProgrammingError` passes through untouched so the callers that give it a sharper
        meaning (the preflight, the managed-schema listing) can still catch it. Rows come back
        as tuples, so they are zipped with the cursor's lowercased column names to match the
        shared parsers.
        """

        import redshift_connector

        try:
            cursor = self._conn.cursor()
            try:
                cursor.execute(sql)
                rows = cursor.fetchall()
                columns = [str(column[0]).lower() for column in cursor.description or []]
            finally:
                cursor.close()
        except redshift_connector.ProgrammingError:
            raise
        except redshift_connector.Error as exc:
            raise WarehouseError(f"Redshift query for {stage} failed: {exc}") from exc
        return [
            {column: _as_utc(value) for column, value in zip(columns, row)} for row in rows or []
        ]


def _as_utc(value: Any) -> Any:
    """Stamp UTC on naive datetimes; everything else passes through.

    The SYS views report timestamps in UTC but as `timestamp without time zone`, so the
    driver hands back naive datetimes (confirmed live), unlike BigQuery and Snowflake, whose
    values arrive timezone-aware. The verdicts compare against aware `now` values, so the
    zone is restored here, at the one boundary that knows it was dropped.
    """

    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _programming_error() -> type[Exception]:
    """The connector's `ProgrammingError`, imported lazily to keep module import SDK-free."""

    import redshift_connector

    return cast("type[Exception]", redshift_connector.ProgrammingError)
