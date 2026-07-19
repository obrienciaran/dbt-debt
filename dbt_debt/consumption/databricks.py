"""Databricks ``WarehouseClient`` backed by the optional SQL connector."""

from __future__ import annotations

import os
from collections.abc import Set
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from dbt_debt.config import Config
from dbt_debt.consumption import databricks_queries, jobs
from dbt_debt.consumption.client import (
    InvalidIdentifierError,
    MissingCredentialsError,
    MissingPermissionError,
    WarehouseError,
)
from dbt_debt.domain import TableHygiene, TableStorage, UsageRow, WarehouseRelation

_PERMISSION_HINT = (
    "Databricks usage analysis requires SELECT on system.access.table_lineage and "
    "system.query.history, plus USE CATALOG on system and USE SCHEMA on system.access and "
    "system.query. Partial system-table visibility cannot safely prove a relation is unused."
)


class RealDatabricksClient:
    """Live Databricks client; importing this module never imports the connector."""

    def __init__(self, config: Config, database: str | None = None) -> None:
        try:
            from databricks import sql
        except (ImportError, ModuleNotFoundError) as exc:
            raise WarehouseError(
                "Databricks support needs the optional connector dependency; install it with "
                "`pip install 'dbt-debt[databricks]'`."
            ) from exc

        self._config = config
        self._database = database
        host = _server_hostname()
        http_path = os.environ.get("DATABRICKS_HTTP_PATH")
        if not host or not http_path:
            raise MissingCredentialsError(
                "Could not connect to Databricks: set DATABRICKS_HOST (or "
                "DATABRICKS_SERVER_HOSTNAME) and DATABRICKS_HTTP_PATH. Set DATABRICKS_TOKEN "
                "for PAT authentication, or configure another authentication method supported "
                "by the Databricks SQL Connector."
            )
        kwargs: dict[str, Any] = {"server_hostname": host, "http_path": http_path}
        token = os.environ.get("DATABRICKS_TOKEN")
        if token:
            kwargs["access_token"] = token
        try:
            self._conn = sql.connect(**kwargs)
        except Exception as exc:
            if not _is_connector_error(exc, sql):
                raise
            raise MissingCredentialsError(
                f"Could not connect to Databricks at {host} using {http_path}. ({exc})"
            ) from exc

    def assert_usage_permission(self) -> None:
        try:
            self._run(databricks_queries.permission_probe_query(), "preflight")
        except WarehouseError as exc:
            message = str(exc)
            if _looks_like_permission_error(message):
                raise MissingPermissionError(
                    f"Cannot read the required Databricks system tables. {_PERMISSION_HINT} ({exc})"
                ) from exc
            raise WarehouseError(f"Databricks system-table preflight failed: {exc}") from exc

    def table_usage(self) -> list[UsageRow]:
        exclusion = databricks_queries.exclusion_clause(
            self._config.query_comment_pattern, "h.statement_text"
        )
        sql = databricks_queries.table_usage_query(self._config.lookback_days, exclusion)
        return jobs.parse_usage_rows(self._run(sql, "usage lineage"))

    def query_texts(self) -> list[str]:
        exclusion = databricks_queries.exclusion_clause(self._config.query_comment_pattern)
        sql = databricks_queries.query_text_query(self._config.lookback_days, exclusion)
        return jobs.parse_query_text_rows(self._run(sql, "query text"))

    def relation_first_seen(self) -> dict[str, datetime]:
        return jobs.parse_first_seen_rows(
            self._run(databricks_queries.first_seen_query(), "relation ages")
        )

    def existing_relations(self, datasets: Set[str]) -> list[WarehouseRelation]:
        if not datasets:
            return []
        if not self._database and any("." not in dataset for dataset in datasets):
            raise InvalidIdentifierError(
                "Cannot list Databricks table metadata: no catalog could be inferred from the "
                "models. Pass --project with the catalog name; orphaned-relation discovery is "
                "skipped."
            )
        catalog_datasets = {
            dataset if "." in dataset else f"{self._database}.{dataset}" for dataset in datasets
        }
        try:
            sql = databricks_queries.existing_relations_query(catalog_datasets)
        except ValueError as exc:
            raise InvalidIdentifierError(
                "A managed catalog or schema name in the manifest is not a valid Databricks "
                f"identifier, so orphaned-relation discovery is skipped. ({exc})"
            ) from exc
        try:
            return jobs.parse_relation_rows(self._run(sql, "warehouse table listing"))
        except WarehouseError as exc:
            raise MissingPermissionError(
                "Cannot read system.information_schema.tables for the managed schemas; "
                "orphaned-relation discovery is skipped and undeclared sources are still "
                f"reported. ({exc})"
            ) from exc

    def table_storage(self) -> dict[str, TableStorage]:
        """No live override: dbt-databricks' catalog.json ``bytes`` value remains authoritative."""

        return {}

    def table_hygiene(self) -> dict[str, TableHygiene]:
        """Deferred: this contribution does not define a Databricks hygiene verdict."""

        return {}

    def source_last_modified(self, datasets: Set[str]) -> dict[str, datetime]:
        """Deferred: this contribution does not define Databricks source freshness."""

        del datasets
        return {}

    def _run(self, sql_text: str, stage: str) -> list[dict[str, Any]]:
        try:
            cursor = self._conn.cursor()
            try:
                cursor.execute(sql_text)
                rows = cursor.fetchall()
                columns = [str(column[0]).lower() for column in cursor.description or []]
            finally:
                cursor.close()
        except Exception as exc:
            if not _is_databricks_error(exc):
                raise
            raise WarehouseError(f"Databricks query for {stage} failed: {exc}") from exc
        return [
            {column: value for column, value in zip(columns, row, strict=False)}
            for row in rows or []
        ]


def _server_hostname() -> str | None:
    """Normalize dbt's DATABRICKS_HOST or the connector's explicit hostname variable."""

    value = os.environ.get("DATABRICKS_SERVER_HOSTNAME") or os.environ.get("DATABRICKS_HOST")
    if not value:
        return None
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    return parsed.hostname


def endpoint_identity() -> str:
    """Non-secret endpoint identity used to isolate cached warehouse results."""

    return f"{_server_hostname() or ''}|{os.environ.get('DATABRICKS_HTTP_PATH', '')}"


def _is_connector_error(exc: Exception, sql_module: Any) -> bool:
    error_type = getattr(sql_module, "Error", None)
    return isinstance(error_type, type) and isinstance(exc, error_type)


def _is_databricks_error(exc: Exception) -> bool:
    try:
        from databricks.sql.exc import Error
    except (ImportError, ModuleNotFoundError):
        return False
    return isinstance(exc, Error)


def _looks_like_permission_error(message: str) -> bool:
    upper = message.upper()
    return any(
        marker in upper
        for marker in (
            "INSUFFICIENT_PERMISSION",
            "PERMISSION_DENIED",
            "NOT AUTHORIZED",
            "UNAUTHORIZED",
            "ACCESS_DENIED",
            "USE SCHEMA",
            "USE CATALOG",
        )
    )
