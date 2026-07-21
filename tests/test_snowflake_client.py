"""Tests for the Snowflake client's SDK-free surface and the warehouse seam.

The connector is an optional extra, so these tests pin the behaviour that must hold without it:
the module imports cleanly, the missing dependency produces a friendly `WarehouseError`, the
Protocol is satisfied, and the paths that never reach the SDK (empty datasets, no inferable
database) behave. The missing dependency is simulated via `without_connector`, so these hold
whether or not the extra happens to be installed. The live paths (connect, preflight, query
execution, error translation) are exercised against a stub connector registered under the real
module names, the same recipe as the Databricks tests.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
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
from dbt_debt.consumption.snowflake import RealSnowflakeClient
from dbt_debt.domain import TableStorage, UsageRow, WarehouseRelation
from tests.fakes import FakeWarehouseClient


class _ConnectorError(Exception):
    pass


class _ProgrammingError(_ConnectorError):
    pass


class _DictCursor:
    """Stands in for the connector's DictCursor marker class."""


class _Cursor:
    def __init__(
        self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None
    ) -> None:
        self.rows = rows
        self.error = error
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self.error:
            raise self.error

    def fetchall(self) -> list[dict[str, Any]] | None:
        return self.rows

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.cursor_classes: list[type] = []

    def cursor(self, cursor_class: type) -> _Cursor:
        self.cursor_classes.append(cursor_class)
        return self._cursor


def _install_connector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cursor: _Cursor | None = None,
    connect_error: Exception | None = None,
) -> dict[str, Any]:
    calls: dict[str, Any] = {}
    errors = ModuleType("snowflake.connector.errors")
    errors.Error = _ConnectorError  # type: ignore[attr-defined]
    errors.ProgrammingError = _ProgrammingError  # type: ignore[attr-defined]
    connector = ModuleType("snowflake.connector")
    connector.errors = errors  # type: ignore[attr-defined]
    connector.DictCursor = _DictCursor  # type: ignore[attr-defined]

    def connect(**kwargs: Any) -> _Connection:
        calls["kwargs"] = dict(kwargs)
        if connect_error:
            raise connect_error
        return _Connection(cursor or _Cursor())

    connector.connect = connect  # type: ignore[attr-defined]
    package = ModuleType("snowflake")
    package.connector = connector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "snowflake", package)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector)
    monkeypatch.setitem(sys.modules, "snowflake.connector.errors", errors)
    return calls


def _live_client(
    monkeypatch: pytest.MonkeyPatch,
    cursor: _Cursor,
    database: str | None = "db",
) -> RealSnowflakeClient:
    _install_connector(monkeypatch, cursor=cursor)
    return RealSnowflakeClient(Config(warehouse="snowflake"), database=database)


def _bare_client(database: str | None) -> RealSnowflakeClient:
    """A client with no live connection, for exercising the pre-SDK code paths."""

    client = RealSnowflakeClient.__new__(RealSnowflakeClient)
    client._config = Config(warehouse="snowflake")
    client._database = database
    return client


def test_module_imports_without_the_connector() -> None:
    # The lazy-import guarantee: loading the module (as the CLI factory does) must never
    # require the SDK; only constructing a live client does. What actually enforces this is
    # CI installing only [dev]: with the extra absent, a top-level SDK import would break this
    # file's own imports at collection. This is the readable statement of the rule.
    import dbt_debt.consumption.snowflake  # noqa: F401


def test_missing_connector_reads_as_a_friendly_warehouse_error(
    without_connector: Callable[[str], None],
) -> None:
    without_connector("snowflake.connector")
    with pytest.raises(WarehouseError, match=r"dbt-debt\[snowflake\]"):
        RealSnowflakeClient(Config(warehouse="snowflake"))


def test_implementations_satisfy_the_protocol() -> None:
    assert issubclass(RealSnowflakeClient, WarehouseClient)
    assert issubclass(FakeWarehouseClient, WarehouseClient)


def test_existing_relations_empty_datasets_short_circuits() -> None:
    assert _bare_client("analytics").existing_relations(set()) == []


def test_existing_relations_without_a_database_steps_aside() -> None:
    # No inferable database means the orphan scan cannot run; it must degrade like a missing
    # metadata grant (skipped with a message), never crash the scan.
    with pytest.raises(MissingPermissionError, match="--project"):
        _bare_client(None).existing_relations({"marts"})


def test_existing_relations_rejects_a_malformed_schema_name() -> None:
    # A schema name failing the builder's injection guard must degrade like a missing grant
    # (the CLI warns and skips orphan discovery), never escape as a ValueError traceback.
    with pytest.raises(InvalidIdentifierError, match="orphaned-relation discovery is skipped"):
        _bare_client("analytics").existing_relations({"bad-schema"})


