"""Tests for the real BigQuery client's preflight behaviour.

These bypass `__init__` (which would need live credentials) and inject a stub job handle, so
they exercise the client's own error handling without touching BigQuery. The SDK is an
optional extra, so these tests must hold without it: when it is absent, stub exception modules
are registered under the real names. The client imports the exception classes lazily at call
time, so the classes it catches and the classes these tests raise stay the same objects,
however the environment is set up.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

from dbt_debt.config import Config
from dbt_debt.consumption.bigquery import RealBigQueryClient
from dbt_debt.consumption.client import (
    InvalidIdentifierError,
    MissingCredentialsError,
    MissingPermissionError,
    WarehouseError,
)

try:
    from google.api_core.exceptions import BadRequest, Forbidden, GoogleAPIError
except ModuleNotFoundError:

    class _GoogleAPIError(Exception):
        pass

    class _Forbidden(_GoogleAPIError):
        pass

    class _BadRequest(_GoogleAPIError):
        pass

    _exceptions = ModuleType("google.api_core.exceptions")
    _exceptions.GoogleAPIError = _GoogleAPIError  # type: ignore[attr-defined]
    _exceptions.Forbidden = _Forbidden  # type: ignore[attr-defined]
    _exceptions.BadRequest = _BadRequest  # type: ignore[attr-defined]
    _api_core = ModuleType("google.api_core")
    _api_core.exceptions = _exceptions  # type: ignore[attr-defined]
    _google = ModuleType("google")
    _google.api_core = _api_core  # type: ignore[attr-defined]
    sys.modules.setdefault("google", _google)
    sys.modules.setdefault("google.api_core", _api_core)
    sys.modules["google.api_core.exceptions"] = _exceptions
    BadRequest, Forbidden, GoogleAPIError = _BadRequest, _Forbidden, _GoogleAPIError


class _RaisingBQ:
    """Stub whose job listing fails as if the caller lacked bigquery.jobs.listAll."""

    def list_jobs(self, **kwargs: Any) -> Any:
        raise Forbidden("denied")


class _ForbiddenQueryBQ:
    """Stub whose query fails as if the caller couldn't read the dataset's table metadata."""

    project = "test-project"

    def query(self, sql: str) -> Any:
        raise Forbidden("denied")


def _client_with(bq: Any) -> RealBigQueryClient:
    client = RealBigQueryClient.__new__(RealBigQueryClient)
    client._config = Config(region="US")
    client._bq = bq
    return client


def test_assert_usage_permission_raises_without_jobs_listall() -> None:
    # Without jobs.listAll the preflight must fail loudly, not silently see only the caller's jobs.
    with pytest.raises(MissingPermissionError):
        _client_with(_RaisingBQ()).assert_usage_permission()


def test_existing_relations_raises_without_tables_list() -> None:
    # A Forbidden listing surfaces as MissingPermissionError so the caller can warn and skip.
    with pytest.raises(MissingPermissionError):
        _client_with(_ForbiddenQueryBQ()).existing_relations({"jaffle_shop"})


def test_existing_relations_rejects_a_malformed_dataset_name() -> None:
    # A dataset name failing the builder's injection guard must degrade like a missing grant
    # (the CLI warns and skips orphan discovery), never escape as a ValueError traceback.
    with pytest.raises(InvalidIdentifierError, match="orphaned-relation discovery is skipped"):
        _client_with(_ForbiddenQueryBQ()).existing_relations({"bad schema"})


def test_source_last_modified_rejects_a_malformed_dataset_name() -> None:
    with pytest.raises(InvalidIdentifierError, match="stale-source check is skipped"):
        _client_with(_ForbiddenQueryBQ()).source_last_modified({"proj.bad schema"})


class _BadRequestBQ:
    """Stub whose query fails the way a wrong --region does."""

    def query(self, sql: str) -> Any:
        raise BadRequest("Unrecognized region")


def test_module_imports_without_the_sdk() -> None:
    # The lazy-import guarantee: loading the module (as the CLI factory does) must never
    # require the SDK; only constructing a live client does.
    import dbt_debt.consumption.bigquery  # noqa: F401


def test_missing_sdk_reads_as_a_friendly_warehouse_error(
    without_package: Callable[[str], None],
) -> None:
    without_package("google")
    with pytest.raises(WarehouseError, match=r"dbt-debt\[bigquery\]"):
        RealBigQueryClient(Config())


def test_non_permission_api_error_becomes_warehouse_error() -> None:
    # A wrong region or transient API failure must end as a readable WarehouseError (exit 3
    # in the CLI), never a raw google traceback.
    with pytest.raises(WarehouseError, match="job history.*--region"):
        _client_with(_BadRequestBQ()).table_usage()


def test_permission_errors_are_warehouse_errors_too() -> None:
    # The CLI catches the base class, so one except covers credentials, permissions, and
    # mid-scan API failures.
    assert issubclass(MissingCredentialsError, WarehouseError)
    assert issubclass(MissingPermissionError, WarehouseError)


class _CredentialsError(Exception):
    """Stands in for google.auth's DefaultCredentialsError under the stubbed SDK."""


