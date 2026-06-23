"""Tests for the real BigQuery client's preflight behaviour.

These bypass `__init__` (which would need live credentials) and inject a stub job handle, so
they exercise the client's own error handling without touching BigQuery.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.api_core.exceptions import Forbidden

from dbt_debt.config import Config
from dbt_debt.consumption.bigquery import RealBigQueryClient
from dbt_debt.consumption.client import MissingPermissionError


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