def test_connects_with_the_named_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_connector(monkeypatch)
    RealSnowflakeClient(Config(warehouse="snowflake", connection="team"))
    assert calls["kwargs"] == {"connection_name": "team"}
    RealSnowflakeClient(Config(warehouse="snowflake"))
    assert calls["kwargs"] == {}


def test_connect_failure_reads_as_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_connector(monkeypatch, connect_error=_ConnectorError("bad key"))
    with pytest.raises(MissingCredentialsError, match="connections.toml"):
        RealSnowflakeClient(Config(warehouse="snowflake"))


def test_preflight_passes_when_access_history_is_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor(rows=[{"1": 1}])
    client = _live_client(monkeypatch, cursor)
    client.assert_usage_permission()
    assert "ACCESS_HISTORY" in cursor.executed[0]


def test_preflight_denial_names_the_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _live_client(monkeypatch, _Cursor(error=_ProgrammingError("not authorized")))
    with pytest.raises(MissingPermissionError, match="IMPORTED PRIVILEGES"):
        client.assert_usage_permission()


def test_usage_rows_are_lowercased_for_the_shared_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Snowflake uppercases unquoted result aliases, so the client lowercases row keys before
    # handing them to the warehouse-neutral parsers.
    when = datetime(2026, 1, 2, tzinfo=timezone.utc)
    cursor = _Cursor(
        rows=[
            {
                "RELATION_KEY": "DB.MARTS.M",
                "QUERY_COUNT": 2,
                "LAST_QUERIED": when,
                "BYTES_SCANNED": 10,
            }
        ]
    )
    client = _live_client(monkeypatch, cursor)
    assert client.table_usage() == [UsageRow("db.marts.m", 2, when, 10)]
    assert client._conn.cursor_classes == [_DictCursor]  # type: ignore[attr-defined]
    assert cursor.closed is True


def test_query_texts_and_first_seen_reuse_the_shared_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    when = datetime(2026, 1, 2, tzinfo=timezone.utc)
    texts = _live_client(monkeypatch, _Cursor(rows=[{"QUERY": "SELECT 1"}]))
    assert texts.query_texts() == ["SELECT 1"]
    ages = _live_client(
        monkeypatch, _Cursor(rows=[{"RELATION_KEY": "DB.MARTS.M", "FIRST_SEEN": when}])
    )
    assert ages.relation_first_seen() == {"db.marts.m": when}


def test_table_storage_reads_live_byte_figures(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor(
        rows=[
            {
                "RELATION_KEY": "DB.MARTS.M",
                "ACTIVE_BYTES": 5,
                "TIME_TRAVEL_BYTES": 1,
                "FAILSAFE_BYTES": None,
            }
        ]
    )
    client = _live_client(monkeypatch, cursor)
    assert client.table_storage() == {"db.marts.m": TableStorage(5, 1, 0)}


def test_table_hygiene_is_always_empty() -> None:
    # Snowflake maintains tables automatically; the CLI only calls this on Redshift.
    assert _bare_client("db").table_hygiene() == {}


def test_source_last_modified_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    when = datetime(2026, 1, 2, tzinfo=timezone.utc)
    client = _live_client(
        monkeypatch, _Cursor(rows=[{"RELATION_KEY": "DB.RAW.T", "LAST_MODIFIED": when}])
    )
    assert client.source_last_modified(set()) == {}
    assert client.source_last_modified({"db.raw"}) == {"db.raw.t": when}
    with pytest.raises(InvalidIdentifierError, match="stale-source check is skipped"):
        client.source_last_modified({"db.bad-schema"})


def test_existing_relations_parses_rows_live(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor(rows=[{"RELATION_KEY": "DB.MARTS.ZOMBIE", "TABLE_TYPE": "BASE TABLE"}])
    client = _live_client(monkeypatch, cursor)
    assert client.existing_relations({"marts"}) == [
        WarehouseRelation("db.marts.zombie", "BASE TABLE")
    ]


def test_existing_relations_denial_names_the_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _live_client(monkeypatch, _Cursor(error=_ProgrammingError("not authorized")))
    with pytest.raises(MissingPermissionError, match="USAGE on the database"):
        client.existing_relations({"marts"})


def test_errors_past_the_preflight_read_as_warehouse_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After a successful preflight a not-authorized error is unexpected, so both connector
    # error classes translate to a plain WarehouseError naming the stage.
    for error in (_ConnectorError("timeout"), _ProgrammingError("odd denial")):
        client = _live_client(monkeypatch, _Cursor(error=error))
        with pytest.raises(WarehouseError, match="query history") as caught:
            client.table_usage()
        assert not isinstance(caught.value, MissingPermissionError)


def test_missing_result_rows_read_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _live_client(monkeypatch, _Cursor(rows=None))
    assert client.table_usage() == []