def _install_sdk(monkeypatch: pytest.MonkeyPatch, *, credentials: bool = True) -> dict[str, Any]:
    """Register a stub `google.cloud.bigquery` / `google.auth` under the real module names.

    The api_core exception modules are left alone: the module-level stub (or the real SDK)
    already provides them, so the classes the client catches stay the same objects.
    """

    calls: dict[str, Any] = {}

    class _Client:
        def __init__(self, project: str | None = None) -> None:
            calls["project"] = project
            if not credentials:
                raise _CredentialsError("no application default credentials")

    bigquery_module = ModuleType("google.cloud.bigquery")
    bigquery_module.Client = _Client  # type: ignore[attr-defined]
    cloud = ModuleType("google.cloud")
    cloud.bigquery = bigquery_module  # type: ignore[attr-defined]
    auth_exceptions = ModuleType("google.auth.exceptions")
    auth_exceptions.DefaultCredentialsError = _CredentialsError  # type: ignore[attr-defined]
    auth = ModuleType("google.auth")
    auth.exceptions = auth_exceptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery_module)
    monkeypatch.setitem(sys.modules, "google.auth", auth)
    monkeypatch.setitem(sys.modules, "google.auth.exceptions", auth_exceptions)
    return calls


def test_init_passes_the_project_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_sdk(monkeypatch)
    RealBigQueryClient(Config(), project="analytics-proj")
    assert calls["project"] == "analytics-proj"


def test_missing_credentials_name_the_gcloud_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_sdk(monkeypatch, credentials=False)
    with pytest.raises(MissingCredentialsError, match="gcloud auth application-default login"):
        RealBigQueryClient(Config())


class _ListableBQ:
    """Stub whose job listing succeeds, as it does with bigquery.jobs.listAll granted."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def list_jobs(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return iter([])


class _APIErrorBQ:
    """Stub whose job listing fails with a non-permission API error."""

    def list_jobs(self, **kwargs: Any) -> Any:
        raise GoogleAPIError("backend unavailable")


def test_preflight_passes_when_jobs_are_listable() -> None:
    bq = _ListableBQ()
    _client_with(bq).assert_usage_permission()
    assert bq.kwargs == {"all_users": True, "max_results": 1}


def test_non_permission_preflight_failure_remains_a_warehouse_error() -> None:
    with pytest.raises(WarehouseError, match="job listing failed") as caught:
        _client_with(_APIErrorBQ()).assert_usage_permission()
    assert not isinstance(caught.value, MissingPermissionError)


class _RowsBQ:
    """Stub returning canned rows, the shape `_run` hands to the shared parsers."""

    project = "test-project"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sql: list[str] = []

    def query(self, sql: str) -> Any:
        self.sql.append(sql)

        class _Job:
            def __init__(self, rows: list[dict[str, Any]]) -> None:
                self._rows = rows

            def result(self) -> list[dict[str, Any]]:
                return self._rows

        return _Job(self.rows)


def test_query_texts_and_first_seen_reuse_the_shared_parsers() -> None:
    from datetime import datetime, timezone

    when = datetime(2026, 1, 2, tzinfo=timezone.utc)
    texts = _client_with(_RowsBQ([{"query": "SELECT 1"}]))
    assert texts.query_texts() == ["SELECT 1"]
    ages = _client_with(_RowsBQ([{"relation_key": "P.D.T", "first_seen": when}]))
    assert ages.relation_first_seen() == {"p.d.t": when}


def test_existing_relations_paths() -> None:
    client = _client_with(_RowsBQ([{"relation_key": "p.marts.zombie", "table_type": "BASE TABLE"}]))
    assert client.existing_relations(set()) == []
    relations = client.existing_relations({"marts"})
    assert [r.relation_key for r in relations] == ["p.marts.zombie"]


def test_catalog_backed_checks_return_no_verdict_data() -> None:
    # Sizes come from catalog.json and BigQuery manages layout itself, so both live-metadata
    # checks stay empty and cost no warehouse call.
    client = _client_with(_RowsBQ([]))
    assert client.table_storage() == {}
    assert client.table_hygiene() == {}


def test_source_last_modified_paths() -> None:
    from datetime import datetime, timezone

    when = datetime(2026, 1, 2, tzinfo=timezone.utc)
    client = _client_with(_RowsBQ([{"relation_key": "p.raw.t", "last_modified": when}]))
    assert client.source_last_modified(set()) == {}
    assert client.source_last_modified({"p.raw"}) == {"p.raw.t": when}
    with pytest.raises(MissingPermissionError, match="stale-source check"):
        _client_with(_ForbiddenQueryBQ()).source_last_modified({"p.raw"})
