"""Tests for the Redshift client's SDK-free surface and the warehouse seam.

The connector is deliberately absent from the dev environment (it is an optional extra), so
these tests pin exactly the behaviour that must hold without it: the module imports cleanly,
the missing dependency produces a friendly `WarehouseError`, the Protocol is satisfied, and
the paths that never reach the SDK (empty datasets, no inferable database, no staleness
metadata) behave. The live paths (env-var connection, the empty-result preflight, tuple-row
normalization, error translation) are exercised against a stub connector registered under the
real module name, the same recipe as the Databricks tests.
"""

from __future__ import annotations

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
from dbt_debt.consumption.redshift import RealRedshiftClient
from dbt_debt.domain import TableHygiene, UsageRow, WarehouseRelation


class _ConnectorError(Exception):
    pass


class _ProgrammingError(_ConnectorError):
    pass


class _Cursor:
    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        columns: tuple[str, ...] | None = (),
        error: Exception | None = None,
    ) -> None:
        self.rows = rows
        self.description = None if columns is None else [(column,) for column in columns]
        self.error = error
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self.error:
            raise self.error

    def fetchall(self) -> list[tuple[Any, ...]] | None:
        return self.rows

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.autocommit = False

    def cursor(self) -> _Cursor:
        return self._cursor


def _install_connector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cursor: _Cursor | None = None,
    connect_error: Exception | None = None,
) -> dict[str, Any]:
    calls: dict[str, Any] = {}
    module = ModuleType("redshift_connector")
    module.Error = _ConnectorError  # type: ignore[attr-defined]
    module.ProgrammingError = _ProgrammingError  # type: ignore[attr-defined]

    def connect(**kwargs: Any) -> _Connection:
        calls["kwargs"] = dict(kwargs)
        if connect_error:
            raise connect_error
        return _Connection(cursor or _Cursor())

    module.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redshift_connector", module)
    return calls


def _configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDSHIFT_HOST", "wg.eu-west-1.redshift-serverless.amazonaws.com")
    monkeypatch.setenv("REDSHIFT_USER", "admin")
    monkeypatch.setenv("REDSHIFT_PASSWORD", "secret")
    monkeypatch.delenv("REDSHIFT_DATABASE", raising=False)
    monkeypatch.delenv("REDSHIFT_PORT", raising=False)


def _live_client(
    monkeypatch: pytest.MonkeyPatch,
    cursor: _Cursor,
    database: str | None = "dev",
) -> RealRedshiftClient:
    _install_connector(monkeypatch, cursor=cursor)
    _configured_env(monkeypatch)
    return RealRedshiftClient(Config(warehouse="redshift"), database=database)


def _bare_client(database: str | None) -> RealRedshiftClient:
    """A client with no live connection, for exercising the pre-SDK code paths."""

    client = RealRedshiftClient.__new__(RealRedshiftClient)
    client._config = Config(warehouse="redshift")
    client._database = database
    return client


def test_module_imports_without_the_connector() -> None:
    # The lazy-import guarantee: loading the module (as the CLI factory does) must never
    # require the SDK; only constructing a live client does. What actually enforces this is
    # CI installing only [dev]: with the extra absent, a top-level SDK import would break this
    # file's own imports at collection. This is the readable statement of the rule.
    import dbt_debt.consumption.redshift  # noqa: F401


def test_missing_settings_or_connector_reads_as_a_friendly_error() -> None:
    # With the connector absent this trips the extra hint; with it installed but no
    # REDSHIFT_* environment variables it names them instead. Both are WarehouseErrors the
    # CLI turns into exit 3.
    with pytest.raises(Exception, match=r"dbt-debt\[redshift\]|REDSHIFT_HOST"):
        RealRedshiftClient(Config(warehouse="redshift"))


def test_implementations_satisfy_the_protocol() -> None:
    assert issubclass(RealRedshiftClient, WarehouseClient)


def test_existing_relations_empty_datasets_short_circuits() -> None:
    assert _bare_client("dev").existing_relations(set()) == []


def test_existing_relations_without_a_database_steps_aside() -> None:
    # No inferable database means the orphan scan cannot run; it must degrade like a missing
    # metadata grant (skipped with a message), never crash the scan.
    with pytest.raises(MissingPermissionError, match="--project"):
        _bare_client(None).existing_relations({"marts"})


def test_existing_relations_rejects_a_malformed_schema_name() -> None:
    # A schema name failing the builder's injection guard must degrade like a missing grant
    # (the CLI warns and skips orphan discovery), never escape as a ValueError traceback.
    with pytest.raises(InvalidIdentifierError, match="orphaned-relation discovery is skipped"):
        _bare_client("dev").existing_relations({"bad-schema"})


def test_naive_datetimes_are_stamped_utc() -> None:
    # The SYS views report UTC timestamps as `timestamp without time zone`, so the driver
    # returns naive datetimes; the verdicts compare against aware `now` values and would
    # raise on a naive one (seen on the first live scan).
    from datetime import datetime, timezone

    from dbt_debt.consumption.jobs import as_utc

    naive = datetime(2026, 7, 10, 12, 0, 0)
    assert as_utc(naive) == datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
    aware = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert as_utc(aware) is aware
    assert as_utc("not a datetime") == "not a datetime"


def test_source_last_modified_is_always_empty() -> None:
    # Redshift exposes no last-data-received metadata; the stale-source check is skipped by
    # the CLI, and the client must never guess.
    assert _bare_client("dev").source_last_modified({"dev.raw"}) == {}


