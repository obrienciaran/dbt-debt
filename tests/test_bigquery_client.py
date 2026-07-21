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
    from google.api_core.exceptions import BadRequest, Forbidden
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
    BadRequest, Forbidden = _BadRequest, _Forbidden


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
