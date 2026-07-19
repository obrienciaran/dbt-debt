"""Mock-only tests for the lazy Databricks connector client."""

from __future__ import annotations

import builtins
import sys
from datetime import datetime, timezone
from types import ModuleType
from typing import Any

import pytest

from dbt_debt.config import Config
from dbt_debt.consumption.client import (
    InvalidIdentifierError,
    MissingCredentialsError,
    MissingPermissionError,
    WarehouseClient,
    WarehouseError,
)
from dbt_debt.consumption.databricks import RealDatabricksClient, endpoint_identity
from dbt_debt.domain import UsageRow


class _ConnectorError(Exception):
    pass


class _Cursor:
    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        columns: tuple[str, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.description = [(column,) for column in columns]
        self.error = error
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self.error:
            raise self.error

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


def _install_connector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cursor: _Cursor | None = None,
    connect_error: Exception | None = None,
) -> tuple[ModuleType, dict[str, Any]]:
    calls: dict[str, Any] = {}
    sql = ModuleType("databricks.sql")
    sql.Error = _ConnectorError  # type: ignore[attr-defined]

    def connect(**kwargs: Any) -> _Connection:
        calls.update(kwargs)
        if connect_error:
            raise connect_error
        return _Connection(cursor or _Cursor())

    sql.connect = connect  # type: ignore[attr-defined]
    package = ModuleType("databricks")
    package.sql = sql  # type: ignore[attr-defined]
    exc = ModuleType("databricks.sql.exc")
    exc.Error = _ConnectorError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databricks", package)
    monkeypatch.setitem(sys.modules, "databricks.sql", sql)
    monkeypatch.setitem(sys.modules, "databricks.sql.exc", exc)
    return sql, calls


def _configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.example.com/")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("DATABRICKS_TOKEN", "secret")
    monkeypatch.delenv("DATABRICKS_SERVER_HOSTNAME", raising=False)


def _bare_client(
    cursor: _Cursor | None = None, database: str | None = "main"
) -> RealDatabricksClient:
    client = RealDatabricksClient.__new__(RealDatabricksClient)
    client._config = Config(warehouse="databricks")
    client._database = database
    client._conn = _Connection(cursor or _Cursor())
    return client


def test_module_imports_without_importing_the_connector() -> None:
    import dbt_debt.consumption.databricks  # noqa: F401


def test_missing_connector_has_the_optional_extra_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def missing(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "databricks":
            raise ModuleNotFoundError("forced missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(WarehouseError, match=r"dbt-debt\[databricks\]"):
        RealDatabricksClient(Config(warehouse="databricks"))


def test_missing_endpoint_settings_fail_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_connector(monkeypatch)
    for key in (
        "DATABRICKS_HOST",
        "DATABRICKS_SERVER_HOSTNAME",
        "DATABRICKS_HTTP_PATH",
        "DATABRICKS_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(MissingCredentialsError, match="DATABRICKS_HTTP_PATH"):
        RealDatabricksClient(Config(warehouse="databricks"))


def test_connector_receives_normalized_dbt_environment_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, calls = _install_connector(monkeypatch)
    _configured_env(monkeypatch)
    RealDatabricksClient(Config(warehouse="databricks"))
    assert calls == {
        "server_hostname": "workspace.example.com",
        "http_path": "/sql/1.0/warehouses/abc",
        "access_token": "secret",
    }
    assert endpoint_identity() == "workspace.example.com|/sql/1.0/warehouses/abc"


def test_connector_failure_becomes_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_connector(monkeypatch, connect_error=_ConnectorError("bad token"))
    _configured_env(monkeypatch)
    with pytest.raises(MissingCredentialsError, match="bad token"):
        RealDatabricksClient(Config(warehouse="databricks"))


def test_rows_are_normalized_for_shared_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    when = datetime(2026, 1, 2, tzinfo=timezone.utc)
    cursor = _Cursor(
        rows=[("MAIN.MARTS.MODEL", 2, when, None)],
        columns=("RELATION_KEY", "QUERY_COUNT", "LAST_QUERIED", "BYTES_SCANNED"),
    )
    _install_connector(monkeypatch, cursor=cursor)
    _configured_env(monkeypatch)
    client = RealDatabricksClient(Config(warehouse="databricks"))
    assert client.table_usage() == [UsageRow("main.marts.model", 2, when, 0)]
    assert cursor.closed is True


def test_required_preflight_fails_loudly_on_system_table_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_connector(monkeypatch)
    _configured_env(monkeypatch)
    client = RealDatabricksClient(Config(warehouse="databricks"))
    client._conn = _Connection(_Cursor(error=_ConnectorError("USE SCHEMA denied")))
    with pytest.raises(MissingPermissionError, match="system.access.table_lineage"):
        client.assert_usage_permission()


def test_non_permission_preflight_failure_remains_a_warehouse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_connector(monkeypatch)
    _configured_env(monkeypatch)
    client = RealDatabricksClient(Config(warehouse="databricks"))
    client._conn = _Connection(_Cursor(error=_ConnectorError("network timeout")))
    with pytest.raises(WarehouseError, match="preflight failed") as caught:
        client.assert_usage_permission()
    assert not isinstance(caught.value, MissingPermissionError)


def test_existing_relations_empty_and_invalid_paths_degrade_safely() -> None:
    client = _bare_client()
    assert client.existing_relations(set()) == []
    with pytest.raises(InvalidIdentifierError, match="orphaned-relation discovery"):
        client.existing_relations({"main.bad-schema"})


def test_existing_relations_qualifies_managed_schemas_with_the_catalog() -> None:
    cursor = _Cursor()
    client = _bare_client(cursor, database="workspace")
    assert client.existing_relations({"Marts"}) == []
    assert "IN ('workspace.marts')" in cursor.executed[0]


def test_existing_relations_without_an_inferred_catalog_steps_aside() -> None:
    client = _bare_client(database=None)
    with pytest.raises(InvalidIdentifierError, match="--project"):
        client.existing_relations({"marts"})


def test_optional_unvalidated_checks_return_no_verdict_data() -> None:
    client = _bare_client()
    assert client.table_storage() == {}
    assert client.table_hygiene() == {}
    assert client.source_last_modified({"main.raw"}) == {}


def test_client_satisfies_the_warehouse_protocol() -> None:
    assert issubclass(RealDatabricksClient, WarehouseClient)