def test_missing_env_settings_fail_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_connector(monkeypatch)
    for key in ("REDSHIFT_HOST", "REDSHIFT_USER", "REDSHIFT_PASSWORD", "REDSHIFT_DATABASE"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(MissingCredentialsError, match="REDSHIFT_HOST"):
        RealRedshiftClient(Config(warehouse="redshift"))


def test_connector_receives_env_settings_and_autocommit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_connector(monkeypatch)
    _configured_env(monkeypatch)
    client = RealRedshiftClient(Config(warehouse="redshift"), database="dev")
    assert calls["kwargs"] == {
        "host": "wg.eu-west-1.redshift-serverless.amazonaws.com",
        "database": "dev",
        "user": "admin",
        "password": "secret",
        "port": 5439,
    }
    assert client._conn.autocommit is True  # type: ignore[attr-defined]


def test_env_database_and_port_override_the_inferred_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_connector(monkeypatch)
    _configured_env(monkeypatch)
    monkeypatch.setenv("REDSHIFT_DATABASE", "analytics")
    monkeypatch.setenv("REDSHIFT_PORT", "5555")
    RealRedshiftClient(Config(warehouse="redshift"), database="dev")
    assert calls["kwargs"]["database"] == "analytics"
    assert calls["kwargs"]["port"] == 5555


def test_connect_failure_reads_as_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_connector(monkeypatch, connect_error=_ConnectorError("bad password"))
    _configured_env(monkeypatch)
    with pytest.raises(MissingCredentialsError, match="Could not connect to Redshift at"):
        RealRedshiftClient(Config(warehouse="redshift"), database="dev")


def test_preflight_passes_when_other_users_history_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _live_client(monkeypatch, _Cursor(rows=[(1,)], columns=("ok",)))
    client.assert_usage_permission()


def test_preflight_empty_result_is_the_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SYS views silently row-filter for regular users, so unlike the other warehouses an
    # empty probe result, not an access error, is what proves the missing grant.
    client = _live_client(monkeypatch, _Cursor(rows=[], columns=("ok",)))
    with pytest.raises(MissingPermissionError, match="cannot see other users"):
        client.assert_usage_permission()


def test_preflight_query_error_reads_as_a_permission_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _live_client(monkeypatch, _Cursor(error=_ProgrammingError("denied")))
    with pytest.raises(MissingPermissionError, match="Cannot verify"):
        client.assert_usage_permission()


def test_tuple_rows_are_zipped_and_stamped_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    # The driver returns tuples and naive UTC timestamps; the client zips them with the
    # cursor's lowercased column names and restores the zone for the shared parsers.
    naive = datetime(2026, 1, 2, 15, 30, 0)
    cursor = _Cursor(
        rows=[("dev.marts.m", 3, naive, None)],
        columns=("RELATION_KEY", "QUERY_COUNT", "LAST_QUERIED", "BYTES_SCANNED"),
    )
    client = _live_client(monkeypatch, cursor)
    assert client.table_usage() == [
        UsageRow("dev.marts.m", 3, datetime(2026, 1, 2, 15, 30, 0, tzinfo=timezone.utc), 0)
    ]
    assert cursor.closed is True


def test_query_texts_and_first_seen_reuse_the_shared_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    when = datetime(2026, 1, 2, tzinfo=timezone.utc)
    texts = _live_client(monkeypatch, _Cursor(rows=[("SELECT 1",)], columns=("query",)))
    assert texts.query_texts() == ["SELECT 1"]
    ages = _live_client(
        monkeypatch,
        _Cursor(rows=[("dev.marts.m", when)], columns=("relation_key", "first_seen")),
    )
    assert ages.relation_first_seen() == {"dev.marts.m": when}


def test_table_hygiene_reads_the_maintenance_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor(
        rows=[("dev.marts.m", 25.0, 12.0, 4.5, 1000, 2_000_000_000)],
        columns=(
            "relation_key",
            "unsorted_percent",
            "stats_off_percent",
            "skew_rows",
            "total_rows",
            "active_bytes",
        ),
    )
    client = _live_client(monkeypatch, cursor)
    assert client.table_hygiene() == {
        "dev.marts.m": TableHygiene(25.0, 12.0, 4.5, 1000, 2_000_000_000)
    }


def test_table_storage_reads_live_byte_figures(monkeypatch: pytest.MonkeyPatch) -> None:
    from dbt_debt.domain import TableStorage

    cursor = _Cursor(rows=[("dev.marts.m", 3_000_000)], columns=("relation_key", "active_bytes"))
    client = _live_client(monkeypatch, cursor)
    assert client.table_storage() == {"dev.marts.m": TableStorage(3_000_000, 0, 0)}


def test_existing_relations_parses_rows_live(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor(rows=[("dev.marts.zombie", "TABLE")], columns=("relation_key", "table_type"))
    client = _live_client(monkeypatch, cursor)
    assert client.existing_relations({"marts"}) == [WarehouseRelation("dev.marts.zombie", "TABLE")]


def test_existing_relations_denial_names_the_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _live_client(monkeypatch, _Cursor(error=_ProgrammingError("denied")))
    with pytest.raises(MissingPermissionError, match="USAGE on the"):
        client.existing_relations({"marts"})


def test_errors_past_the_preflight_read_as_warehouse_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After a successful preflight a permission error is unexpected, so both connector error
    # classes translate to a plain WarehouseError naming the stage.
    for error in (_ConnectorError("timeout"), _ProgrammingError("odd denial")):
        client = _live_client(monkeypatch, _Cursor(error=error))
        with pytest.raises(WarehouseError, match="query history") as caught:
            client.table_usage()
        assert not isinstance(caught.value, MissingPermissionError)


def test_missing_rows_and_description_read_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _live_client(monkeypatch, _Cursor(rows=None, columns=None))
    assert client.table_usage() == []
